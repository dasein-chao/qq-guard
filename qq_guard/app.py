"""应用编排：加载配置 → 接收消息 → 分级 → 存储 → 提醒 → 摘要 → 清理。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import AppConfig, load_config, upgrade_config_file, CONFIG_PATH, ROOT
from .engine import Classifier
from .models import LEVEL_LABEL, Message, Verdict
from .notifiers import Notifier
from .sources import LocalSource, OneBotWebSocketSource
from .store import Store


class App:
    def __init__(self, cfg: AppConfig, ai_client=None):
        self.cfg = cfg
        self.classifier = Classifier(cfg.rules, ai_client=ai_client)
        self.notifier = Notifier(cfg.notify, cfg.rules)
        self.store = Store(cfg)
        self._last_summary_hour = -1
        self._last_cleanup_hour = -1

    def handle(self, msg: Message) -> Verdict:
        # 私聊不监控：直接存为 P3 但不提醒不摘要（保留原文用于合规审计）
        verdict = self.classifier.classify(msg)
        notified = False
        if verdict.needs_notify:
            notified = self.notifier.notify_urgent(msg, verdict)
        self.store.insert(msg, verdict, notified)
        flag = " [重复]" if verdict.is_duplicate else ""
        print(f"[{verdict.level} {verdict.label}]{flag} {msg.source_label}: "
              f"{msg.content[:60]} (分数{verdict.score} | {verdict.reason})")
        return verdict

    def send_summary(self) -> None:
        items = self.store.pending_digest(self.cfg.rules.digest_levels)
        if not items:
            print("[摘要] 今日没有需要汇总的重要信息。")
            return
        title = f"QQ消息摘要 {time.strftime('%m-%d %H:%M')}"
        ok = self.notifier.send_digest(items, title)
        if ok:
            self.store.mark_digested([m.message_id for m, _ in items])
            print(f"[摘要] 已发送 {len(items)} 条")

    def cleanup(self) -> int:
        r = self.cfg.retention
        keep = r.important_days if not r.keep_p0p1_forever else 0
        n = self.store.cleanup(days=r.normal_days, important_days=keep)
        keep_desc = "永久" if keep == 0 else f"{keep} 天"
        print(f"[清理] 删除 {n} 条：P2/P3 超 {r.normal_days} 天，P0/P1 超 {keep_desc}")
        return n

    def _maybe_summary(self) -> None:
        now = time.localtime()
        if (self.cfg.notify.summary_enabled
                and now.tm_hour == self.cfg.notify.summary_hour
                and self._last_summary_hour != now.tm_hour):
            self._last_summary_hour = now.tm_hour
            self.send_summary()

    def _maybe_cleanup(self) -> None:
        now = time.localtime()
        if (now.tm_hour == self.cfg.retention.cleanup_hour
                and self._last_cleanup_hour != now.tm_hour):
            self._last_cleanup_hour = now.tm_hour
            self.cleanup()

    def run_local(self, sample_path: str | Path) -> None:
        print("=== 本地验证模式 ===")
        LocalSource(sample_path).run(self.handle)
        print("\n=== 分级统计 ===")
        print(self.store.stats())
        if self.cfg.notify.summary_enabled:
            self.send_summary()

    def run_listen(self) -> None:
        print("=== 实时监听模式 ===")
        s = self.cfg.source
        if not s.websocket_url.startswith(("ws://127.0.0.1", "ws://localhost", "ws://[::1]")):
            print("[安全拒绝] 协议端地址必须是本机回环，禁止远程地址")
            return
        OneBotWebSocketSource(
            s.websocket_url,
            token_file=s.access_token_file,
        ).run(self.handle)

    def run_daemon(self) -> None:
        import threading

        def loop():
            while True:
                self._maybe_summary()
                self._maybe_cleanup()
                time.sleep(60)

        threading.Thread(target=loop, daemon=True).start()
        self.run_listen()

    def show_stats(self) -> None:
        print("=== 存储统计 ===")
        print(json.dumps(self.store.stats(), ensure_ascii=False, indent=2))
        print("\n=== 最近消息 ===")
        for msg, verdict in self.store.recent(20):
            print(f"[{verdict.level} {verdict.label}] {msg.source_label}: "
                  f"{msg.content[:60]} | {verdict.reason}")

    def show_query(self, group: str, date: str, keyword: str, category: str, priority: str) -> None:
        results = self.store.query(group=group, date=date, keyword=keyword,
                                   category=category, priority=priority)
        print(f"=== 查询结果 {len(results)} 条 ===")
        for msg, verdict in results:
            print(f"[{verdict.level}] {msg.group_name or msg.group_id}/{msg.sender_name}: "
                  f"{msg.content[:70]} | 类别={verdict.info.category} "
                  f"截止={verdict.info.deadline} ID={msg.message_id}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="QQ 群聊智能监控与信息筛选系统")
    parser.add_argument("mode", nargs="?", default="local",
                        choices=["local", "listen", "daemon", "stats", "summary", "config",
                                 "backup", "backups", "verify", "restore", "query",
                                 "cleanup", "upgrade-config"],
                        help="local=本地验证, listen=实时监听, daemon=监听+摘要+清理, "
                             "stats=统计, summary=立即发摘要, config=生成默认配置, "
                             "backup/backups/verify/restore=备份恢复, query=历史查询, "
                             "cleanup=清理普通消息, upgrade-config=补齐配置新字段")
    parser.add_argument("--sample", default=str(ROOT / "sample_messages.json"))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--from", dest="from_backup", default="latest")
    parser.add_argument("--label", default="")
    parser.add_argument("--group", default="", help="query: 群名或群号")
    parser.add_argument("--date", default="", help="query: 日期 YYYY-MM-DD")
    parser.add_argument("--keyword", default="", help="query: 关键词")
    parser.add_argument("--category", default="", help="query: 类别")
    parser.add_argument("--priority", default="", help="query: P0/P1/P2/P3")
    parser.add_argument("--dry-run", action="store_true", help="cleanup: 只统计不删除")
    args = parser.parse_args()

    if args.mode == "config":
        from dataclasses import asdict
        CONFIG_PATH.write_text(json.dumps(asdict(AppConfig()), ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"[配置] 已生成: {CONFIG_PATH}")
        return

    if args.mode == "upgrade-config":
        changed, msg = upgrade_config_file(Path(args.config))
        print(f"[配置升级] {'已改动' if changed else '无需改动'}: {msg}")
        return

    if args.mode in ("backup", "backups", "verify", "restore"):
        from . import backup as bk
        if args.mode == "backup":
            d = bk.create_backup(load_config(Path(args.config)), label=args.label)
            print(f"[备份完成] {d}")
        elif args.mode == "backups":
            items = bk.list_backups()
            if not items:
                print("还没有任何备份。先跑 python main.py backup")
            else:
                print(f"{'备份名':<30} {'时间':<21} {'消息数'}")
                for it in items:
                    print(f"{it['name']:<30} {it['created']:<21} {it['messages']}")
        elif args.mode == "verify":
            d = bk.resolve_backup(args.from_backup)
            ok, problems = bk.verify_backup(d)
            print(f"[校验] {d.name}: {'通过' if ok else '失败'}")
            for p in problems:
                print(f"  - {p}")
        elif args.mode == "restore":
            ok, msg = bk.restore_backup(args.from_backup)
            print(f"[恢复] {'成功' if ok else '失败'}\n{msg}")
        return

    cfg = load_config(Path(args.config))
    app = App(cfg)
    if args.mode == "local":
        app.run_local(args.sample)
    elif args.mode == "listen":
        app.run_listen()
    elif args.mode == "daemon":
        app.run_daemon()
    elif args.mode == "stats":
        app.show_stats()
    elif args.mode == "summary":
        app.send_summary()
    elif args.mode == "query":
        app.show_query(args.group, args.date, args.keyword, args.category, args.priority)
    elif args.mode == "cleanup":
        if args.dry_run:
            n = app.store.cleanup(days=cfg.retention.normal_days, dry_run=True)
            print(f"[清理预演] 将删除 {n} 条普通消息")
        else:
            app.cleanup()
