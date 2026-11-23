"""第一阶段端到端测试。用独立数据库，不污染主库。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qq_guard.config import AppConfig, load_config, CONFIG_PATH
from qq_guard.notifiers import Notifier
from qq_guard.sources import LocalSource
from qq_guard.store import Store
from qq_guard.app import App

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def main() -> None:
    cfg = load_config()
    cfg.db_path = "_test_phase1.db"
    cfg.notify.desktop_enabled = False
    cfg.notify.summary_enabled = False
    db = Path(cfg.db_path)
    db.unlink(missing_ok=True)

    app = App(cfg)

    # 1. 分级与结构化提取
    print("\n=== 1. 分级 + 结构化提取 ===")
    msgs: list = []
    LocalSource("sample_p0p3.json").run(lambda m: msgs.append((m, app.handle(m))))
    stats = app.store.stats()
    # P0 标准：现在不处理就会错过/效果打折（含"明天+名额有限"）
    check("四级分类结果", stats.get("P0") == 3 and stats.get("P1") == 2
          and stats.get("P2") == 2 and stats.get("P3") == 4, json.dumps(stats, ensure_ascii=False))
    p0 = [(m, v) for m, v in msgs if v.level == "P0"]
    check("P0 有截止时间抽取", any(v.info.deadline != "待核实" for _, v in p0),
          f"deadline={p0[0][1].info.deadline}" if p0 else "无P0")
    check("P0 有行动要求", any(v.info.needs_action == "是" for _, v in p0))
    check("私聊不监控", any(v.level == "P3" and m.is_private for m, v in msgs))
    check("重复消息降为P3", any(v.is_duplicate and v.level == "P3" for _, v in msgs))

    # 2. 即时提醒格式
    print("\n=== 2. 即时提醒格式 ===")
    m, v = p0[0]
    alert = Notifier.format_alert(m, v)
    required = ["【优先级】", "【事项标题】", "【来源群聊及发送者】", "【消息时间】",
                "【重要原因】", "【需要采取的行动】", "【截止时间】", "【原始消息】",
                "【可信度】", "【消息ID】"]
    check("提醒九要素齐全", all(k in alert for k in required),
          f"缺{[k for k in required if k not in alert]}")
    check("提醒保留原文", m.content in alert)
    check("提醒保留消息ID", m.message_id in alert)

    # 3. 摘要
    print("\n=== 3. 每日摘要 ===")
    pending = app.store.pending_digest(["P0", "P1", "P2"])
    digest = Notifier.format_digest(pending)
    check("摘要五类结构", all(k in digest for k in ["今日重要通知", "科研、竞赛与活动机会",
                                                "课程、考试与学习事务", "值得保留的资料和讨论"]))
    check("摘要不含P3", "哈哈哈哈" not in digest)
    check("摘要保留来源", "班级群" in digest)
    check("摘要保留消息ID", "p0-001" in digest)
    empty_digest = Notifier.format_digest([])
    check("无内容摘要不凑数", "没有需要汇总" in empty_digest)

    # 4. 查询
    print("\n=== 4. 历史查询 ===")
    check("按群查询", len(app.store.query(group="班级群")) == 2)
    check("按关键词查询", len(app.store.query(keyword="奖学金")) == 1)
    check("按优先级查询", len(app.store.query(priority="P0")) == 3)
    check("按类别查询", len(app.store.query(category="科研")) >= 1)
    today = time.strftime("%Y-%m-%d", time.localtime(msgs[0][0].timestamp))
    check("按日期查询", len(app.store.query(date=today)) >= 1, f"date={today}")

    # 5. 留存清理（dry-run + 实际）
    print("\n=== 5. 留存清理 ===")
    import sqlite3 as _s
    with _s.connect(str(db)) as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE level IN ('P2','P3')",
                     (int(time.time()) - 8 * 86400,))
    dry = app.store.cleanup(days=3, important_days=60, dry_run=True)
    check("清理预演数量(P2/P3超3天)", dry == 6, f"应清理6条，实际{dry}")
    deleted = app.store.cleanup(days=3, important_days=60)
    check("实际清理", deleted == 6, f"删除{deleted}条")
    check("P0/P1 未被误删", app.store.stats().get("P0") == 3 and app.store.stats().get("P1") == 2)

    # P0/P1 到期也应清理（按 important_days）
    with _s.connect(str(db)) as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE level IN ('P0','P1')",
                     (int(time.time()) - 90 * 86400,))
    old_imp = app.store.cleanup(days=3, important_days=60, dry_run=True)
    check("P0/P1 超60天可清理", old_imp == 5, f"应清理5条，实际{old_imp}")

    # 6. 备份恢复
    print("\n=== 6. 备份恢复 ===")
    from qq_guard import backup as bk
    orig = CONFIG_PATH.read_bytes()
    bdir = bk.create_backup(cfg, label="第一阶段测试")
    ok, problems = bk.verify_backup(bdir)
    check("备份完整性校验", ok, str(problems))
    check("备份含新字段CSV", "优先级" in (bdir / "export" / "messages.csv").read_text(encoding="utf-8-sig"))
    ok2, msg = bk.restore_backup(bdir.name)
    check("一键恢复", ok2, msg.replace("\n", " / ")[:80])
    CONFIG_PATH.write_bytes(orig)  # 恢复测试配置，避免污染

    # 7. 旧数据兼容
    print("\n=== 7. 旧数据兼容 ===")
    cfg2 = load_config()
    cfg2.db_path = "_test_legacy.db"
    legacy = Path(cfg2.db_path)
    legacy.unlink(missing_ok=True)
    import sqlite3
    with sqlite3.connect(str(legacy)) as conn:
        conn.execute("""CREATE TABLE messages (id TEXT PRIMARY KEY, ts INTEGER, sender_id TEXT,
            sender_name TEXT, group_id TEXT, group_name TEXT, content TEXT, is_private INTEGER,
            level TEXT, score INTEGER, reason TEXT, notified INTEGER DEFAULT 0,
            digested INTEGER DEFAULT 0)""")
        conn.execute("INSERT INTO messages (id,ts,sender_id,sender_name,content,level,score,reason) "
                     "VALUES ('old1',?, '1','老师','旧紧急消息','urgent',90,'旧数据')",
                     (int(time.time()),))
    s = Store(cfg2)
    old = s.all_messages()
    check("旧数据迁移", len(old) == 1 and old[0][1].level == "P0",
          f"level={old[0][1].level}" if old else "无数据")
    check("旧库补全新列", "category" in [r[1] for r in __import__("sqlite3").connect(str(legacy)).execute("PRAGMA table_info(messages)").fetchall()])

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"总计 {passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  失败: {name} | {detail}")


if __name__ == "__main__":
    main()
