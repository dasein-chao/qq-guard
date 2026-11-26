"""Android 通知监听入口（原生 Termux，免 root，不接触 QQ 协议）。

原理：
    QQ 客户端自己弹通知 → Android 通知栏 → `termux-notification-list` 读出 → 转成 Message。

为什么这条路封号风险为零：
    全程不登录任何第三方协议、不改 QQ 客户端、不调签名服务，
    只是"读本机通知栏"。对腾讯而言这就是一台正常在用 QQ 的手机。

代价（必须如实知道）：
    1. 只能看到 QQ 愿意弹通知的消息（群若设"接收但不提醒"，这里看不到）
    2. QQ 可能把多条合并成一条，正文也可能被截断（优先取 bigText）
    3. 锁屏/省电模式下通知可能延迟

因此它适合"先跑起来验证筛得准不准"，不适合"7×24 一条不漏"。
后者的正确路线仍是专用旧机 + 协议端，等验证完再换。

环境要求（踩坑记录，来自项目 issue 与实测）：
    - 必须用**原生 Termux**，不能进 proot（proot 里 termux-api 会静默失效）
    - Termux 与 Termux:API 必须同源安装（都 F-Droid 或都 GitHub，签名不同会卡死）
    - 必须给 Termux:API 开"通知使用权"，给 Termux 关电池优化
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .models import Message

MsgCallback = Callable[[Message], None]

QQ_PACKAGE = "com.tencent.mobileqq"

# 通知标题里 QQ 常带的数量后缀："(3条新消息)" "等 3 条" "(3)"
_COUNT_SUFFIX = re.compile(
    r"[\(\（]\s*\d+\s*条?(?:新)?消息?\s*[\)\）]|等\s*\d+\s*条(?:新)?消息?|[\(\（]\d+[\)\）]\s*$"
)
# "昵称：内容" / "昵称: 内容"（中英文冒号都出现）
_SENDER_PREFIX = re.compile(r"^([^\s:：]{1,24})\s*[:：]\s*(.+)$", re.S)


def api_available() -> bool:
    """Termux:API 是否可用（找不到命令就直接告警，不静默失败）。"""
    return shutil.which("termux-notification-list") is not None


def list_notifications(timeout: int = 25) -> list[dict]:
    """读取当前通知栏。失败返回空列表，不抛异常打断主循环。"""
    if not api_available():
        return []
    try:
        proc = subprocess.run(
            ["termux-notification-list"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[通知读取失败] {type(e).__name__}: {e}")
        return []
    out = (proc.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print("[通知读取失败] 返回内容不是 JSON（可能是通知使用权未授予）")
        return []
    return data if isinstance(data, list) else []


def _clean_title(title: str) -> str:
    t = _COUNT_SUFFIX.sub("", title or "").strip()
    return t or (title or "").strip()


def _pick_text(ntf: dict) -> str:
    """正文优先取 bigText（QQ 折叠通知里常是完整内容），其次 content。"""
    for key in ("bigText", "text", "content", "contentView", "subText"):
        val = ntf.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # 部分版本只给 textLines
    lines = ntf.get("textLines")
    if isinstance(lines, list) and lines:
        return "\n".join(str(x) for x in lines if str(x).strip()).strip()
    return ""


def _split_sender(text: str, group_hint: str) -> tuple[str, str]:
    """把 "昵称：内容" 拆成 (发送者, 内容)；拆不开就把整段当内容。"""
    m = _SENDER_PREFIX.match(text)
    if not m:
        return "", text
    name, body = m.group(1).strip(), m.group(2).strip()
    # 群名被塞进正文的情况："群名: 昵称: 内容" —— 再剥一层
    if group_hint and name == group_hint and body:
        m2 = _SENDER_PREFIX.match(body)
        if m2:
            return m2.group(1).strip(), m2.group(2).strip()
    # 太长的不像昵称，判定为正文自带冒号（如 "注意: 明天..."）
    if len(name) > 16:
        return "", text
    return name, body


def classify_scope(ntf: dict, group_hints: list[str]) -> str:
    """判断这条通知属于 群/私聊/未知。未知交回上层按保守策略处理。

    优先用 channelId（QQ 会为群消息和好友消息用不同通知渠道），
    再看标题里是否出现"群"等特征词。
    """
    ch = str(ntf.get("channelId") or ntf.get("channel") or "").lower()
    if ch:
        for hint in group_hints:
            if hint.lower() in ch:
                return "group"
        # 明确是好友/私聊渠道
        for hint in ("friend", "private", "c2c", "好友", "私聊"):
            if hint.lower() in ch:
                return "private"
    title = _clean_title(str(ntf.get("title") or ""))
    for hint in group_hints:
        if hint and hint in title:
            return "group"
    return "unknown"


def parse_notification(
    ntf: dict,
    group_hints: list[str] | None = None,
    watch_groups: list[str] | None = None,
    assume_group_when_unknown: bool = False,
) -> Message | None:
    """单条通知 → Message。不满足条件返回 None。

    安全取向：无法确认是群消息时默认丢弃（私聊一律不监控是铁律），
    只有显式打开 assume_group_when_unknown 才按群处理。
    """
    group_hints = group_hints or ["group", "群"]
    pkg = str(ntf.get("packageName") or "")
    if pkg and pkg != QQ_PACKAGE:
        return None

    title = _clean_title(str(ntf.get("title") or ""))
    text = _pick_text(ntf)
    if not text and not title:
        return None

    scope = classify_scope(ntf, group_hints)
    if scope == "private":
        return None
    if scope == "unknown" and not assume_group_when_unknown:
        return None

    sender, content = _split_sender(text, title)
    if not content:
        content = text

    # 群名：标题即群名；若标题不像群名再看配置白名单命中
    group_name = title
    if scope == "unknown" and watch_groups:
        hit = next((g for g in watch_groups if g and g in text), "")
        if hit:
            group_name = hit
        elif not assume_group_when_unknown:
            return None

    if watch_groups and group_name not in watch_groups:
        return None

    post_time = ntf.get("postTime") or ntf.get("when") or 0
    try:
        ts = int(post_time)
        if ts > 10**12:      # 毫秒
            ts //= 1000
    except (TypeError, ValueError):
        ts = int(time.time())
    if not ts:
        ts = int(time.time())

    return Message(
        message_id=str(ntf.get("id") or ntf.get("key") or f"ntf{ts}"),
        sender_id=str(ntf.get("sender") or ""),
        sender_name=sender or "未知发送者",
        content=content,
        group_id=str(ntf.get("group") or ""),
        group_name=group_name,
        timestamp=ts,
        is_private=False,
        raw={
            "source": "notification",
            "packageName": pkg,
            "channelId": ntf.get("channelId"),
            "title": ntf.get("title"),
            "bigText": ntf.get("bigText"),
            "content": ntf.get("content"),
            "postTime": ntf.get("postTime"),
        },
    )


class NotificationSource:
    """轮询通知栏。同一通知 id + 相同正文不重复投递。"""

    def __init__(self, poll_seconds: int = 3, group_hints: list[str] | None = None,
                 watch_groups: list[str] | None = None,
                 assume_group_when_unknown: bool = False):
        self.poll_seconds = max(1, poll_seconds)
        self.group_hints = group_hints or ["group", "群"]
        self.watch_groups = watch_groups or []
        self.assume_group_when_unknown = assume_group_when_unknown
        # (通知id) -> 上次正文，用于判断"这条通知有没有变成新内容"
        self._seen: dict[str, str] = {}

    def run(self, callback: MsgCallback) -> None:
        if not api_available():
            print("[致命] 找不到 termux-notification-list。")
            print("       请在 Termux 里执行: pkg install termux-api")
            print("       并安装 Termux:API App（必须与 Termux 同源），")
            print("       在系统设置里授予它『通知使用权』。")
            return
        print(f"[就绪] 每 {self.poll_seconds} 秒读一次通知栏；"
              f"只看 {QQ_PACKAGE}；未知归属默认{'当群处理' if self.assume_group_when_unknown else '丢弃'}")
        while True:
            for ntf in list_notifications():
                msg = parse_notification(
                    ntf, self.group_hints, self.watch_groups,
                    self.assume_group_when_unknown,
                )
                if not msg:
                    continue
                key = f"{msg.message_id}|{msg.group_name}"
                fingerprint = msg.content[:200]
                if self._seen.get(key) == fingerprint:
                    continue          # 同一条通知没有新内容
                self._seen[key] = fingerprint
                callback(msg)
            # 防内存膨胀
            if len(self._seen) > 500:
                self._seen = dict(list(self._seen.items())[-200:])
            time.sleep(self.poll_seconds)
