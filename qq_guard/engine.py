"""分级引擎：P0-P3 四级分类 + 结构化信息抽取 + 上下文识别 + 重复消息处理。

规则打底，AI 兜底可选（通过 ai_client 回调注入，不绑定具体服务商）。
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Callable

from .ai_client import SYSTEM_PROMPT
from .models import LEVELS, ExtractedInfo, Message, Verdict

if TYPE_CHECKING:
    from .config import RuleConfig

# AI 兜底回调签名：(messages: list[dict]) -> dict | None
AiClient = Callable[[list[dict]], dict | None]


class Classifier:
    def __init__(self, rules: RuleConfig, ai_client: AiClient | None = None,
                 ai_mode: str = "hybrid", vague_lo: int = 15, vague_hi: int = 65):
        self.rules = rules
        self.ai_client = ai_client
        # rules=从不用 AI / ai=每条都问 / hybrid=规则拿不准时才问
        self.ai_mode = ai_mode if ai_mode in ("rules", "ai", "hybrid") else "hybrid"
        self.vague_lo = vague_lo
        self.vague_hi = vague_hi
        # AI 调用计数，便于向用户如实汇报"这次靠 AI 判了几条"
        self.ai_called = 0
        self.ai_upgraded = 0
        self._identity_set = set(rules.my_identities)
        # 每群最近消息，用于上下文识别
        self._context: dict[str, deque[Message]] = defaultdict(
            lambda: deque(maxlen=rules.context_window_size)
        )
        # 最近消息指纹队列，用于重复检测（不能只存一条，否则会被中间消息覆盖）
        self._recent_hashes: dict[str, deque[tuple[str, float]]] = defaultdict(
            lambda: deque(maxlen=max(20, rules.context_window_size * 4))
        )

    def classify(self, msg: Message) -> Verdict:
        # 私聊不监控：直接 P3，不分析不摘要
        if msg.is_private:
            return Verdict(level="P3", score=0, reason="私聊不监控",
                           needs_notify=False, needs_digest=False,
                           info=ExtractedInfo(category="私聊", title="私聊不监控",
                                              original_id=msg.message_id))

        p0_hits = self._hits(msg.content, self.rules.p0_keywords)
        p1_hits = self._hits(msg.content, self.rules.p1_keywords)
        p2_hits = self._hits(msg.content, self.rules.p2_keywords)
        p3_hits = self._hits(msg.content, self.rules.p3_keywords)

        #  legacy 关键词仍参与，但权重降低（兼容旧配置）
        urgent_hits = self._hits(msg.content, self.rules.urgent_keywords)
        important_hits = self._hits(msg.content, self.rules.important_keywords)

        at_hits = [k for k in self.rules.me_keywords if k and k in msg.content]
        if any(ident and ident in msg.content for ident in self._identity_set):
            at_hits.append("我的标识")

        important_sender = (msg.sender_id in self.rules.important_senders
                            or msg.sender_name in self.rules.important_senders)
        authoritative = self._is_authoritative(msg.sender_name, msg.group_name)
        urgency = self._urgency_signals(msg.content)
        in_ignored_group = any(
            g and (g == msg.group_id or g == msg.group_name)
            for g in self.rules.ignored_groups
        )

        score = 0
        if p0_hits:
            score += 60
        if urgent_hits:
            score += 40
        if p1_hits:
            score += min(38 + 6 * (len(p1_hits) - 1), 50)
        if important_hits:
            score += min(20 + 4 * (len(important_hits) - 1), 28)
        if p2_hits:
            score += 12
        if p3_hits:
            score -= 25
        if at_hits:
            score += 40          # @全体 / @我 是正式通知的强信号
        if important_sender:
            score += 35
        if authoritative:
            score += 18          # 老师/辅导员/教务处等权威发送者
        if urgency["deadline_soon"]:
            score += 45          # 今天/明天/24小时内
        if urgency.get("deadline_imminent"):
            score += 30          # 本周内/三天内
        if urgency["deadline_far"]:
            score += 15          # 有明确日期但还远
        if in_ignored_group and not important_sender and not p0_hits:
            score = -100
        score = max(0, min(score, 100))

        # 上下文加分：重要消息可能藏在水聊里，看同群最近消息是否同主题
        context_boost = self._context_boost(msg)
        score = max(0, min(score + context_boost, 100))

        # 分级：P0 = 现在必须处理，否则错过/效果打折
        if p0_hits:
            level = "P0"
        elif urgency.get("urgent_now") and (urgency["deadline_soon"] or urgency["deadline_imminent"]):
            level = "P0"          # 名额有限/先到先得 + 近期 = 现在就得定
        elif urgency["deadline_soon"] and (urgent_hits or at_hits or score >= 80):
            level = "P0"          # 近期限 + 强信号
        elif urgency["deadline_soon"] and (
                authoritative or any(k in msg.content for k in ("必须", "务必", "不得", "一律"))):
            level = "P0"          # 近期限(24h内) + 权威发送者/强制语气 = 现在就得办
        elif score >= 60:
            level = "P1"
        elif p1_hits and not (len(p2_hits) >= 2 and score < 60):
            # P1 词命中，但如果明显是资料/讨论（多个 P2 信号且分数不高），降为 P2
            level = "P1"
        elif (at_hits or authoritative) and (important_hits or urgency["has_deadline"]):
            level = "P1"
        elif p2_hits or score >= 18:
            level = "P2"
        else:
            level = "P3"

        # 结构化抽取
        info = self._extract(msg, level)
        info.original_id = msg.message_id

        # AI 判定：规则打底，AI 在"看不清"的时候接手（可选）
        # rules 模式从不出网；hybrid 只问灰区；ai 模式每条都问
        ai_used = False
        ai_reason = ""
        if self.ai_client and self.ai_mode != "rules":
            ask = False
            if self.ai_mode == "ai":
                ask = True
            elif self.vague_lo <= score <= self.vague_hi and level in ("P1", "P2", "P3"):
                ask = True
            if ask:
                self.ai_called += 1
                ai_result = self._ask_ai(msg, level, info)
                ai_level = str((ai_result or {}).get("level", "")).upper()
                if ai_level in ("P0", "P1", "P2", "P3"):
                    # 规则判 P0 时不让 AI 往下压：漏掉紧急事的代价 > 多响一次
                    if not (level == "P0" and ai_level != "P0"):
                        if ai_level != level and LEVELS.index(ai_level) < LEVELS.index(level):
                            self.ai_upgraded += 1
                        level = ai_level
                    info = self._merge_ai_info(info, ai_result)
                    info.original_id = msg.message_id
                    ai_used = True
                    ai_reason = str(ai_result.get("reason", "") or "")[:80]

        # 重复消息处理
        duplicate = self._is_duplicate(msg)
        if duplicate:
            level = "P3"  # 重复消息降为 P3，不推送不进摘要

        reasons = []
        if p0_hits:
            reasons.append("P0词:" + ",".join(p0_hits))
        if urgent_hits:
            reasons.append("紧急词:" + ",".join(urgent_hits))
        if p1_hits:
            reasons.append("P1词:" + ",".join(p1_hits))
        if important_hits:
            reasons.append("重要词:" + ",".join(important_hits))
        if p2_hits:
            reasons.append("P2词:" + ",".join(p2_hits))
        if p3_hits:
            reasons.append("P3词:" + ",".join(p3_hits))
        if at_hits:
            reasons.append("被@")
        if important_sender:
            reasons.append("重要联系人")
        if context_boost:
            reasons.append(f"上下文+{context_boost}")
        if ai_used:
            reasons.append("AI判定:" + ai_reason if ai_reason else "AI判定")
        if duplicate:
            reasons.append("重复消息")

        verdict = Verdict(
            level=level,
            score=score,
            reason=" | ".join(reasons) if reasons else "无命中",
            needs_notify=level in ("P0", "P1") and not duplicate,
            needs_digest=level in ("P0", "P1", "P2") and not duplicate,
            info=info,
            is_duplicate=duplicate,
        )

        # 写入上下文
        if level != "P3":
            self._context[msg.group_id or msg.group_name or "default"].append(msg)
        return verdict

    def _hits(self, content: str, keywords: list[str]) -> list[str]:
        return [k for k in keywords if k and k.lower() in content.lower()]

    @staticmethod
    def _is_authoritative(sender_name: str, group_name: str) -> bool:
        """权威发送者：老师、辅导员、助教、班委、学院/教务处等。
        这类人发的群消息更可能是正式通知。"""
        markers = [
            "老师", "辅导员", "助教", "班委", "班长", "团支书", "学委", "学习委员",
            "教务处", "学院办公室", "办公室", "教务处", "系主任", "导师", "教授",
            "负责人", "管理员", "主任", "秘书",
        ]
        return any(m in (sender_name or "") for m in markers)

    @staticmethod
    def _urgency_signals(content: str) -> dict:
        """时间紧迫度识别。区分"近期限"和"有明确日期"。"""
        # 只有 24-48 小时内才算 P0 级紧迫；"本周""三天内"归到 imminent（P1 级）
        soon_patterns = [
            "今天", "今晚", "今日", "明天", "明早", "明晚", "明日",
            "24小时", "小时内", "马上", "立即", "现在", "即刻",
        ]
        imminent_patterns = [
            "本周", "这周", "周内", "三天内", "两天内", "还有3天", "还有2天",
            "周四", "周五", "周六", "周日", "尽快",
        ]
        far_patterns = [
            "下周", "下月", "本月", "月底", "下周一", "下周五",
            "月", "号", "日截止", "之前提交", "前提交", "前报名",
        ]
        # 强制出席/参加也视为有硬性要求
        mandatory = ["不得缺席", "必须参加", "务必参加", "准时参加", "不准请假", "全员"]
        # "现在不处理就没了"的信号：名额有限、先到先得、限时等
        # 用户标准：现在不决定就会错过，哪怕事件在明天，现在也必须处理 → P0
        now_or_never = ["名额有限", "名额", "先到先得", "限时", "仅限", "限人数",
                        "即将截止", "抓紧", "最后一天", "报满"]
        urgent_now = any(k in content for k in now_or_never)
        # "今晚10点前""明天8点半前"这类钟点式硬截止：没有"截止/提交"字样也必须是硬期限
        # （2026-11-27 修复：原逻辑只认"截止/提交"等词，导致"今晚10点前必须交"被漏判为 P2）
        clock_deadline = re.search(
            r"\d{1,2}\s*[点时](?:半|\d{1,2}\s*分)?\s*(?:之)?前", content
        )
        has_deadline = any(k in content for k in
                           ["截止", "之前", "前提交", "前报名", "前反馈", "前联系",
                            "最晚", "限额", "限时", "内定", "报名", "提交", "反馈",
                            "必须交", "必须提交", "务必交", "务必提交", "不得缺席"]) \
            or urgent_now or any(k in content for k in mandatory) \
            or bool(clock_deadline)
        deadline_soon = any(p in content for p in soon_patterns) and has_deadline
        deadline_imminent = any(p in content for p in imminent_patterns) and has_deadline
        # "还有N天"：N<=1 算 P0 级，N<=5 算 P1 级
        m = re.search(r"还有\s*(\d+)\s*天", content)
        if m:
            n = int(m.group(1))
            has_deadline = True
            if n <= 1:
                deadline_soon = True
            elif n <= 5:
                deadline_imminent = True
        deadline_far = (not deadline_soon) and (not deadline_imminent) and (
            has_deadline or any(p in content for p in far_patterns)
        )
        return {
            "has_deadline": has_deadline,
            "deadline_soon": deadline_soon,
            "deadline_imminent": deadline_imminent,
            "deadline_far": deadline_far,
            "urgent_now": urgent_now,
        }

    def _context_boost(self, msg: Message) -> int:
        """上下文识别：同群最近消息若与当前消息高度相关，说明不是孤立闲聊。"""
        ctx = self._context.get(msg.group_id or msg.group_name or "default")
        if not ctx:
            return 0
        boost = 0
        for prev in ctx:
            ratio = SequenceMatcher(None, msg.content, prev.content).ratio()
            if 0.45 < ratio < 0.92:  # 相关但不重复
                boost += 8
                break
            # 前文含重要信号而当前是短回复（"好的""收到"），继承部分重要性
            if len(msg.content) <= 8 and any(
                k in prev.content for k in self.rules.p0_keywords + self.rules.p1_keywords
            ):
                boost += 15
                break
        return min(boost, 20)

    def _is_duplicate(self, msg: Message) -> bool:
        import time
        now = time.time()
        key = msg.group_id or msg.group_name or "default"
        queue = self._recent_hashes[key]
        for prev_hash, prev_time in queue:
            if now - prev_time < self.rules.dedupe_window_seconds:
                ratio = SequenceMatcher(None, msg.content, prev_hash).ratio()
                if ratio >= self.rules.duplicate_similarity:
                    return True
        queue.append((msg.content, now))
        return False

    def _extract(self, msg: Message, level: str) -> ExtractedInfo:
        """规则方式抽取结构化信息。未知一律待核实，不猜测。"""
        content = msg.content
        category = self._guess_category(content)
        deadline = "待核实"
        for marker in self.rules.deadline_markers:
            idx = content.find(marker)
            if idx >= 0:
                deadline = content[max(0, idx - 12): min(len(content), idx + 24)].strip()
                break
            m = re.search(r"(\d{1,2}月\d{1,2}日?|\d{1,2}/\d{1,2}|\d{4}-\d{2}-\d{2})", content)
            if m:
                deadline = content[max(0, m.start() - 12): min(len(content), m.end() + 20)].strip()
                break

        needs_action = "待核实"
        if any(k in content for k in self.rules.action_markers):
            needs_action = "是"

        # 标题：取前 40 字，去掉纯表情
        title = content[:40].strip() or "待核实"

        importance = "高" if level in ("P0", "P1") else ("中" if level == "P2" else "低")
        urgency = "高" if level == "P0" else ("中" if level == "P1" else "低")
        confidence = "高" if len(content) >= 10 else "中"

        return ExtractedInfo(
            category=category,
            title=title,
            deadline=deadline,
            needs_action=needs_action,
            action_required=content[:80] if needs_action == "是" else "待核实",
            importance=importance,
            urgency=urgency,
            confidence=confidence,
        )

    def _guess_category(self, content: str) -> str:
        rules = [
            (("招募", "课题组", "实验室", "科研", "导师", "项目"), "科研机会"),
            (("竞赛", "大创", "互联网+", "挑战杯", "创新创业"), "竞赛活动"),
            (("考试", "课程", "作业", "复习", "课件", "成绩"), "课程学习"),
            (("通知", "公示", "评选", "奖学金", "学院", "班级"), "正式通知"),
            (("讲座", "沙龙", "活动", "实践"), "活动机会"),
            (("资料", "分享", "笔记", "真题", "教程", "推荐"), "学习资料"),
        ]
        for keywords, label in rules:
            if any(k in content for k in keywords):
                return label
        return "其他"

    def _ask_ai(self, msg: Message, level: str, info: ExtractedInfo) -> dict | None:
        """调用注入的 AI 客户端。失败返回 None，不影响原始消息保存。"""
        import time as _t
        when = ""
        if msg.timestamp:
            try:
                when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(msg.timestamp))
            except Exception:
                when = ""
        now = _t.strftime("%Y-%m-%d %H:%M")
        try:
            return self.ai_client([{
                "role": "system",
                "content": SYSTEM_PROMPT,
            }, {
                "role": "user",
                "content": (
                    f"现在是 {now}。\n"
                    f"群：{msg.group_name or msg.group_id or '未知'}\n"
                    f"发送者：{msg.sender_name or '未知'}\n"
                    f"消息时间：{when or '未知'}\n"
                    f"内容：{msg.content}\n\n"
                    f"（规则引擎初判 {level}，供参考，你可以推翻）"
                ),
            }])
        except Exception:
            return None

    @staticmethod
    def _merge_ai_info(info: ExtractedInfo, ai: dict | None) -> ExtractedInfo:
        """把 AI 的扁平字段合进结构化信息。AI 说"待核实"的不覆盖规则已抽到的值。"""
        if not ai:
            return info
        mapping = {
            "category": "category", "title": "title", "deadline": "deadline",
            "action": "action_required", "reason": None, "confidence": "confidence",
        }
        for ai_key, info_key in mapping.items():
            if not info_key:
                continue
            val = str(ai.get(ai_key, "") or "").strip()
            if val and val not in ("待核实", "未知", "无", "none", "None", "-"):
                setattr(info, info_key, val[:120])
        lv = str(ai.get("level", "")).upper()
        if lv in ("P0", "P1"):
            info.importance = "高"
        elif lv == "P3":
            info.importance = "低"
        return info
