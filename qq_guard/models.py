"""消息模型、分级结果、结构化信息。

优先级体系：P0 紧急 / P1 重要 / P2 一般有价值 / P3 低价值。
旧版 level（urgent/important/normal/noise）通过 store 迁移映射到新体系。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LEVELS = ["P0", "P1", "P2", "P3"]
LEVEL_LABEL = {
    "P0": "紧急",
    "P1": "重要",
    "P2": "一般有价值",
    "P3": "低价值",
}
# 旧系统兼容映射
LEGACY_LEVEL_MAP = {
    "urgent": "P0",
    "important": "P1",
    "normal": "P2",
    "noise": "P3",
}


@dataclass
class Message:
    message_id: str
    sender_id: str
    sender_name: str
    content: str
    group_id: str = ""
    group_name: str = ""
    timestamp: int = 0
    is_private: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        if self.is_private:
            return f"私聊:{self.sender_name}"
        return f"群:{self.group_name or self.group_id}/{self.sender_name}"


@dataclass
class ExtractedInfo:
    """从消息中抽取的结构化信息。未知字段用"待核实"，不猜测。"""
    category: str = "待核实"          # 通知/科研/竞赛/课程/资料/闲聊 等
    title: str = "待核实"
    event_time: str = "待核实"
    deadline: str = "待核实"
    quota_limit: str = "待核实"
    needs_action: str = "待核实"      # 是/否/待核实
    action_required: str = "待核实"
    importance: str = "待核实"        # 高/中/低
    urgency: str = "待核实"           # 高/中/低（与重要程度分开）
    confidence: str = "待核实"        # 高/中/低
    original_id: str = ""             # 原始消息 ID，用于定位

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category, "title": self.title,
            "event_time": self.event_time, "deadline": self.deadline,
            "quota_limit": self.quota_limit, "needs_action": self.needs_action,
            "action_required": self.action_required, "importance": self.importance,
            "urgency": self.urgency, "confidence": self.confidence,
            "original_id": self.original_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExtractedInfo":
        if not data:
            return cls()
        allowed = {k: str(data.get(k, "待核实")) for k in cls.__dataclass_fields__}
        allowed["original_id"] = str(data.get("original_id", "") or "")
        return cls(**allowed)


@dataclass
class Verdict:
    level: str = "P3"                 # P0/P1/P2/P3
    score: int = 0
    reason: str = ""
    needs_notify: bool = False
    needs_digest: bool = False
    info: ExtractedInfo = field(default_factory=ExtractedInfo)
    is_duplicate: bool = False

    @property
    def label(self) -> str:
        return LEVEL_LABEL.get(self.level, self.level)
