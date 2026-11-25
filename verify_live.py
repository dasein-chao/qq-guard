"""真人接入验证（安全模式）：连协议端，收一条真实群消息，验证整条链路。

用法（协议端已扫码登录、正在运行时）：
    python verify_live.py

安全设计（测试阶段默认不泄露任何真实内容）：
- 关闭所有外部推送（ntfy / 邮件 / Webhook）和桌面弹窗
- 消息正文不打印，只显示长度和脱敏预览（前4字 + ***）
- 日志不落盘
- 只在控制台显示分级结果，验证"链路活着"足够

要测试真实推送，单独跑：python main.py summary（那才会推 ntfy）
"""
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


def redact(text: str) -> str:
    """完全脱敏：只报长度，一个字都不显示。"""
    if not text:
        return "(空)"
    return f"[已隐藏，共{len(text)}字]"


def main():
    print("=== 真人接入验证（安全模式）===")
    print("真实群消息只用于验证链路，不推送、不落盘、不显示正文。\n")

    if not port_open():
        print("[失败] 127.0.0.1:3001 没反应。协议端没启动或没登录成功。")
        print("       先按 协议端启动说明.md 启动协议端并扫码。")
        return 1
    print("[1/4] 协议端端口活着 ✓")

    cfg = load_config()
    token_file = Path(cfg.source.access_token_file)
    if not token_file.exists():
        print(f"[失败] 找不到 token 文件 {token_file}")
        return 1
    print("[2/4] 访问令牌存在 ✓")

    # 安全：不用 App.handle（那条路会入库），直接用分类器。
    # 真实消息只在内存里过一遍分级，不外发、不落盘、不弹窗。
    from qq_guard.engine import Classifier

    classifier = Classifier(cfg.rules)
    received = []

    def on_msg(msg):
        verdict = classifier.classify(msg)
        received.append((msg, verdict))

    print("[3/4] 安全模式：不外发、不入库、不显示正文 ✓")

    src = OneBotWebSocketSource(
        cfg.source.websocket_url,
        token_file=cfg.source.access_token_file,
    )

    import threading

    def listen():
        try:
            src.run(on_msg)
        except Exception as e:
            print(f"[监听异常] {type(e).__name__}: {e}")

    threading.Thread(target=listen, daemon=True).start()

    print("\n等待真实群消息（最多 60 秒）...")
    print("去任意监控群发一句话，或等别人发一条。私聊不算。\n")

    deadline = time.time() + 60
    while time.time() < deadline and not received:
        time.sleep(0.5)

    if not received:
        print("[失败] 60 秒内没收到任何群消息。")
        print("       原因可能：协议端没登录 / 群消息没进来 / 发的是私聊（不监控）。")
        return 1

    print("[4/4] 收到真实消息 ✓\n")
    for msg, verdict in received[:3]:
        source = msg.group_name or msg.group_id or "(未知群)"
        print(f"  来源群  : {redact(source)}")
        print(f"  发送者  : {redact(msg.sender_name)}")
        print(f"  内容预览: {redact(msg.content)}")
        print(f"  分级    : {verdict.level} {verdict.label} (分数{verdict.score})")
        print(f"  判定理由: {verdict.reason[:60]}")
        print(f"  是否触发提醒: {'是' if verdict.needs_notify else '否'}")
        print()

    print("=== 结论 ===")
    print("真实消息已流入系统并完成分级，整条链路活着。")
    print("本次没有推送任何内容到外部，消息只存在本机数据库。")
    print("\n下一步（你自己决定是否开启推送）：")
    print("  1. 在 config.json 填自己的 ntfy topic")
    print("  2. 跑 python main.py summary 测试日报推送")
    print("  3. 确认手机 ntfy App 收到后再开 daemon 常驻")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
