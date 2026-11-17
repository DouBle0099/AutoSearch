"""AutoSearch command line. All paths default to the project directory."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from engine import Engine, single_worker
from sources import SH

ROOT = Path(__file__).resolve().parent


def main():
    cli = argparse.ArgumentParser(description="AutoSearch 中文研究跟踪")
    cli.add_argument("--data", type=Path, default=ROOT / "data", help="SQLite 与报告目录")
    cli.add_argument("--settings", type=Path, default=ROOT / "settings.json")
    sub = cli.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="创建新研究周期")
    start.add_argument("--question", required=True)
    start.add_argument("--seed", action="append", required=True, help="代表论文题名，可补充摘要或研究笔记，可重复")
    start.add_argument("--term", action="append", help="英文检索短语。未指定时由模型生成")
    sub.add_parser("run", help="处理到期任务")
    watch = sub.add_parser("watch", help="持续处理，每隔指定分钟检查")
    watch.add_argument("--minutes", type=int, default=30)
    sub.add_parser("status", help="研究周期、来源状态、预算与投递记录")
    fb = sub.add_parser("feedback", help="用论文标识标记本轮命中")
    fb.add_argument("ident")
    fb.add_argument("note", choices=["相关", "偏题", "值得追踪"])
    reconcile = sub.add_parser("mail-resolve", help="人工核对送达状态")
    reconcile.add_argument("id")
    reconcile.add_argument("state", choices=["sent", "retry"])
    args = cli.parse_args()
    cfg = json.loads(args.settings.read_text(encoding="utf-8-sig"))
    if not (
        0 <= cfg["daily_hour"] <= 23
        and 1 <= cfg["highlights"] <= 50
        and cfg["lookback_days"] >= 1
        and cfg["discussion_seconds"] > 0
        and all(
            cfg[k] >= 0
            for k in ("daily_tokens", "monthly_tokens", "discussion_tokens", "report_reserve")
        )
    ):
        cli.error("设置值超出范围")
    if args.action == "watch" and args.minutes < 1:
        cli.error("检查间隔至少一分钟")
    while True:
        try:
            with single_worker(args.data):
                app = Engine(args.data, cfg)
                try:
                    if args.action == "start":
                        if not args.question.strip() or any(not s.strip() for s in args.seed):
                            cli.error("研究问题与代表论文不能为空")
                        cid = app.start(args.question, args.seed, args.term)
                        print("已创建周期", cid, app.cycle()["terms"])
                    elif args.action == "status":
                        print("当前周期", json.dumps(app.cycle(), ensure_ascii=False))
                        print("模型", app.model.name if app.model.ready else "未配置")
                        for row in app.db.execute(
                            "select source,day,error from checks where cycle=(select max(id) from cycles) order by day desc limit 20"
                        ):
                            print(*row)
                        print(
                            "当日用量（含未结算预留）",
                            app.db.execute(
                                "select coalesce(sum(tokens),0) from usage where day=?",
                                (datetime.now(SH).date().isoformat(),),
                            ).fetchone()[0],
                        )
                        print("归档", args.data.resolve() / "reports")
                        for row in app.db.execute("select * from mail where state='uncertain'"):
                            print("送达待确认", *row)
                    elif args.action == "feedback":
                        c = app.cycle()
                        rows = (
                            []
                            if not c
                            else app.db.execute(
                                "select batch from hits where cycle=? and ident=?", (c["id"], args.ident)
                            ).fetchall()
                        )
                        if not rows:
                            raise ValueError("本轮找不到该论文标识")
                        with app.db:
                            app.db.execute(
                                "insert or replace into feedback values(?,?,?)",
                                (c["id"], args.ident, args.note),
                            )
                            app.db.executemany(
                                "insert or ignore into dirty values(?,?)",
                                [(c["id"], r[0]) for r in rows],
                            )
                    elif args.action == "mail-resolve":
                        with app.db:
                            updated = app.db.execute(
                                "update mail set state=? where id=? and state='uncertain'",
                                (args.state, args.id),
                            )
                            if not updated.rowcount:
                                raise ValueError("找不到待确认邮件")
                    else:
                        app.tick()
                        for error in app.errors:
                            print(error, file=sys.stderr)
                        if not app.cycle():
                            print("等待创建研究周期。")
                        elif not app.errors:
                            print("到期任务检查完毕。")
                        if app.errors and args.action == "run":
                            return 1
                finally:
                    app.db.close()
        except (RuntimeError, OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            if args.action != "watch":
                return 1
        if args.action != "watch":
            return 0
        time.sleep(args.minutes * 60)


if __name__ == "__main__":
    raise SystemExit(main())
