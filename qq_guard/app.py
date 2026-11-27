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
from .sources_ntf import NotificationSource, list_notifications, parse_notification
from .store import Store


class App:
    def __init__(self, cfg: AppConfig, ai_client=None):
        self.cfg = cfg
        # 判定方式：rules 不出网；ai / hybrid 需要 Key，没有就自动退回规则
        self.judge = None
        if ai_client is None and cfg.judge.mode != "rules":
            from .ai_client import LlmJudge
            j = cfg.judge
            candidate = LlmJudge(
                base_url=j.base_url, model=j.model, key_file=j.key_file,
                timeout=j.timeout, daily_call_cap=j.daily_call_cap,
                redact_enabled=j.redact_enabled,
            )
            if candidate.available:
                ai_client = candidate
                self.judge = candidate
            else:
                print(f"[AI] 判定模式为 {j.mode}，但没找到 Key 文件 {j.key_file}，"
                      f"本次退回纯规则（不影响使用，只是判得粗一点）")
        elif ai_client is not None:
            self.judge = ai_client
        self.classifier = Classifier(
            cfg.rules, ai_client=ai_client, ai_mode=cfg.judge.mode,
            vague_lo=cfg.judge.vague_score_min, vague_hi=cfg.judge.vague_score_max,
        )
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
        cover = self.store.coverage(24)
        line = self._coverage_line(cover)
        if not items:
            print("[摘要] 今日没有需要汇总的重要信息。")
            print(f"[覆盖] {line}")
            return
        # AI 复核：重排 + 捞回被规则丢掉的
        if self.judge and self.judge.available and self.cfg.judge.digest_rerank:
            try:
                items, rescued = self._ai_rerank(items)
                if rescued:
                    print(f"[AI] 从被丢弃的消息里捞回 {rescued} 条")
            except Exception as e:
                print(f"[AI] 复核失败，按规则结果发送：{type(e).__name__}")
        title = f"QQ消息摘要 {time.strftime('%m-%d %H:%M')}"
        ok = self.notifier.send_digest(items, title, coverage=line)
        if ok:
            self.store.mark_digested([m.message_id for m, _ in items])
            print(f"[摘要] 已发送 {len(items)} 条")
        print(f"[覆盖] {line}")

    def _ai_rerank(self, items):
        """日报前让 AI 复核：重排优先级 + 捞回被规则判成 P3 的重要消息。"""
        from .models import LEVEL_LABEL
        batch: list[dict] = []
        by_id = {}
        for msg, verdict in items:
            by_id[msg.message_id] = (msg, verdict)
            batch.append({
                "id": msg.message_id, "group": msg.group_name or msg.group_id,
                "sender": msg.sender_name,
                "time": time.strftime("%m-%d %H:%M", time.localtime(msg.timestamp or time.time())),
                "text": msg.content,
            })
        # 捞回候选：规则判 P3 但可能被漏掉的
        rescue_items = self.store.pending_rescue(hours=24, limit=30)
        rescue_ids = set()
        for msg, verdict in rescue_items:
            by_id[msg.message_id] = (msg, verdict)
            rescue_ids.add(msg.message_id)
            batch.append({
                "id": msg.message_id, "group": msg.group_name or msg.group_id,
                "sender": msg.sender_name,
                "time": time.strftime("%m-%d %H:%M", time.localtime(msg.timestamp or time.time())),
                "text": msg.content,
            })
        result = self.judge.judge_batch(batch)
        if not result:
            return items, 0

        rescued = 0
        merged: list[tuple] = []
        for mid, (msg, verdict) in by_id.items():
            row = result.get(mid)
            if row:
                lv = str(row.get("level", "")).upper()
                if lv in ("P0", "P1", "P2", "P3"):
                    was_rescue = mid in rescue_ids
                    if was_rescue and lv in ("P0", "P1"):
                        rescued += 1
                    elif was_rescue:
                        continue          # AI 也认为不重要，继续丢弃
                    verdict.level = lv
                    r = str(row.get("reason", "") or "")[:60]
                    if r:
                        verdict.reason = f"{verdict.reason} | AI复核:{r}".strip(" |")
                    for k, attr in (("category", "category"), ("title", "title"),
                                    ("deadline", "deadline"), ("action", "action_required")):
                        v = str(row.get(k, "") or "").strip()
                        if v and v not in ("待核实", "无", "-"):
                            setattr(verdict.info, attr, v[:120])
            elif mid in rescue_ids:
                continue              # AI 没覆盖到，保守不放进来
            merged.append((msg, verdict))
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        merged.sort(key=lambda x: (order.get(x[1].level, 9), x[0].timestamp))
        return merged, rescued

    def ai_status(self) -> None:
        j = self.cfg.judge
        print("=== AI 判官状态 ===")
        print(f"判定模式：{j.mode}（rules=只用规则 / ai=每条问AI / hybrid=拿不准才问）")
        if j.mode == "rules":
            print("当前不出网，纯规则判定。")
            return
        if not self.judge:
            print(f"未配置 Key（{j.key_file} 不存在或为空）→ 当前实际用纯规则。")
            print(f"配置方法：把 Key 存成项目根目录的 {j.key_file}，一行，不要有空格。")
            return
        s = self.judge.status()
        print(f"服务商：{j.provider or '自定义'}  {s['base_url']}")
        print(f"模型：{s['model']}")
        print(f"Key：{s['key_hint']}  今日已用 {s['calls_today']}/{s['daily_cap']} 次")
        print(f"出网脱敏：{'开（QQ号/手机号/身份证变占位符）' if s['redact'] else '关'}")

    def ai_selftest(self) -> None:
        """用一条"规则必漏"的消息实测 AI 是否在线、判得对不对。"""
        if not self.judge or not self.judge.available:
            print(f"[自检] 没有可用 Key。把 Key 存成项目根目录的 {self.cfg.judge.key_file} "
                  f"（一行，别加空格引号），再跑一次本命令即可实测。")
            return
        cases = [
            ("辅导员的语气", "各位注意，答辩安排有调整，原定明早的场次整体提前到今晚八点，"
                         "没到的按弃权处理"),
            ("水群", "哈哈哈哈这个梗我笑了十分钟"),
            ("软性通知（规则容易漏）", "有想申报明年大创的同学这周内找我登记一下，"
                              "过了这周就不再补了"),
        ]
        print("=== AI 实测（3 条，含 1 条规则容易漏的软性通知）===")
        items = [{"id": f"t{i}", "group": "测试群", "sender": "测试", "text": t}
                 for i, (_, t) in enumerate(cases)]
        res = self.judge.judge_batch(items)
        if not res:
            print("调用失败，看 llm_usage.jsonl 里的 error 字段。")
            return
        for i, (label, text) in enumerate(cases):
            r = res.get(f"t{i}", {})
            print(f"[{r.get('level', '?')}] {label}：{r.get('title', '')}"
                  f" ｜期限={r.get('deadline', '')} ｜{r.get('reason', '')}")
        print("自检完成。若第 3 条被判 P1/P0，说明 AI 补上了规则的短板。")

    @staticmethod
    def _ago(hours: float) -> str:
        if hours < 0:
            return "从未收到"
        if hours < 1:
            return f"{int(hours * 60)} 分钟前"
        return f"{hours:.1f} 小时前"

    @classmethod
    def _coverage_line(cls, cover: dict) -> str:
        """如实报告"看见了什么、可能漏了什么"，不假装一条不漏。"""
        s = (f"本次覆盖：近{cover['hours']}小时看到 {cover['total']} 条消息，"
             f"来自 {cover['groups']} 个群；最后一条在{cls._ago(cover['silent_hours'])}。")
        if cover["silent_hours"] >= 8:
            s += ("\n⚠️ 超过 8 小时没收到任何通知：可能手机没电/后台被杀，"
                  "也可能群里没人说话。重要群请手动翻一下，别全靠这套系统。")
        else:
            s += ("\n（本系统读的是 QQ 通知栏，看不到被设为「免打扰」的群消息——"
                  "这是已知天花板，不瞒你。）")
        return s

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

    def run_notification(self) -> None:
        """手机通知栏模式：免 root、不碰 QQ 协议，封号风险为零。"""
        print("=== 通知监听模式（手机本机运行）===")
        s = self.cfg.source
        NotificationSource(
            poll_seconds=s.notification_poll_seconds,
            group_hints=s.group_channel_hints,
            watch_groups=s.watch_groups,
            assume_group_when_unknown=s.assume_group_when_unknown,
        ).run(self.handle)

    def probe_notifications(self, seconds: int = 90) -> None:
        """探针：把 QQ 通知的真实字段抓出来，用实际数据校准解析规则。

        只打印结构摘要（正文截断 60 字），并把结构化记录写到 probe_log，
        目的是回答三个问题：
          1) channelId 能否区分群消息和好友消息
          2) 正文在 bigText 里还是 content 里，有没有被截断
          3) 标题里群名长什么样（有没有"3条新消息"这类后缀）
        """
        s = self.cfg.source
        print(f"=== 通知探针：接下来 {seconds} 秒抓取 QQ 通知 ===")
        print("请在手机上照常收发几条 QQ 群消息（别发私聊，不需要）...\n")
        seen: dict[str, dict] = {}
        deadline = time.time() + seconds
        while time.time() < deadline:
            for ntf in list_notifications():
                if str(ntf.get("packageName") or "") != "com.tencent.mobileqq":
                    continue
                key = str(ntf.get("id") or ntf.get("key") or "")
                seen[key] = ntf
            time.sleep(2)

        if not seen:
            print("没抓到任何 QQ 通知。逐项检查：")
            print("  1) 系统设置 → 通知与状态栏 → 通知使用权 → 允许 Termux:API")
            print("  2) QQ 里至少有一个群开着『接收消息并提醒』")
            print("  3) 把 QQ 通知渠道的『静音』关掉（静音渠道也可能不产生通知记录）")
            print("  4) 关掉系统『超级省电』模式（会让 termux-api 崩溃）")
            print("  5) 确认没在 proot 里跑（proot 里 termux-api 必然失效）")
            return

        lines: list[str] = []
        verdicts: list[str] = []
        for k, ntf in seen.items():
            title = str(ntf.get("title") or "")
            big = str(ntf.get("bigText") or "")
            content = str(ntf.get("content") or "")
            ch = str(ntf.get("channelId") or "")
            scope = parse_notification(
                ntf, s.group_channel_hints, [], True
            )
            parsed = "群消息" if scope else "无法判定"
            lines.append(
                f"[{parsed}] channelId={ch or '(空)'}\n"
                f"    title   = {title[:60]}\n"
                f"    bigText = {'有' if big else '无'}"
                f"{'（比content长）' if len(big) > len(content) else ''}\n"
                f"    content = {content[:60] or '(空)'}\n"
                f"    正文长度 = {len(big or content)}"
            )
            verdicts.append(ch)
        report = "\n".join(lines)
        print("\n" + report + "\n")
        print("=" * 56)
        non_empty = [c for c in verdicts if c]
        if non_empty and len(set(non_empty)) > 1:
            print("结论①：channelId 有多个值 → 可以靠它区分群消息/好友消息（最理想）")
        elif non_empty:
            print("结论②：channelId 只有一个值 → 区分不了，需要用群名白名单兜底")
        else:
            print("结论③：channelId 为空 → 只能靠群名白名单，建议先填 watch_groups")
        if any(len(str(n.get('bigText') or '')) > len(str(n.get('content') or '')) for n in seen.values()):
            print("结论④：bigText 比 content 长 → 正文取 bigText，截断风险小")
        else:
            print("结论⑤：bigText 不比 content 长 → 正文可能被 QQ 截断，P0 提醒里要提示回原群看")
        print("=" * 56)
        print("以上这段结论（不用发全文）就是校准解析规则需要的全部信息。")

        out = Path(s.probe_log)
        if not out.is_absolute():
            out = ROOT / out
        with out.open("a", encoding="utf-8") as f:
            for ntf in seen.values():
                slim = {k: ntf.get(k) for k in
                        ("id", "key", "group", "title", "content", "bigText",
                         "channelId", "packageName", "postTime")}
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")
        print(f"[探针记录] 已追加到 {out.name}")

    def run_daemon(self) -> None:
        import threading

        def loop():
            while True:
                self._maybe_summary()
                self._maybe_cleanup()
                time.sleep(60)

        threading.Thread(target=loop, daemon=True).start()
        if getattr(self.cfg.source, "mode", "onebot") == "notification":
            self.run_notification()
        else:
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
                        choices=["local", "listen", "ntf", "probe", "daemon", "stats",
                                  "summary", "config", "backup", "backups", "verify",
                                  "restore", "query", "cleanup", "upgrade-config", "judge"],
                        help="local=本地验证, listen=协议端监听, ntf=手机通知监听, "
                             "probe=抓取通知真实格式, daemon=监听+摘要+清理, "
                             "stats=统计, summary=立即发摘要, config=生成默认配置, "
                             "judge=AI判官状态与实测, "
                             "backup/backups/verify/restore=备份恢复, query=历史查询, "
                             "cleanup=清理普通消息, upgrade-config=补齐配置新字段")
    parser.add_argument("--sample", default=str(ROOT / "sample_messages.json"))
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--seconds", type=int, default=90, help="probe: 抓取时长（秒）")
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
    elif args.mode == "ntf":
        app.run_notification()
    elif args.mode == "probe":
        app.probe_notifications(args.seconds)
    elif args.mode == "daemon":
        app.run_daemon()
    elif args.mode == "stats":
        app.show_stats()
    elif args.mode == "summary":
        app.send_summary()
    elif args.mode == "judge":
        app.ai_status()
        print()
        app.ai_selftest()
    elif args.mode == "query":
        app.show_query(args.group, args.date, args.keyword, args.category, args.priority)
    elif args.mode == "cleanup":
        if args.dry_run:
            n = app.store.cleanup(days=cfg.retention.normal_days, dry_run=True)
            print(f"[清理预演] 将删除 {n} 条普通消息")
        else:
            app.cleanup()
