"""Public literature sources. Return normalized paper records, without model calls."""
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import json
import os
import re
import time
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

SH = timezone(timedelta(hours=8))
ATOM = "{http://www.w3.org/2005/Atom}"
ARX = "{http://arxiv.org/schemas/atom}"
last_arxiv = 0.0


def request(url, params=None, *, body=None, headers=None, timeout=30):
    global last_arxiv
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    if urlparse(url).hostname == "export.arxiv.org":
        time.sleep(max(0, last_arxiv + 3.1 - time.monotonic()))
        last_arxiv = time.monotonic()
    base = {"User-Agent": "AutoSearch/1.0 " + os.getenv("AUTOSEARCH_CONTACT", "")}
    req = Request(url, data=body, headers={**base, **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        data = response.read(32_000_001)
        if len(data) > 32_000_000:
            raise ValueError("来源响应超过 32 MB")
        return data


def clean(value):
    return " ".join(str(value or "").split())


def paper(key, title, url, abstract="", authors="", version="", doi="", group="", year=""):
    return dict(
        key=key,
        title=clean(title),
        url=url,
        abstract=clean(abstract),
        authors=clean(authors),
        version=str(version),
        doi=doi.lower(),
        group=group,
        year=str(year),
    )


def resolve_seed(value):
    """Expand supported identifiers once, when the research cycle is created."""
    value = value.strip()
    ident = re.fullmatch(
        r"(?:https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?/?",
        value,
        re.I,
    )
    if ident:
        feed = ET.fromstring(request("https://export.arxiv.org/api/query", {"id_list": ident[1]}))
        entry = feed.find(ATOM + "entry")
        if entry is None or "/abs/" not in entry.findtext(ATOM + "id", ""):
            raise ValueError("未找到代表论文 " + value)
        return (
            clean(entry.findtext(ATOM + "title"))
            + " ["
            + value
            + "] "
            + clean(entry.findtext(ATOM + "summary"))[:900]
        )
    doi = re.fullmatch(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)", value, re.I)
    if doi:
        item = json.loads(request("https://api.crossref.org/works/" + quote(doi[1], safe="/")))[
            "message"
        ]
        p = from_crossref(item)
        return p["title"] + " [" + p["url"] + "] " + p["abstract"][:900]
    return value


def arxiv(terms, start, end):
    query = " OR ".join('all:"' + term.replace('"', "") + '"' for term in terms)
    offset = 0
    while True:
        raw = request(
            "https://export.arxiv.org/api/query",
            dict(
                search_query=query,
                start=offset,
                max_results=100,
                sortBy="lastUpdatedDate",
                sortOrder="descending",
            ),
        )
        root = ET.fromstring(raw)
        if root.tag != ATOM + "feed":
            raise ValueError("arXiv 返回未知格式")
        entries = root.findall(ATOM + "entry")
        total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
        if total is None:
            raise ValueError("arXiv 缺少结果总数")
        for e in entries:
            url = e.findtext(ATOM + "id", "")
            if "/abs/" not in url:
                raise ValueError("arXiv 返回无效记录")
            updated = e.findtext(ATOM + "updated")
            date = datetime.fromisoformat(updated).astimezone(SH)
            if date < start:
                return
            if date >= end:
                continue
            ident = url.split("/abs/")[-1]
            yield paper(
                "arxiv:" + re.sub(r"v\d+$", "", ident),
                e.findtext(ATOM + "title"),
                "https://arxiv.org/abs/" + ident,
                e.findtext(ATOM + "summary"),
                ", ".join(a.findtext(ATOM + "name", "") for a in e.findall(ATOM + "author")),
                ident,
                e.findtext(ARX + "doi", ""),
                year=date.year,
            )
        offset += len(entries)
        if offset >= int(total):
            break
        if not entries:
            raise ValueError("arXiv 分页提前中断")


def crossref(params, issn=None, *, timeout=30):
    base = "https://api.crossref.org/"
    url = base + ("journals/" + issn + "/" if issn else "") + "works"
    params = dict(params)
    if os.getenv("AUTOSEARCH_CONTACT"):
        params["mailto"] = os.environ["AUTOSEARCH_CONTACT"]
    return json.loads(request(url, params, timeout=timeout))["message"]


def from_crossref(item, group=""):
    doi = item["DOI"]
    abstract = BeautifulSoup(item.get("abstract", ""), "html.parser").get_text(" ", strip=True)
    parts = item.get("published", {}).get("date-parts", [[]])[0]
    authors = ", ".join(
        clean(a.get("given", "") + " " + a.get("family", "")) for a in item.get("author", [])
    )
    return paper(
        "doi:" + doi.lower(),
        next(iter(item.get("title") or []), doi),
        "https://doi.org/" + doi,
        abstract,
        authors,
        doi=doi,
        group=group,
        year=parts[0] if parts else "",
    )


def journal(venue, start, end):
    cursor = "*"
    while True:
        result = crossref(
            {
                "filter": f"from-index-date:{start},until-index-date:{end},type:journal-article",
                "rows": 200,
                "cursor": cursor,
            },
            venue["issn"],
        )
        items = result["items"]
        for item in items:
            vol, issue = item.get("volume", ""), item.get("issue", "")
            # Online publication can precede formal issue publication by many months.
            issue_dates = item.get("journal-issue", {})
            dates = (
                issue_dates.get("published-print"),
                item.get("published-print"),
                issue_dates.get("published-online"),
                item.get("published"),
            )
            date_parts = next(
                (d["date-parts"][0] for d in dates if d and d.get("date-parts") and d["date-parts"][0]),
                [],
            )
            if not date_parts:
                continue
            year = date_parts[0]
            month = date_parts[1] if len(date_parts) > 1 else 1
            earliest = date(year, month, date_parts[2] if len(date_parts) > 2 else 1)
            last_month = month if len(date_parts) > 1 else 12
            latest = date(
                year,
                last_month,
                date_parts[2] if len(date_parts) > 2 else monthrange(year, last_month)[1],
            )
            if latest < start or earliest > end:
                continue
            group = f"{venue['name']} {date_parts[0]} 卷{vol} 期{issue}"
            if not issue:
                group = (
                    f"{venue['name']} 连续出版 {date_parts[0]}-{date_parts[1] if len(date_parts)>1 else 1:02}"
                    if venue.get("continuous")
                    else ""
                )
            yield from_crossref(item, group)
        if len(items) < 200:
            break
        cursor = result["next-cursor"]


def pmlr(venue):
    base = "https://proceedings.mlr.press/"
    index = BeautifulSoup(request(base), "html.parser")
    volumes = []
    for li in index.select("li"):
        label = li.get_text(" ", strip=True)
        match = re.fullmatch(
            r"Volume\s+(\d+)\s+Proceedings of\s+" + re.escape(venue) + r"\s+(\d{4})", label, re.I
        )
        if match and li.a:
            volumes.append((int(match[2]), int(match[1]), urljoin(base, li.a["href"])))
    if not volumes:
        raise ValueError("PMLR 未识别到 " + venue + " 主会目录")
    year, _, url = max(volumes)
    soup = BeautifulSoup(request(url), "html.parser")
    entries = soup.select("div.paper")
    if not entries:
        raise ValueError("PMLR 论文集缺少论文列表")
    for entry in entries:
        link = next((a for a in entry.select("a[href]") if a.get_text(strip=True) == "abs"), None)
        if link is None or entry.select_one(".title") is None:
            raise ValueError("PMLR 论文条目缺少题名或链接")
        href = urljoin(url, link["href"])
        authors = entry.select_one(".authors")
        yield paper(
            href,
            entry.select_one(".title").get_text(" "),
            href,
            authors=authors.get_text(" ") if authors else "",
            group=f"{venue} {year}",
            year=year,
        )


def cvf(venue):
    base = "https://openaccess.thecvf.com/"
    index = BeautifulSoup(request(base), "html.parser")
    editions = []
    for a in index.select("a[href]"):
        url = urljoin(base, a["href"])
        match = re.fullmatch("/" + re.escape(venue) + r"(\d{4})/?", urlparse(url).path, re.I)
        if match and not urlparse(url).query:
            editions.append((int(match[1]), url))
    if not editions:
        raise ValueError("CVF 未识别到 " + venue + " 主会目录")
    year, url = max(editions)
    soup = BeautifulSoup(request(url), "html.parser")
    if not soup.select("dt.ptitle"):
        all_days = next((a for a in soup.select("a[href]") if "day=all" in a["href"].lower()), None)
        if all_days:
            soup = BeautifulSoup(request(urljoin(url, all_days["href"])), "html.parser")
    entries = soup.select("dt.ptitle")
    if not entries:
        raise ValueError("CVF 论文集缺少论文列表")
    for entry in entries:
        href = urljoin(base, entry.a["href"])
        authors = entry.find_next_sibling("dd")
        yield paper(
            href,
            entry.get_text(" "),
            href,
            authors=authors.get_text(" ") if authors else "",
            group=f"{venue} {year}",
            year=year,
        )


def evidence(p):
    """Only fetch official HTML for selected papers. Always retain the abstract fallback."""
    sections = []
    url = p["url"]
    host = urlparse(url).hostname
    if host in {"proceedings.mlr.press", "openaccess.thecvf.com"} and not p["abstract"]:
        soup = BeautifulSoup(request(url), "html.parser")
        abstract = soup.select_one("#abstract, .abstract")
        if abstract:
            p["abstract"] = clean(abstract.get_text(" "))
    if host == "arxiv.org":
        try:
            soup = BeautifulSoup(request(url.replace("/abs/", "/html/"), timeout=12), "html.parser")
            for s in soup.select("section.ltx_section"):
                title = s.find(["h2", "h3"])
                if title and re.search(
                    "method|experiment|result|limitation|discussion|conclusion", title.get_text(), re.I
                ):
                    sections.append(clean(s.get_text(" "))[:1500])
        except (OSError, ValueError):
            pass
    return {"摘要": p["abstract"][:3500], "章节": sections[:5]}
