"""真人接入验证：连协议端，收一条真实群消息，验证整条链路。

用法（协议端已扫码登录、正在运行时）：
    python verify_live.py

它会：
1. 检查 3001 端口是否活着
2. 连上协议端（带 token 认证）
3. 等 60 秒，抓第一条真实群消息
4. 显示分级结果、是否触发提醒
5. 告诉你系统是否真的活着
"""
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from qq_guard.config import load_config
from qq_guard.sources import OneBotWebSocketSource
from qq_guard.app import App


def port_open(host="127.0.0.1", port=3001, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    print("=== 真人接入验证 ===\n")

    if not port_open():
        print("[失败] 127.0.0.1:3001 没反应。协议端没启动或没登录成功。")
        print("       先按 协议端启动说明.md 启动 Lagrange.OneBot.exe 并扫码。")
        return 1
    print("[1/4] 协议端端口活着 ✓")

    cfg = load_config()
    token_file = Path(cfg.source.access_token_file)
    if not token_file.exists():
        print(f"[失败] 找不到 token 文件 {token_file}")
        return 1
    print("[2/4] 访问令牌存在 ✓")

    cfg.notify.desktop_enabled = True
    cfg.notify.ntfy_enabled = True
    cfg.notify.summary_enabled = False
    app = App(cfg)

    received = []

    def on_msg(msg):
        verdict = app.handle(msg)
        received.append((msg, verdict))

    print("[3/4] 连接协议端，等待真实群消息（最多 60 秒）...")
    print("      现在去你的 QQ 群里发一句话，或等别人发一条。\n")

    src = OneBotWebSocketSource(
        cfg.source.websocket_url,
        token_file=cfg.source.access_token_file,
    )

    import threading
    result = {}

    def listen():
        try:
            src.run(on_msg)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=listen, daemon=True)
    t.start()

    deadline = time.time() + 60
    while time.time() < deadline and not received:
        time.sleep(0.5)

    if not received:
        print("[失败] 60 秒内没收到任何群消息。")
        print("       可能原因：协议端没登录 / 没选对群 / 私聊发给它了（私聊不监控）。")
        return 1

    print("[4/4] 收到真实消息 ✓\n")
    for msg, verdict in received[:3]:
        print(f"  来源    : {msg.source_label}")
        print(f"  内容    : {msg.content[:80]}")
        print(f"  分级    : {verdict.level} {verdict.label} (分数{verdict.score})")
        print(f"  原因    : {verdict.reason}")
        print(f"  提醒    : {'是' if verdict.needs_notify else '否'}")
        print(f"  消息ID  : {msg.message_id}")
        print()

    print("=== 结论 ===")
    print("真实消息已流入系统并完成分级。整条链路活着。")
    print("去手机看 ntfy 有没有收到提醒（P0 应该静音也响）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
