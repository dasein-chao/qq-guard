"""提醒通道：邮件、Webhook、桌面弹窗。全部可独立开关。"""
from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from .models import Message, Verdict

if TYPE_CHECKING:
    from .config import NotifyConfig, RuleConfig


class Notifier:
    def __init__(self, notify_cfg: NotifyConfig, rule_cfg: RuleConfig):
        self.cfg = notify_cfg
        self.rule_cfg = rule_cfg
        self._last_notified: dict[str, float] = {}
        self._lock = threading.Lock()

    def _should_notify(self, msg: Message) -> bool:
        key = f"{msg.sender_id}:{msg.content[:80]}"
        now = time.time()
        with self._lock:
            last = self._last_notified.get(key, 0)
            if now - last < self.rule_cfg.dedupe_window_seconds:
                return False
            self._last_notified[key] = now
        return True

    def notify_urgent(self, msg: Message, verdict: Verdict) -> bool:
        if not self._should_notify(msg):
            print(f"[去重] 跳过重复提醒: {msg.source_label}")
            return False
        title = f"[{verdict.level} {verdict.label}] {verdict.info.title[:30]}"
        body = self.format_alert(msg, verdict)
        if self.cfg.desktop_enabled:
            self._desktop(title, body)
        if getattr(self.cfg, "ntfy_enabled", False):
            self._ntfy(title, body, verdict.level)
        if self.cfg.email_enabled:
            self._email(title, body)
        if self.cfg.webhook_enabled:
            self._webhook(title, body)
        return True

    @staticmethod
    def format_alert(msg: Message, verdict: Verdict) -> str:
        """按需求规定的格式输出即时提醒，保留原始消息与消息 ID。"""
        i = verdict.info
        return (
            f"【优先级】{verdict.level} {verdict.label}\n"
            f"【事项标题】{i.title}\n"
            f"【来源群聊及发送者】{msg.group_name or msg.group_id} / {msg.sender_name}\n"
            f"【消息时间】{Notifier._fmt_time(msg.timestamp)}\n"
            f"【重要原因】{i.importance} / {i.category} / {verdict.reason}\n"
            f"【需要采取的行动】{i.action_required}\n"
            f"【截止时间】{i.deadline}\n"
            f"【原始消息】{msg.content}\n"
            f"【消息ID】{msg.message_id}\n"
            f"【可信度】{i.confidence}"
        )

    def send_digest(self, items: list[tuple[Message, Verdict]], title: str = "QQ消息摘要") -> bool:
        body = self.format_digest(items)
        ok = False
        if getattr(self.cfg, "ntfy_enabled", False):
            ok = self._ntfy(title, body, "digest") or ok
        if self.cfg.email_enabled:
            ok = self._email(title, body) or ok
        if self.cfg.webhook_enabled:
            ok = self._webhook(title, body) or ok
        if not (getattr(self.cfg, "ntfy_enabled", False)
                or self.cfg.email_enabled or self.cfg.webhook_enabled):
            print(f"\n{'=' * 60}\n{title}\n{'=' * 60}\n{body}\n")
            ok = True
        return ok

    @staticmethod
    def format_digest(items: list[tuple[Message, Verdict]]) -> str:
        """每日摘要：按需求的五类组织，合并重复事件，保留来源与行动要求。"""
        if not items:
            return "今日没有需要汇总的重要信息。"

        # 五类映射：通知 / 机会 / 课程学习 / 后续关注 / 资料讨论
        sections: list[tuple[str, list[tuple[Message, Verdict]]]] = [
            ("今日重要通知", []),
            ("科研、竞赛与活动机会", []),
            ("课程、考试与学习事务", []),
            ("需要后续关注的事项", []),
            ("值得保留的资料和讨论", []),
        ]
        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        for pair in items:
            m, v = pair
            cat = v.info.category
            if cat in ("正式通知",) or v.level == "P0":
                idx = 0
            elif cat in ("科研机会", "竞赛活动", "活动机会"):
                idx = 1
            elif cat in ("课程学习",):
                idx = 2
            elif v.info.needs_action == "是" and v.level in ("P0", "P1"):
                idx = 3
            else:
                idx = 4
            sections[idx][1].append(pair)

        lines: list[str] = []
        seen: set[str] = set()
        for name, group in sections:
            if not group:
                continue
            group.sort(key=lambda p: (priority_rank.get(p[1].level, 9), -p[0].timestamp))
            lines.append(f"【{name}】")
            for m, v in group:
                # 合并重复事件：标题相同只列一次
                dedup_key = v.info.title[:30]
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                action = v.info.action_required if v.info.action_required != "待核实" else "无"
                deadline = v.info.deadline
                lines.append(
                    f"- [{v.level} {v.label}] {v.info.title}\n"
                    f"  来源：{m.group_name or m.group_id} / {m.sender_name} "
                    f"({Notifier._fmt_time(m.timestamp)})\n"
                    f"  行动：{action} | 截止：{deadline} | 消息ID：{m.message_id}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() if lines else "今日没有需要汇总的重要信息。"

    @staticmethod
    def _fmt_time(ts: int) -> str:
        if not ts:
            return "未知时间"
        return time.strftime("%m-%d %H:%M", time.localtime(ts / 1000 if ts > 1e12 else ts))

    def _desktop(self, title: str, body: str) -> None:
        brief = body.replace("\n", " | ")[:160]
        try:
            import subprocess
            ps = (
                "$ErrorActionPreference='SilentlyContinue';"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.BalloonTipTitle=" + json.dumps(title, ensure_ascii=False) + ";"
                "$n.BalloonTipText=" + json.dumps(brief, ensure_ascii=False) + ";"
                "$n.Visible=$true;$n.ShowBalloonTip(8000);Start-Sleep -s 9;$n.Dispose()"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import winsound
                winsound.Beep(880, 250)
            except Exception:
                pass
        except Exception as e:
            print(f"[桌面提醒失败] {e}")

    def _email(self, title: str, body: str) -> bool:
        cfg = self.cfg
        if not (cfg.smtp_user and cfg.smtp_password and cfg.email_to):
            print("[邮件] 未配置发件邮箱/授权码/收件地址，跳过")
            return False
        msg = MIMEMultipart()
        msg["From"] = cfg.smtp_user
        msg["To"] = cfg.email_to
        msg["Subject"] = Header(title, "utf-8")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        try:
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.sendmail(cfg.smtp_user, [cfg.email_to], msg.as_string())
            print(f"[邮件] 已发送: {title}")
            return True
        except Exception as e:
            print(f"[邮件失败] {e}")
            return False

    def _ntfy(self, title: str, body: str, level: str = "P0") -> bool:
        """ntfy 推送。P0=max（穿透静音持续响）+ urgent 标签，P1=high，其余=default。"""
        cfg = self.cfg
        base = getattr(cfg, "ntfy_server", "https://ntfy.sh").rstrip("/")
        topic = getattr(cfg, "ntfy_topic", "")
        level_topics = getattr(cfg, "ntfy_topic_by_level", {}) or {}
        topic = level_topics.get(level, level_topics.get("default", topic))
        if not topic:
            print("[ntfy] 未配置 topic，跳过")
            return False
        # 占位符未填时友好跳过，别让新用户第一次跑就看到编码错误
        if any("\u4e00" <= ch <= "\u9fff" for ch in topic) or not all(
                ch.isascii() and (ch.isalnum() or ch in "-_") for ch in topic):
            print("[ntfy] topic 还是占位符或含非法字符，未推送。请在 config.json 填自己的随机 topic")
            return False
        # P0 必须穿透静音：max 优先级 + urgent 标签；P1 高；摘要静默
        if level == "P0":
            priority, tags = "max", "rotating_light"
        elif level == "P1":
            priority, tags = "high", "warning"
        elif level == "digest":
            priority, tags = "default", "newspaper"
        else:
            priority, tags = "default", "bell"
        # 中文不能放进 HTTP header（latin-1 编码会崩），用 ntfy 的 JSON 发布格式
        payload = json.dumps({
            "topic": topic,
            "title": title,
            "message": body,
            "priority": priority,
            "tags": [tags],
        }, ensure_ascii=False)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            req = urllib.request.Request(
                f"{base}/{topic}",
                data=payload.encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            print(f"[ntfy] 已推送 ({priority}): {title}")
            return True
        except Exception as e:
            print(f"[ntfy失败] {e}")
            return False

    def _webhook(self, title: str, body: str) -> bool:
        cfg = self.cfg
        if not cfg.webhook_url:
            return False
        t = json.dumps(title, ensure_ascii=False)[1:-1]
        c = json.dumps(body, ensure_ascii=False)[1:-1]
        payload = cfg.webhook_payload_template.replace("{title}", t).replace("{content}", c)
        try:
            req = urllib.request.Request(
                cfg.webhook_url,
                data=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            print(f"[Webhook] 已发送: {title}")
            return True
        except Exception as e:
            print(f"[Webhook失败] {e}")
            return False
