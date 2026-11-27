"""AI 判官测试：起一个假的 OpenAI 兼容服务器，验证真实 HTTP 路径。

不需要真 Key、不花钱、不出网（只连 127.0.0.1）。
覆盖：请求格式、JSON 解析容错、错误处理、配额、脱敏、与规则引擎的配合。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from qq_guard.ai_client import LlmJudge, redact
from qq_guard.config import RuleConfig
from qq_guard.engine import Classifier
from qq_guard.models import Message

ok_all = True
_received: list[dict] = []
_reply = {"mode": "good", "text": '{"level":"P0","category":"通知","title":"答辩提前",'
                                  '"deadline":"今晚八点","action":"准时到场","reason":"期限在今晚",'
                                  '"confidence":"高"}'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        _received.append({
            "auth": self.headers.get("Authorization", ""),
            "path": self.path,
            "payload": payload,
        })
        mode = _reply["mode"]
        if mode == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid api key"}')
            return
        if mode == "429":
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limited"}')
            return
        text = _reply["text"]
        if mode == "fenced":
            text = "```json\n" + text + "\n```"
        if mode == "garbage":
            text = "抱歉，我无法判断。"
        resp = {"choices": [{"message": {"content": text}}],
                "usage": {"total_tokens": 123}}
        raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check(name, got, want):
    global ok_all
    good = got == want
    ok_all &= good
    print(("  PASS " if good else "  FAIL ") + name + ("" if good else f"  期望={want!r} 实际={got!r}"))
    return good


def mkjudge(port, cap=200, redact_on=True, tmpkey=".test_llm_key"):
    return LlmJudge(base_url=f"http://127.0.0.1:{port}/v1", model="test-model",
                    key_file=tmpkey, api_key="sk-test-1234567890",
                    timeout=5, daily_call_cap=cap, redact_enabled=redact_on,
                    usage_path=".test_llm_usage.jsonl")


def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = [{"role": "user", "content": "答辩提前到今晚八点"}]

    print("== 1. 正常调用 ==")
    j = mkjudge(port)
    check("有 key 时 available", j.available, True)
    r = j(msgs)
    check("返回 level", (r or {}).get("level"), "P0")
    check("请求路径正确", _received[-1]["path"], "/v1/chat/completions")
    check("带 Bearer 头", _received[-1]["auth"].startswith("Bearer sk-"), True)
    check("model 透传", _received[-1]["payload"].get("model"), "test-model")

    print("== 2. 解析容错 ==")
    _reply["mode"] = "fenced"
    check("```json 包裹能解析", (j(msgs) or {}).get("level"), "P0")
    _reply["mode"] = "garbage"
    check("非 JSON 返回 None", j(msgs), None)
    _reply["mode"] = "good"

    print("== 3. 错误处理（不能让程序崩） ==")
    _reply["mode"] = "401"
    check("401 返回 None", j(msgs), None)
    _reply["mode"] = "429"
    check("429 返回 None", j(msgs), None)
    _reply["mode"] = "good"
    bad = LlmJudge(base_url="http://127.0.0.1:9/v1", model="x", api_key="k",
                   timeout=2, usage_path=".test_llm_usage.jsonl")
    check("端口不通返回 None", bad(msgs), None)

    print("== 4. 无 Key ==")
    nokey = LlmJudge(base_url=f"http://127.0.0.1:{port}/v1", model="x",
                     key_file=".no_such_key_file", api_key="",
                     usage_path=".test_llm_usage.jsonl")
    check("无 key 时 available=False", nokey.available, False)
    check("无 key 不请求直接返回 None", nokey(msgs), None)

    print("== 5. 脱敏（出网前把 QQ号/手机号替换掉） ==")
    check("QQ号", redact("联系我 123456789 报名"), "联系我 [号码] 报名")
    check("手机号", redact("打 13812345678"), "打 [手机号]")
    check("身份证", redact("身份证 110101199001011234"), "身份证 [身份证]")
    check("普通数字不动", redact("今晚8点开会"), "今晚8点开会")
    jr = mkjudge(port)
    jr.judge_batch([{"id": "a1", "group": "某群", "sender": "老师",
                     "text": "加我 987654321 私聊"}])
    sent = _received[-1]["payload"]["messages"][-1]["content"]
    check("批量请求里已脱敏", "987654321" not in sent, True)
    check("批量请求里保留群名", "某群" in sent, True)

    print("== 6. 批量判定 ==")
    _reply["text"] = ('{"items":[{"id":"a1","level":"P1","title":"登记","reason":"软性期限"},'
                      '{"id":"a2","level":"P3","title":"闲聊","reason":"无信息"}]}')
    res = jr.judge_batch([{"id": "a1", "group": "g", "sender": "s", "text": "这周内找我登记"},
                          {"id": "a2", "group": "g", "sender": "s", "text": "哈哈哈"}])
    check("批量条数", len(res), 2)
    check("批量级别 a1", res.get("a1", {}).get("level"), "P1")

    print("== 7. 每日配额上限 ==")
    capped = mkjudge(port, cap=1)
    capped.usage_path = ".test_cap_usage.jsonl"
    import os
    try:
        os.remove(".test_cap_usage.jsonl")
    except OSError:
        pass
    first = capped(msgs)
    second = capped(msgs)
    check("第1次成功", first is not None, True)
    check("第2次被配额拦住", second, None)
    check("配额剩余归零", capped.quota_left(), 0)

    print("== 8. 和规则引擎配合 ==")
    rules = RuleConfig()

    class Spy:
        def __init__(self, level="P1"):
            self.calls = 0
            self.level = level

        def __call__(self, messages):
            self.calls += 1
            return {"level": self.level, "reason": "测试", "title": "t"}

    def m(content, is_private=False):
        return Message(message_id="m1", sender_id="1", sender_name="同学",
                       content=content, group_id="g", group_name="测试群",
                       timestamp=1764200000, is_private=is_private)

    spy = Spy()
    c = Classifier(rules, ai_client=spy, ai_mode="rules")
    c.classify(m("这周内找我登记一下"))
    check("rules 模式从不调 AI", spy.calls, 0)

    spy2 = Spy("P0")
    c2 = Classifier(rules, ai_client=spy2, ai_mode="hybrid")
    v = c2.classify(m("有想申报明年大创的同学这周内找我登记一下，过了这周就不再补了"))
    check("hybrid 对灰区调用了 AI", spy2.calls >= 1, True)
    check("hybrid 灰区按 AI 升级到 P0", v.level, "P0")

    spy2b = Spy("P1")
    c2b = Classifier(rules, ai_client=spy2b, ai_mode="hybrid")
    v2b = c2b.classify(m("有想申报明年大创的同学这周内找我登记一下"))
    check("rules 原判 P1 时 AI 也是 P1 则不变", v2b.level, "P1")

    spy_p0 = Spy("P3")
    c_p0 = Classifier(rules, ai_client=spy_p0, ai_mode="hybrid")
    v_p0 = c_p0.classify(m("明天下午有个讲座名额有限，先到先得"))
    check("规则判 P0 的不问 AI（不等网络）", spy_p0.calls, 0)

    spy3 = Spy("P3")
    c3 = Classifier(rules, ai_client=spy3, ai_mode="hybrid")
    v3 = c3.classify(m("辅导员通知：今晚10点前必须把确认表交到我这里"))
    check("规则判 P0 时 AI 不能压下去", v3.level, "P0")

    spy4 = Spy("P0")
    c4 = Classifier(rules, ai_client=spy4, ai_mode="hybrid")
    c4.classify(m("好友私聊：明天见", is_private=True))
    check("私聊永不问 AI", spy4.calls, 0)

    spy5 = Spy("P1")
    c5 = Classifier(rules, ai_client=spy5, ai_mode="ai")
    c5.classify(m("哈哈哈"))
    check("ai 模式每条都问", spy5.calls, 1)

    print()
    print("全部通过" if ok_all else "存在失败项")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
