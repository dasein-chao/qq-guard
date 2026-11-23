"""ntfy 通道测试：本地假服务器接收推送，验证分级优先级头。不依赖外网、不推真机。"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")
from qq_guard.config import load_config, NotifyConfig, RuleConfig
from qq_guard.notifiers import Notifier
from qq_guard.models import Message, ExtractedInfo, Verdict

received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        payload = json.loads(body)
        received.append({
            "path": self.path,
            "title": payload.get("title"),
            "priority": payload.get("priority"),
            "tags": (payload.get("tags") or [""])[0],
            "body": payload.get("message", ""),
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *args):
        pass


def verdict(level):
    return Verdict(
        level=level,
        score=90,
        reason="测试",
        info=ExtractedInfo(
            title="测试事项", category="正式通知", importance="高",
            action_required="立即确认", deadline="今晚23:59",
            confidence="高", needs_action="是",
        ),
    )


def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    cfg = NotifyConfig()
    cfg.ntfy_enabled = True
    cfg.ntfy_server = f"http://127.0.0.1:{port}"
    cfg.ntfy_topic = "test-topic-xyz"
    cfg.desktop_enabled = False
    cfg.email_enabled = False
    cfg.webhook_enabled = False
    n = Notifier(cfg, RuleConfig())
    msg_p0 = Message("m1", "u1", "老师", "明天必须交材料", "g1", "课程群")
    msg_p1 = Message("m2", "u2", "学长", "课题组招人", "g2", "科研群")
    msg_dig = Message("m3", "u3", "同学", "资料分享", "g3", "学习群")

    n.notify_urgent(msg_p0, verdict("P0"))
    n.notify_urgent(msg_p1, verdict("P1"))
    n.send_digest([(msg_dig, verdict("P1"))], title="QQ消息摘要")

    srv.shutdown()

    ok = True
    expect = [("P0", "max", "rotating_light"),
              ("P1", "high", "warning"),
              ("digest", "default", "newspaper")]
    if len(received) != 3:
        print(f"FAIL 应收到3条，实际{len(received)}")
        ok = False
    for i, (name, pri, tag) in enumerate(expect):
        if i >= len(received):
            break
        r = received[i]
        good = r["priority"] == pri and r["tags"] == tag and r["path"] == "/test-topic-xyz"
        print(f"{'PASS' if good else 'FAIL'} {name}: priority={r['priority']} tags={r['tags']} path={r['path']}")
        ok = ok and good
    # 验证正文含原始消息与消息ID
    if received and "明天必须交材料" in received[0]["body"] and "m1" in received[0]["body"]:
        print("PASS 正文保留原始消息与消息ID")
    else:
        print("FAIL 正文缺失原文或消息ID")
        ok = False
    print("总计:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
