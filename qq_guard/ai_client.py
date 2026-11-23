"""AI 客户端：通过本地 Node.js 桥接调用平台模型，支持主备切换与降级。

模型不可用时返回 None，调用方照常保存原始消息——不丢信息。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BRIDGE_URL = "http://127.0.0.1:8787"


@dataclass
class ModelCallLog:
    timestamp: int
    model: str
    ok: bool
    error: str = ""
    tokens: int = 0
    credit: float | str = ""


@dataclass
class AiClient:
    """可作为 Classifier 的 ai_client 回调注入。"""
    bridge_url: str = BRIDGE_URL
    primary_model: str = "glm-5.3-flash"
    backup_model: str = "deepseek-v4.1-flash"
    timeout: int = 60
    log_path: str = "model_calls.jsonl"
    _logs: list[ModelCallLog] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def available(self) -> bool:
        """桥接服务是否就绪且凭证已配置。"""
        try:
            with urllib.request.urlopen(f"{self.bridge_url}/health", timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data.get("ok") and data.get("credentials") == "ready"
        except Exception:
            return False

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.bridge_url}/health", timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e), "hint": "桥接服务未启动"}

    def list_models(self) -> list[dict]:
        try:
            with urllib.request.urlopen(f"{self.bridge_url}/models", timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data.get("models", []) if data.get("ok") else []
        except Exception:
            return []

    def __call__(self, messages: list[dict]) -> dict | None:
        """实现 Classifier 的 ai_client 接口。先主后备，都失败返回 None。"""
        for model, role in [(self.primary_model, "主模型"), (self.backup_model, "备用模型")]:
            result = self._call(model, messages)
            if result is not None:
                return result
            self._log(ModelCallLog(int(time.time()), model, False,
                                   f"{role}调用失败，尝试下一个" if role == "主模型" else "备用模型也失败"))
        return None

    def _call(self, model: str, messages: list[dict]) -> dict | None:
        payload = json.dumps({"model": model, "messages": messages}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.bridge_url}/classify",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            if not data.get("ok"):
                self._log(ModelCallLog(int(time.time()), model, False, data.get("error", "unknown")))
                return None
            content = data.get("content", "")
            result = self._parse_json(content)
            usage = data.get("usage") or {}
            self._log(ModelCallLog(
                int(time.time()), model, True, "",
                tokens=int(usage.get("totalTokens") or 0),
                credit=usage.get("credit") or "",
            ))
            return result
        except Exception as e:
            self._log(ModelCallLog(int(time.time()), model, False, str(e)))
            return None

    @staticmethod
    def _parse_json(text: str) -> dict | None:
        """从模型输出中提取 JSON，容忍 ```json 包裹和前后文字。"""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            return None
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    def _log(self, entry: ModelCallLog) -> None:
        with self._lock:
            self._logs.append(entry)
            try:
                p = Path(self.log_path)
                if not p.is_absolute():
                    p = Path(__file__).resolve().parent.parent / p
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": entry.timestamp, "model": entry.model, "ok": entry.ok,
                        "error": entry.error, "tokens": entry.tokens, "credit": entry.credit,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def stats(self) -> dict:
        with self._lock:
            total = len(self._logs)
            ok = sum(1 for x in self._logs if x.ok)
            by_model: dict[str, dict] = {}
            for x in self._logs:
                d = by_model.setdefault(x.model, {"calls": 0, "ok": 0, "errors": []})
                d["calls"] += 1
                d["ok"] += 1 if x.ok else 0
                if x.error:
                    d["errors"].append(x.error)
            return {"total": total, "success": ok, "failed": total - ok, "by_model": by_model}
