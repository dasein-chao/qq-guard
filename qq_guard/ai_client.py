"""AI 判官：直连 OpenAI 兼容接口，纯标准库（手机 Termux 上也能跑）。

设计原则（对应项目铁律）：
1. 只出网一次「文字」，不上传任何凭据、不碰 QQ、不模拟登录 —— 与封号风险无关。
2. 服务商可替换：base_url + model + key 三项即可，不绑定任何一家。
3. AI 挂了必须降级到规则，绝不因为 AI 不可用就丢消息或不出提醒。
4. key 只从文件/环境变量读，永不写进配置、日志、异常信息、数据库。
5. 默认做脱敏（QQ 号、手机号、身份证号 → 占位符），减少群友隐私外流。

免费方案参考：智谱 GLM-4.7-Flash（OpenAI 兼容，官方免费档）。
base_url = https://open.bigmodel.cn/api/paas/v4   model = glm-4.7-flash
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 常见 AI 服务商的 OpenAI 兼容地址（填 key 即可用，不需要记 URL）
PRESETS: dict[str, tuple[str, str]] = {
    # 名称: (base_url, 默认模型)
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "z-ai/glm-4.7-flash:free"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
}

# 脱敏规则：这些不脱敏发出去等于把群友隐私交出手机
_REDACT_PATTERNS = [
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)[1-9]\d{4,11}(?!\d)"), "[号码]"),   # QQ 号 / 学号
]


def redact(text: str) -> str:
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


SYSTEM_PROMPT = """你是 QQ 群消息分级助手，服务对象是一名中国大学生。只输出 JSON，不要解释性文字。

分级标准：
- P0 紧急：现在不处理就会错过或效果打折。今天/明天到期、名额有限且正在抢、要求立刻确认、突然改时间改地点、临时集合。
- P1 重要：正式通知、科研/竞赛/实习机会、报名、成绩学籍、需本人在期限内处理但不紧急。
- P2 一般：有信息量但不需行动（资料分享、经验讨论、活动预告）。
- P3 低价值：闲聊、表情、广告、拼单、代取快递、已过期的通知。

判断要点：
- 时间是关键信号。"今晚10点前""明天中午12点截止""本周五"都算硬期限，要换算成距现在多久再定级。
- 看得出发送者身份（辅导员/老师/班长/官方号）且内容带强制语气（必须/务必/一律/否则/逾期），倾向升级。
- 别被"紧急！""十万火急"这类词骗；先看有没有具体的、可验证的截止时间或名额。没有具体期限的"紧急"多半是玩笑或广告，给 P2/P3。
- 拿不准时宁可给高不給低，但要在 reason 里写明拿不准的原因。
- 没提到的字段填"待核实"，不要编。

输出格式：
{"level":"P0|P1|P2|P3","category":"通知|科研机会|竞赛活动|课程学习|学习资料|活动机会|闲聊|广告|其他",
 "title":"≤20字概括","deadline":"原文里的期限，无则待核实","action":"要做什么，无则待核实",
 "reason":"一句话说明为什么是这个级别","confidence":"高|中|低"}"""


@dataclass
class LlmJudge:
    """可作为 Classifier 的 ai_client 回调；也可独立做批量重排。"""

    base_url: str = PRESETS["zhipu"][0]
    model: str = PRESETS["zhipu"][1]
    key_file: str = ".llm_key"
    api_key: str = ""                 # 优先用文件/环境变量，这里是兜底
    timeout: int = 45
    # 每日调用上限：防跑飞、防超额。日报批量算 1 次
    daily_call_cap: int = 200
    redact_enabled: bool = True
    usage_path: str = "llm_usage.jsonl"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _calls_today: int = 0
    _day: str = ""

    # ---------- 凭据 ----------
    def _resolve_key(self) -> str:
        if self.api_key:
            return self.api_key.strip()
        env = os.environ.get("QQGUARD_LLM_KEY", "").strip()
        if env:
            return env
        p = Path(self.key_file)
        if not p.is_absolute():
            p = ROOT / p
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    @property
    def available(self) -> bool:
        return bool(self._resolve_key())

    def status(self) -> dict:
        key = self._resolve_key()
        return {
            "ready": bool(key),
            "base_url": self.base_url,
            "model": self.model,
            "key_hint": (key[:4] + "…" + key[-2:]) if len(key) > 8 else "未配置",
            "calls_today": self._load_usage(),
            "daily_cap": self.daily_call_cap,
            "redact": self.redact_enabled,
        }

    # ---------- 配额 ----------
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load_usage(self) -> int:
        p = Path(self.usage_path)
        if not p.is_absolute():
            p = ROOT / p
        today = self._today()
        n = 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("day") == today:
                    n += 1
        except Exception:
            return 0
        return n

    def quota_left(self) -> int:
        return max(0, self.daily_call_cap - self._load_usage())

    def _record(self, ok: bool, err: str = "", tokens: int = 0) -> None:
        p = Path(self.usage_path)
        if not p.is_absolute():
            p = ROOT / p
        try:
            with self._lock:
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "day": self._today(), "ts": int(time.time()),
                        "model": self.model, "ok": ok,
                        "tokens": tokens, "error": err[:200],
                    }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- HTTP ----------
    def _post(self, payload: dict) -> tuple[dict | None, str]:
        """返回 (解析后的 JSON 对象, 错误说明)。任何异常都不抛出去。"""
        key = self._resolve_key()
        if not key:
            return None, "未配置 API Key"
        if self.quota_left() <= 0:
            return None, f"已达今日调用上限 {self.daily_call_cap} 次"
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 注意：detail 里可能有请求回显，但不含 key，可以留着便于排错
            msg = f"HTTP {e.code}"
            if e.code == 401:
                msg += "（Key 无效或没开通该模型）"
            elif e.code == 429:
                msg += "（限流/额度用完，稍后自动重试即可）"
            elif e.code == 404:
                msg += "（模型名或 base_url 不对）"
            self._record(False, msg + " " + detail)
            return None, msg
        except Exception as e:
            self._record(False, f"{type(e).__name__}: {e}")
            return None, f"网络异常: {type(e).__name__}"

        try:
            data = json.loads(raw)
        except Exception:
            self._record(False, "返回非 JSON")
            return None, "返回内容不是 JSON"

        try:
            text = data["choices"][0]["message"]["content"]
        except Exception:
            self._record(False, "返回结构异常")
            return None, "返回结构异常（可能被网关拦截）"

        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        obj = self._parse_json(text)
        if obj is None:
            self._record(False, "模型输出无法解析为 JSON")
            return None, "模型输出无法解析为 JSON"
        self._record(True, "", tokens)
        return obj, ""

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        text = (text or "").strip()
        if text.startswith("```"):
            parts = text.split("```")
            if len(parts) > 1:
                text = parts[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    # ---------- 单条判定（Classifier 回调） ----------
    def __call__(self, messages: list[dict]) -> dict | None:
        """兼容旧接口：传入 OpenAI 格式 messages，返回判定 dict 或 None。"""
        if not self.available:
            return None
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 500,
        }
        if self.model.startswith("glm"):
            # 智谱默认会输出思考过程，关掉省时省钱
            payload["thinking"] = {"type": "disabled"}
        obj, _ = self._post(payload)
        return obj

    # ---------- 批量判定（日报重排，1 次调用判多条） ----------
    def judge_batch(self, items: list[dict]) -> dict[str, dict]:
        """items: [{id, group, sender, text}]，返回 {id: 判定}。

        一次请求判 20 条以内最划算；超过自动分批。
        """
        if not items or not self.available:
            return {}
        out: dict[str, dict] = {}
        for i in range(0, len(items), 20):
            chunk = items[i:i + 20]
            lines = []
            for it in chunk:
                text = it.get("text", "")
                if self.redact_enabled:
                    text = redact(text)
                lines.append(
                    f'[{it["id"]}] 群={it.get("group", "")} 发送者={it.get("sender", "")} '
                    f'时间={it.get("time", "")}\n{text}'
                )
            user = (
                "逐条判定下面这些群消息。输出 JSON："
                '{"items":[{"id":"...","level":"P0|P1|P2|P3","category":"...",'
                '"title":"...","deadline":"...","action":"...","reason":"...",'
                '"confidence":"高|中|低"}]}\n\n' + "\n\n".join(lines)
            )
            obj, err = self._post({
                "model": self.model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user}],
                "temperature": 0.1,
                "max_tokens": 300 * len(chunk),
                **({"thinking": {"type": "disabled"}} if self.model.startswith("glm") else {}),
            })
            if not obj:
                continue
            for row in (obj.get("items") or []):
                if isinstance(row, dict) and row.get("id"):
                    out[str(row["id"])] = row
        return out
