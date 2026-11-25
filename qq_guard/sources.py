"""消息入口：OneBot v11 WebSocket 与本地样例文件。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .models import Message

MsgCallback = Callable[[Message], None]


def parse_onebot_event(data: dict) -> Message | None:
    """把 OneBot v11 消息事件转成统一 Message。非消息事件返回 None。"""
    if data.get("post_type") != "message":
        return None
    sender = data.get("sender") or {}
    content = str(data.get("raw_message") or data.get("message") or "")
    # 简化 CQ 码，保留可读性
    import re
    content = re.sub(r"\[CQ:at,qq=(\d+)[^\]]*\]", r"@\1", content)
    content = re.sub(r"\[CQ:image[^\]]*\]", "[图片]", content)
    content = re.sub(r"\[CQ:face[^\]]*\]", "[表情]", content)
    content = re.sub(r"\[CQ:([a-zA-Z]+)[^\]]*\]", r"[\1]", content)
    return Message(
        message_id=str(data.get("message_id", f"{int(time.time()*1000)}")),
        sender_id=str(sender.get("user_id", data.get("user_id", ""))),
        sender_name=str(sender.get("nickname") or sender.get("card") or data.get("user_id", "")),
        content=content,
        group_id=str(data.get("group_id", "")),
        group_name=str(
            sender.get("group_name")
            or data.get("group_name")
            or sender.get("card")
            or data.get("group_id", "")
        ),
        timestamp=int(data.get("time", time.time())),
        is_private=data.get("message_type") == "private",
        raw=data,
    )


class LocalSource:
    """从 JSON 文件读取样例消息，用于本地验证。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def run(self, callback: MsgCallback) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("messages", [])
        for item in items:
            msg = Message(
                message_id=str(item.get("id", str(int(time.time() * 1000)))),
                sender_id=str(item.get("sender_id", "")),
                sender_name=str(item.get("sender_name", "")),
                content=str(item.get("content", "")),
                group_id=str(item.get("group_id", "")),
                group_name=str(item.get("group_name", "")),
                timestamp=int(item.get("timestamp", time.time())),
                is_private=bool(item.get("is_private", False)),
            )
            callback(msg)


class OneBotWebSocketSource:
    """连接 OneBot v11 正向 WebSocket，实时接收消息。

    安全：
    - 只连本地回环地址（配置里协议端也只绑 127.0.0.1）
    - 支持 AccessToken 认证，token 从本地文件读，不写进代码或日志
    稳定：断线指数退避重连，避免疯狂重试触发风控。
    """

    def __init__(self, url: str, token: str = "", reconnect_delay: int = 5,
                 token_file: str = ""):
        self.url = url
        self.token = token
        self.token_file = token_file
        self.reconnect_delay = reconnect_delay

    def _resolve_token(self) -> str:
        if self.token:
            return self.token
        if self.token_file:
            p = Path(self.token_file)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        return ""

    def run(self, callback: MsgCallback) -> None:
        try:
            import websocket
        except ImportError:
            print("[错误] 需要 websocket-client: python -m pip install websocket-client")
            return

        token = self._resolve_token()
        headers = [f"Authorization: Bearer {token}"] if token else []
        if not headers:
            print("[安全警告] 未设置 AccessToken，协议端连接无认证")
        else:
            print(f"[安全] 已加载 AccessToken（{len(token)} 字符，不显示在日志）")

        delay = self.reconnect_delay
        failures = 0
        while True:
            try:
                print(f"[连接] {self.url}")
                ws = websocket.create_connection(
                    self.url, timeout=30, header=headers,
                    suppress_origin=True,
                )
                print("[连接成功] 开始监听群消息")
                failures = 0
                delay = self.reconnect_delay
                while True:
                    raw = ws.recv()
                    if not raw:
                        continue
                    if raw == "ping":
                        ws.send("pong")
                        continue
                    try:
                        data = json.loads(raw)
                        msg = parse_onebot_event(data)
                        if msg:
                            callback(msg)
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                failures += 1
                # 指数退避，上限 60 秒；避免频繁重连被风控盯上
                delay = min(delay * 2, 60)
                print(f"[断线] {type(e).__name__}: {e}；{delay}秒后重连（第{failures}次）")
                time.sleep(delay)
