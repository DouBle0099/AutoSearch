"""Offline integration tests via public entry points, source parsers and a stub LLM."""
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest
from unittest.mock import patch

import research
import sources
from engine import Engine
from sources import SH, paper


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = json.loads(Path(__file__).with_name("settings.json").read_text())
        self.now = datetime.now(SH).replace(hour=15)
        self.env = patch.dict(
            os.environ, {"AUTOSEARCH_MODEL": "", "AUTOSEARCH_API_KEY": "", "AUTOSEARCH_MAIL_TO": ""}
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = Engine(self.root, self.cfg)
        self.addCleanup(self.app.db.close)
        self.app.start("如何分配观测预算？", ["Representative paper"], ["sampling"])
        self.work = paper(
            "arxiv:1",
            "Sampling under constraints",
            "https://example.org/a",
            "Rare events remain difficult.",
            "A. Author",
            version="1v1",
            doi="10.1234/one",
        )

    def collect(self, source, c, day):
        if source == "arXiv":
            return [self.work]
        if source == "PMLR/ICML":
            return [self.work | {"key": "doi:10.1234/one", "group": "ICML 2026", "version": ""}]
        if source.startswith("期刊/"):
            return [self.work | {"group": "Journal 卷1 期2"}]
        return []

    def reports(self):
        return {r[0]: r[1] for r in self.app.db.execute("select label,body from reports")}

    def test_cycles_replay_and_outage(self):
        label = "arXiv " + self.now.date().isoformat()
        with patch.object(self.app, "collect", side_effect=OSError("offline")):
            self.app.tick(self.now)
        self.assertIn("待补采", self.reports()[label])
        with patch.object(self.app, "collect", side_effect=self.collect) as fetch:
            self.app.tick(self.now)
            self.assertIn("原文摘要", self.reports()[label])
            self.assertIn("关联版本", self.reports()[label])
            self.assertIn("ICML 2026", self.reports())
            self.assertIn("Journal 卷1 期2", self.reports())
            fetch.reset_mock()
            self.app.tick(self.now)
            fetch.assert_not_called()
            self.app.tick(self.now + timedelta(days=1))
            self.assertIn(
                "未发现", self.reports()["arXiv " + (self.now.date() + timedelta(days=1)).isoformat()]
            )
            self.app.start("不同的问题", ["Another paper"], ["unrelated"])
            self.app.tick(self.now)
            self.assertTrue((self.root / "reports" / "2" / (label + ".md")).exists())
            self.assertIn(
                "Sampling", (self.root / "reports" / "1" / (label + ".md")).read_text(encoding="utf-8")
            )

    def test_discussion_budget_and_cache(self):
        self.app.model.key, self.app.model.name = "stub", "demo"

        def llm(url, *, body, **kwargs):
            msg = json.loads(body)["messages"][-1]["content"]
            if "局限原文逐字" in msg:
                out = dict.fromkeys(research.FIELDS, "样例")
                out.update({"局限原文": "Rare events remain difficult.", "机制检索词": "rare events"})
            elif "0–2" in msg:
                out = {"设想": ["用相同预算比较召回率。"]}
            elif "假设冲突" in msg:
                out = {"保留": True, "理由": "样例"}
            else:
                out = {"判断": "样例"}
            return json.dumps(
                {"choices": [{"message": {"content": json.dumps(out)}}], "usage": {"total_tokens": 200}}
            ).encode()

        ref = {
            "title": ["Rare event sampling"],
            "abstract": "Adaptive allocation improves coverage.",
            "DOI": "10.1234/ref",
            "published": {"date-parts": [[2020]]},
        }
        with patch.object(self.app, "collect", side_effect=self.collect), patch.object(
            research, "request", side_effect=llm
        ), patch.object(research, "crossref", return_value={"items": [ref]}):
            self.app.tick(self.now)
            self.assertEqual(self.app.errors, [])
            self.assertTrue(any("相同预算" in body for body in self.reports().values()))
            batch, count = self.app.db.execute(
                "select batch,count(*) from usage where batch<>'' group by batch"
            ).fetchone()
            self.assertEqual(count, 4)
            with self.assertRaises(research.BudgetError):
                self.app.model.call("extra", {}, batch=batch)
            before = self.app.db.execute("select count(*) from usage").fetchone()[0]
            self.app.tick(self.now)
            self.assertEqual(before, self.app.db.execute("select count(*) from usage").fetchone()[0])
            self.cfg["daily_tokens"] = 0
            with self.assertRaises(research.BudgetError):
                self.app.model.call("another", {})

    def test_atomic_pagination_failure(self):
        def partial():
            yield self.work
            raise ValueError("bad page")

        with patch.object(self.app, "collect", return_value=partial()):
            self.app.ingest(self.app.cycle(), "arXiv", self.now.date())
        self.assertEqual(self.app.db.execute("select count(*) from hits").fetchone()[0], 0)
        self.assertTrue(self.app.db.execute("select error from checks").fetchone()[0])

    def test_rate_limit_survives_another_tick(self):
        from urllib.error import HTTPError

        self.cfg.update(pmlr=[], cvf=[], journals=[])
        err = HTTPError("https://example.org", 429, "limited", {"Retry-After": "3600"}, None)
        with patch.object(self.app, "collect", side_effect=err) as fetch:
            self.app.tick(self.now)
            self.app.tick(self.now)
            self.assertEqual(fetch.call_count, 1)
            self.assertIn("待补采", next(iter(self.reports().values())))

    def test_arxiv_parse(self):
        xml = f"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
        <o:totalResults>1</o:totalResults><entry><id>http://arxiv.org/abs/2001.12345v2</id>
        <updated>{self.now.isoformat()}</updated><title>sampling</title><summary>test</summary>
        <author><name>Jane</name></author></entry></feed>"""
        with patch.object(sources, "request", return_value=xml.encode()):
            rows = list(
                sources.arxiv(["sampling"], self.now - timedelta(days=1), self.now + timedelta(days=1))
            )
        self.assertEqual(rows[0]["key"], "arxiv:2001.12345")
        self.assertEqual(rows[0]["version"], "2001.12345v2")

    def test_old_reindex_and_online_first(self):
        day = self.now.date()
        old = {
            "DOI": "10.1234/old",
            "volume": "1",
            "issue": "1",
            "title": ["Sampling"],
            "published": {"date-parts": [[day.year - 1, 1, 1]]},
        }
        recent = old | {
            "DOI": "10.1234/issue",
            "journal-issue": {"published-print": {"date-parts": [[day.year, day.month]]}},
        }
        early = old | {
            "DOI": "10.1234/early",
            "issue": "",
            "published": {"date-parts": [[day.year, day.month, day.day]]},
        }
        with patch.object(sources, "crossref", return_value={"items": [old, recent, early]}):
            self.app.ingest(self.app.cycle(), "期刊/0162-8828", day)
        self.assertEqual(
            self.app.db.execute("select ident from hits").fetchall()[0][0], "doi:10.1234/issue"
        )
        self.assertEqual(self.app.db.execute("select count(*) from hits").fetchone()[0], 1)
        self.assertEqual(
            self.app.db.execute("select ident from online_first").fetchone()[0], "doi:10.1234/early"
        )

    def test_uncertain_mail_is_not_resent(self):
        from delivery import deliver

        row = dict(cycle=1, label="日报", body="一篇论文")
        with patch.dict(
            os.environ,
            {
                "AUTOSEARCH_MAIL_TO": "reader@example.org",
                "AUTOSEARCH_MAIL_FROM": "sender@example.org",
                "AUTOSEARCH_SMTP_HOST": "example.org",
            },
        ), patch("delivery.smtplib.SMTP_SSL") as server:
            send = server.return_value.__enter__.return_value.send_message
            send.side_effect = OSError("disconnected after DATA")
            with self.assertRaises(OSError):
                deliver(self.app.db, row)
            deliver(self.app.db, row)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.app.db.execute("select state from mail").fetchone()[0], "uncertain")


if __name__ == "__main__":
    unittest.main()
