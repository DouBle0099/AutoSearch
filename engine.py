"""Durable collection, report editing, and weekly research workflow."""
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time as daytime, timedelta
from hashlib import sha256
import json
import os
import re
import sqlite3
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

import sources
from delivery import deliver
from research import BudgetError, FIELDS, Model, analyze, dump, ideas
from sources import SH

SCHEMA = """
create table if not exists cycles(id integer primary key, created text, config text);
create table if not exists checks(cycle integer, source text, day text, error text,
 primary key(cycle,source,day));
create table if not exists hits(cycle integer, source text, ident text, version text,
 batch text, day text, rank real, data text,
 primary key(cycle,source,ident,version,batch));
create table if not exists cards(cycle integer, fingerprint text, data text, primary key(cycle,fingerprint));
create table if not exists reports(cycle integer, label text, body text, published text default '',
 primary key(cycle,label));
create table if not exists usage(id integer primary key,day text,batch text,tokens integer);
create table if not exists cost(usage_id integer primary key,price real);
create table if not exists cache(id text primary key,result text);
create table if not exists debates(batch text primary key, result text);
create table if not exists mail(id text primary key,label text,state text);
create table if not exists online_first(cycle integer,source text,ident text,data text,primary key(cycle,source,ident));
create table if not exists mail_queue(cycle integer,label text,recipient text,primary key(cycle,label));
create table if not exists dirty(cycle integer,label text,primary key(cycle,label));
create table if not exists cooldown(source text primary key,until real);
create table if not exists feedback(cycle integer, ident text, note text, primary key(cycle,ident));
create index if not exists hits_batch on hits(cycle,batch);
create index if not exists hits_doi on hits(cycle,json_extract(data,'$.doi'));
create index if not exists hits_title on hits(cycle,json_extract(data,'$.title') collate nocase);
"""


@contextmanager
def single_worker(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError("已有工作进程正在执行，请稍后再试") from e
        yield


def score(p, terms, seeds):
    title, abstract = p["title"].casefold(), p["abstract"].casefold()

    # A phrase matches punctuation variants but respects English word boundaries.
    def words(text):
        return " " + " ".join(re.findall(r"[^\W_]+", text.casefold())) + " "

    t, a = words(title), words(abstract)
    weight = sum(3 * (words(term) in t) + (words(term) in a) for term in terms)
    if weight:
        vocab = set(re.findall(r"[a-z]{5,}", " ".join(seeds).casefold()))
        weight += min(5, len(vocab & set(t.split()))) / 10
    return weight


class Engine:
    def __init__(self, directory, cfg):
        self.directory, self.cfg = directory, cfg
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "research.db"
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.model = Model(path, cfg)
        self.errors = []

    def cycle(self):
        row = self.db.execute("select * from cycles order by id desc limit 1").fetchone()
        return dict(json.loads(row["config"]), id=row["id"], created=row["created"]) if row else None

    def start(self, question, seeds, terms):
        seeds = [sources.resolve_seed(s) for s in seeds]
        if not terms:
            if self.model.ready:
                result = self.model.call(
                    '生成 3–6 个精确的英文论文检索短语，输出 {"terms":["phrase"]}。',
                    {"研究问题": question, "代表论文": seeds},
                    limit=200,
                )
                terms = result.get("terms")
            else:
                raise ValueError("未配置模型时，请用 --term 指定英文检索短语，可重复")
        if (
            not isinstance(terms, list)
            or not 1 <= len(terms) <= 12
            or any(not isinstance(t, str) or not t.strip() or len(t) > 160 for t in terms)
        ):
            raise ValueError("需要 1–12 个非空检索短语，每个至多 160 字符")
        config = dict(question=question, seeds=seeds, terms=[t.strip() for t in terms])
        with self.db:
            return self.db.execute(
                "insert into cycles(created,config) values(?,?)",
                (datetime.now(SH).date().isoformat(), dump(config)),
            ).lastrowid

    def sources(self):
        if self.cfg.get("arxiv", True):
            yield "arXiv"
        yield from ("PMLR/" + name for name in self.cfg["pmlr"])
        yield from ("CVF/" + name for name in self.cfg["cvf"])
        yield from ("期刊/" + v["issn"] for v in self.cfg["journals"])

    def collect(self, source, c, day):
        cutoff = datetime.combine(day, daytime(hour=self.cfg["daily_hour"]), SH)
        if source == "arXiv":
            return sources.arxiv(c["terms"], cutoff - timedelta(days=self.cfg["lookback_days"]), cutoff)
        kind, name = source.split("/", 1)
        if kind == "PMLR":
            return sources.pmlr(name)
        if kind == "CVF":
            return sources.cvf(name)
        venue = next(v for v in self.cfg["journals"] if v["issn"] == name)
        last = self.db.execute(
            "select max(day) from checks where cycle=? and source=? and error=''", (c["id"], source)
        ).fetchone()[0]
        start = date.fromisoformat(last) if last else day
        return sources.journal(venue, start - timedelta(days=self.cfg["lookback_days"]), day)

    def ingest(self, c, source, day):
        cid = c["id"]
        try:
            with self.db:
                for p in self.collect(source, c, day):
                    rank = score(p, c["terms"], c["seeds"])
                    if not rank:
                        continue
                    if source.startswith("期刊/"):
                        if not p["group"]:
                            self.db.execute(
                                "insert or replace into online_first values(?,?,?,?)",
                                (cid, source, p["key"], dump(p)),
                            )
                            continue
                        self.db.execute(
                            "delete from online_first where cycle=? and source=? and ident=?",
                            (cid, source, p["key"]),
                        )
                    batch = p["group"] or "arXiv " + day.isoformat()
                    # arXiv overlapping queries can rediscover the same exact version on later days.
                    if (
                        source == "arXiv"
                        and self.db.execute(
                            "select 1 from hits where cycle=? and source=? and ident=? and version=?",
                            (cid, source, p["key"], p["version"]),
                        ).fetchone()
                    ):
                        continue
                    added = self.db.execute(
                        "insert or ignore into hits values(?,?,?,?,?,?,?,?)",
                        (cid, source, p["key"], p["version"], batch, day.isoformat(), rank, dump(p)),
                    )
                    if added.rowcount:
                        self.db.execute("insert or ignore into dirty values(?,?)", (cid, batch))
                self.db.execute(
                    "insert or replace into checks values(?,?,?,?)", (cid, source, day.isoformat(), "")
                )
                if source == "arXiv":
                    self.db.execute(
                        "insert or ignore into dirty values(?,?)", (cid, "arXiv " + day.isoformat())
                    )
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            with self.db:
                if isinstance(e, HTTPError) and e.code in (429, 503):
                    retry = (e.headers or {}).get("Retry-After", "1800")
                    try:
                        until = (
                            time.time() + int(retry)
                            if retry.isdigit()
                            else parsedate_to_datetime(retry).timestamp()
                        )
                    except (ValueError, TypeError, OverflowError):
                        until = time.time() + 1800
                    self.db.execute(
                        "insert or replace into cooldown values(?,?)",
                        (source, max(time.time() + 60, until)),
                    )
                self.db.execute(
                    "insert or replace into checks values(?,?,?,?)", (cid, source, day.isoformat(), msg)
                )
            self.errors.append(source + " " + msg)

    def save(self, cid, label, body):
        with self.db:
            self.db.execute(
                """insert into reports(cycle,label,body) values(?,?,?)
                on conflict(cycle,label) do update set body=excluded.body""",
                (cid, label, body),
            )
            self.db.execute("delete from dirty where cycle=? and label=?", (cid, label))
            self.queue_mail(cid, label)

    def queue_mail(self, cid, label):
        recipient = os.getenv("AUTOSEARCH_MAIL_TO")
        if recipient:
            self.db.execute("insert or replace into mail_queue values(?,?,?)", (cid, label, recipient))

    def fingerprint(self, c, p):
        return sha256(dump([c["id"], p, self.model.name, self.model.endpoint, 2]).encode()).hexdigest()

    def report(self, c, label):
        cid = c["id"]
        rows = list(
            self.db.execute(
                """select h.* from hits h left join feedback f
            on f.cycle=h.cycle and f.ident=h.ident where h.cycle=? and h.batch=?
            order by case f.note when '值得追踪' then 1 when '偏题' then -1 else 0 end desc,
            rank desc,ident""",
                (cid, label),
            )
        )
        lines = [
            f"# {label}",
            "",
            f"研究周期 {cid} · {c['question']}",
            "",
            "本轮检索词：" + "、".join(c["terms"]),
            "",
        ]
        if label.startswith("arXiv "):
            check = self.db.execute(
                "select error from checks where cycle=? and source='arXiv' and day=?",
                (cid, label.split()[1]),
            ).fetchone()
            if check and check[0]:
                lines.append("**来源检查失败，待补采。** " + check[0])
            elif not rows:
                lines.append("本次检查成功，未发现新的匹配版本。")
        if rows:
            if not label.startswith("arXiv "):
                lines.append(f"收录范围：本轮已采集的主题命中；最近新增于 {max(r['day'] for r in rows)}。同批次后续新增会更新此专报。")
            counts = Counter()
            for r in rows:
                p = json.loads(r["data"])
                counts.update(t for t in c["terms"] if score(p, [t], []))
            lines.append("主题命中（可重叠）：" + "、".join(f"{t} {n} 篇" for t, n in counts.most_common()) + "。")
            if label.startswith("arXiv "):
                end = date.fromisoformat(label.split()[1])
                history = self.db.execute(
                    """select day,ident,data from hits where cycle=? and source='arXiv'
                    and day between ? and ?""",
                    (cid, (end - timedelta(days=13)).isoformat(), end.isoformat()),
                ).fetchall()
                lines.extend(["", "近两周主题走势（按发现日，论文按标识去重，研究团队以首作者近似）："])
                for term, _ in counts.most_common(5):
                    previous, current, authors = set(), set(), set()
                    for item in history:
                        work = json.loads(item["data"])
                        if score(work, [term], []):
                            if date.fromisoformat(item["day"]) > end - timedelta(days=7):
                                current.add(item["ident"])
                                if work["authors"]:
                                    authors.add(work["authors"].split(",")[0].strip().casefold())
                            else:
                                previous.add(item["ident"])
                    lines.append(
                        f"- {term}：近 7 天 {len(current)} 篇，前 7 天 {len(previous)} 篇，近 7 天首作者 {len(authors)} 位。"
                    )
                lines.append("本轮跟踪始于 " + c["created"] + "，启动前的窗口可能不完整。")
            lines.extend(["", f"## 精选（共命中 {len(rows)} 条）"])
        deferred = False
        for row in rows[: self.cfg["highlights"]]:
            p = json.loads(row["data"])
            lines.extend(["", f"### [{p['title']}]({p['url']})", p["authors"]])
            if p["doi"]:
                lines.append("正式出版标识：" + p["doi"])
                related = self.db.execute(
                    """select data from hits where cycle=? and ident<>?
                    and json_extract(data,'$.doi')=? limit 1""",
                    (cid, p["key"], p["doi"]),
                ).fetchone()
                if related:
                    lines.append("关联版本：" + json.loads(related[0])["url"])
            else:
                possible = self.db.execute(
                    """select data from hits where cycle=? and ident<>?
                    and json_extract(data,'$.title')=? collate nocase limit 3""",
                    (cid, p["key"], p["title"]),
                ).fetchall()
                for item in possible:
                    other = json.loads(item[0])
                    if other["authors"] and other["authors"].casefold() == p["authors"].casefold():
                        lines.append("同题名、同作者的候选版本（待核对）：" + other["url"])
            key = self.fingerprint(c, p)
            saved = self.db.execute(
                "select data from cards where cycle=? and fingerprint=?", (cid, key)
            ).fetchone()
            card = json.loads(saved[0]) if saved else None
            if card is None and self.model.ready:
                try:
                    ev = sources.evidence(p)
                    if not ev["摘要"] and not ev["章节"]:
                        lines.append("仅有题录，缺少可核查的解读材料。")
                        continue
                    card = analyze(self.model, c, p, ev)
                    card["范围"] = "摘要及正文章节" if ev["章节"] else "仅据摘要"
                    with self.db:
                        self.db.execute(
                            "insert or replace into cards values(?,?,?)", (cid, key, dump(card))
                        )
                except Exception as e:
                    deferred = True
                    lines.append("中文解读待补充：" + str(e))
                    self.errors.append(str(e))
            if card:
                lines.append("材料范围：" + card["范围"])
                lines.extend(f"- **{f}**　{card[f]}" for f in FIELDS[:6])
            elif not self.model.ready:
                lines.append("未配置模型，已保留原文摘要或题录供后续解读。")
                if p["abstract"]:
                    lines.append(p["abstract"][:800])
        if rows:
            lines.extend(["", "## 本期命中索引", ""])
            lines.extend(
                f"- [{p['title']}]({p['url']}) · 标识 `{p['key']}`"
                for p in (json.loads(r["data"]) for r in rows)
            )
        self.save(cid, label, "\n".join(lines) + "\n")
        return deferred

    def debate(self, c, day):
        week = day.strftime("%G-W%V")
        batch = f"{c['id']}/{week}"
        if (
            not self.model.ready
            or self.db.execute("select 1 from debates where batch=?", (batch,)).fetchone()
        ):
            return
        rows = self.db.execute(
            "select data from hits where cycle=? and day>=? order by rank desc,day desc",
            (c["id"], (day - timedelta(days=7)).isoformat()),
        ).fetchall()
        for row in rows:
            p = json.loads(row[0])
            key = self.fingerprint(c, p)
            r = self.db.execute(
                "select data from cards where cycle=? and fingerprint=?", (c["id"], key)
            ).fetchone()
            if not r or not (card := json.loads(r[0]))["局限原文"]:
                continue
            library = (
                json.loads(item[0])
                for item in self.db.execute("select data from hits order by day desc limit 400")
            )
            try:
                body = ideas(
                    self.model,
                    batch,
                    card,
                    p,
                    library,
                    time.monotonic() + self.cfg["discussion_seconds"],
                )
            except BudgetError as e:
                body = "本批讨论已停止：" + str(e)
            except Exception as e:
                body = "本批讨论中止：" + str(e)
                self.errors.append(body)
            label = "研究设想 " + week
            with self.db:
                self.db.execute("insert into debates values(?,?)", (batch, body))
                self.db.execute(
                    "insert into reports(cycle,label,body) values(?,?,?)",
                    (c["id"], label, f"# {label}\n\n{c['question']}\n\n{body}\n"),
                )
                self.queue_mail(c["id"], label)
            return

    def publish(self):
        for r in self.db.execute("select * from reports").fetchall():
            path = self.directory / "reports" / str(r["cycle"])
            path.mkdir(parents=True, exist_ok=True)
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", r["label"])
            file = path / (name + ".md")
            digest = sha256(r["body"].encode()).hexdigest()
            if r["published"] != digest or not file.exists():
                tmp = file.with_suffix(".tmp")
                tmp.write_text(r["body"], encoding="utf-8")
                tmp.replace(file)
                with self.db:
                    self.db.execute(
                        "update reports set published=? where cycle=? and label=?",
                        (digest, r["cycle"], r["label"]),
                    )
            try:
                if self.db.execute(
                    "select 1 from mail_queue where cycle=? and label=? and recipient=?",
                    (r["cycle"], r["label"], os.getenv("AUTOSEARCH_MAIL_TO")),
                ).fetchone():
                    deliver(self.db, r)
            except Exception as e:
                self.errors.append("邮件投递：" + str(e))

    def tick(self, now=None):
        self.errors.clear()
        c = self.cycle()
        if not c:
            return
        now = now or datetime.now(SH)
        day = now.date()
        if now.hour < self.cfg["daily_hour"]:
            day -= timedelta(days=1)
        if day < date.fromisoformat(c["created"]):
            return
        cid = c["id"]
        labels = set()
        labels.update(r[0] for r in self.db.execute("select label from dirty where cycle=?", (cid,)))
        for source in self.sources():
            if self.db.execute(
                "select 1 from cooldown where source=? and until>?", (source, time.time())
            ).fetchone():
                self.errors.append(source + " 处于来源限流等待期")
                if source == "arXiv":
                    with self.db:
                        self.db.execute(
                            "insert or ignore into checks values(?,?,?,?)",
                            (cid, source, day.isoformat(), "来源限流，待补采"),
                        )
                    labels.add("arXiv " + day.isoformat())
                continue
            # Daily arXiv windows can be reconstructed. Other sources expose current catalogs.
            first = date.fromisoformat(c["created"]) if source == "arXiv" else day
            done = {
                r[0]
                for r in self.db.execute(
                    "select day from checks where cycle=? and source=? and error=''", (cid, source)
                )
            }
            while first <= day:
                if first.isoformat() not in done:
                    self.ingest(c, source, first)
                    if source == "arXiv":
                        labels.add("arXiv " + first.isoformat())
                    else:
                        labels.update(
                            r[0]
                            for r in self.db.execute(
                                "select distinct batch from hits where cycle=? and source=? and day=?",
                                (cid, source, first.isoformat()),
                            )
                        )
                    if self.db.execute(
                        "select error from checks where cycle=? and source=? and day=?",
                        (cid, source, first.isoformat()),
                    ).fetchone()[0]:
                        break
                first += timedelta(days=1)
        # Recover an interrupted collect -> edit transition and revisit pending summaries.
        labels.update(
            r[0]
            for r in self.db.execute(
                """select distinct batch from hits where cycle=?
            except select label from reports where cycle=?""",
                (cid, cid),
            )
        )
        labels.update(
            "arXiv " + r[0]
            for r in self.db.execute(
                """select day from checks where cycle=? and source='arXiv'
            except select substr(label,7) from reports where cycle=? and label like 'arXiv %'""",
                (cid, cid),
            )
        )
        if self.model.ready:
            labels.update(
                r[0]
                for r in self.db.execute(
                    """select label from reports where cycle=? and
                (body like '%中文解读待补充%' or body like '%未配置模型%')""",
                    (cid,),
                )
            )
        for label in sorted(labels, key=lambda s: (not s.startswith("arXiv "), s)):
            self.report(c, label)
        self.debate(c, day)
        self.publish()
