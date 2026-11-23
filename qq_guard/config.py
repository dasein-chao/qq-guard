"""配置定义与加载。配置文件为项目根目录下的 config.json。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


@dataclass
class RuleConfig:
    # 命中即判紧急（高权重）
    urgent_keywords: list[str] = field(default_factory=lambda: [
        "急", "紧急", "立刻", "马上", "尽快", "出事了", "故障", "崩了", "挂了",
        "事故", "报警", "救命", "起火", "住院", "急诊", "被抓", "丢了",
        "deadline", "上线", "发布", "回滚", "宕机", "安全漏洞",
    ])
    # 命中判为值得摘要，但不立即提醒
    important_keywords: list[str] = field(default_factory=lambda: [
        "会议", "评审", "提交", "截止", "通知", "安排", "方案", "需求", "改动",
        "确认", "审批", "面试", "offer", "合同", "付款", "到账", "面试",
        "考试", "成绩", "报名", "报告", "复盘", "计划", "进度",
    ])
    # 命中直接过滤（水群/广告/机器人）
    noise_keywords: list[str] = field(default_factory=lambda: [
        "签到", "打卡", "接龙", "投票", "抽奖", "红包", "恭喜发财",
        "水群", "摸鱼", "早安", "晚安", "中午吃什么",
    ])
    # 重要联系人：QQ号或昵称，命中后其消息最低按 important 处理
    important_senders: list[str] = field(default_factory=list)
    # 完全忽略的群：群号或群名，群内所有消息直接过滤
    ignored_groups: list[str] = field(default_factory=list)
    # AI 兜底：规则分数处于模糊区间时调用大模型判断
    ai_fallback_enabled: bool = False
    ai_fallback_min_score: int = 20
    ai_fallback_max_score: int = 70
    # OpenAI 兼容接口（可选）。留空则 AI 兜底不生效
    ai_api_base: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    # 我自己的 QQ 号/昵称，用于识别 @我
    my_identities: list[str] = field(default_factory=list)
    me_keywords: list[str] = field(default_factory=lambda: ["@我", "[CQ:at", "@全体成员"])
    # 规则分数阈值
    urgent_threshold: int = 75
    important_threshold: int = 35
    # 哪些等级进入摘要（noise 永远不进）
    digest_levels: list[str] = field(
        default_factory=lambda: ["urgent", "important", "normal"]
    )
    # 同一发送者在窗口内的同类消息只提醒一次，避免轰炸
    dedupe_window_seconds: int = 120

    # === P0-P3 新体系（第一阶段）===

    # P0 紧急信号：临时调整、临近截止、需立即确认、错过有损失
    p0_keywords: list[str] = field(default_factory=lambda: [
        "改到", "改时间", "改地点", "临时调整", "临时通知", "现在就", "马上提交",
        "立刻提交", "紧急", "截止今晚", "今天截止", "明天截止", "即将截止",
        "名额已满", "最后一天", "逾期不候", "过期不候", "务必今天",
    ])
    # P1 重要信号：科研/竞赛/机会/正式通知
    p1_keywords: list[str] = field(default_factory=lambda: [
        "招募", "课题组", "实验室", "科研项目", "导师", "竞赛", "创新创业",
        "大创", "互联网+", "挑战杯", "讲座", "报名", "申请", "选拔", "面试",
        "通知", "公示", "评选", "奖学金", "实习", "内推",
    ])
    # P2 一般有价值：学习讨论、资料、经验
    p2_keywords: list[str] = field(default_factory=lambda: [
        "资料", "分享", "经验", "讨论", "复习", "笔记", "课件", "真题",
        "学习", "教程", "推荐", "总结", "答疑",
    ])
    # P3 低价值：闲聊、表情、广告、重复
    p3_keywords: list[str] = field(default_factory=lambda: [
        "签到", "打卡", "接龙", "投票", "抽奖", "红包", "恭喜发财",
        "水群", "摸鱼", "早安", "晚安", "中午吃什么", "哈哈", "哈哈哈",
        "表情包", "广告", "推广", "转发抽奖",
    ])
    # 截止时间信号词（用于抽取 deadline）
    deadline_markers: list[str] = field(default_factory=lambda: [
        "截止", " deadline ", "之前", "前提交", "前报名", "最晚", "限时", "截至",
    ])
    # 需要行动的信号词
    action_markers: list[str] = field(default_factory=lambda: [
        "请", "务必", "需要", "要求", "记得", "请及时", "尽快", "提交", "报名", "确认",
    ])
    # 上下文窗口：判断前一并看同群最近 N 条，识别藏在水聊里的重要信息
    context_window_size: int = 5
    # 文本相似度阈值（0-1），超过则判为重复消息
    duplicate_similarity: float = 0.92


@dataclass
class RetentionConfig:
    """留存策略：只留够回溯的，不堆数据。

    用户明确表示不需要长期存储，"当天提示完当天就回"。
    默认：P2/P3 留 3 天，P0/P1 留 60 天（够追溯一学期内的通知）。
    """
    normal_days: int = 3          # P2/P3 保留天数
    important_days: int = 60      # P0/P1 保留天数（0 = 永久）
    keep_p0p1_forever: bool = False
    cleanup_hour: int = 3         # 每天凌晨执行清理


@dataclass
class NotifyConfig:
    # 邮件（手机邮箱 App 可收推送）。QQ邮箱需用授权码而非登录密码
    email_enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""
    # 通用 Webhook：Server酱³ / 企业微信 / 钉钉 / Bark / Telegram 均支持
    # 留空则禁用。模板里用 {title} 和 {content} 作为占位符
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_payload_template: str = (
        '{"title":"{title}","text":"{content}"}'
    )
    # ntfy 手机推送（免费、无账号、安卓原生通知栏；已定为首选推送渠道）
    # 手机装 ntfy App 后订阅同一个 topic，这里往 topic 发消息即可
    ntfy_enabled: bool = True
    ntfy_server: str = "https://ntfy.sh"
    # topic 名就是凭据，必须足够随机，别用可猜的名字
    ntfy_topic: str = ""
    # P0 用 max（穿透静音持续响铃），P1 用 high，日报用 default
    ntfy_topic_by_level: dict[str, str] = field(default_factory=dict)
    # 桌面弹窗（仅本机运行时有效）
    desktop_enabled: bool = True
    # 摘要发送时间（24小时制，每小时检查一次）
    summary_hour: int = 21
    summary_enabled: bool = True


@dataclass
class SourceConfig:
    # OneBot v11 正向 WebSocket 地址，只允许本地回环
    websocket_url: str = "ws://127.0.0.1:3001"
    # AccessToken 文件路径（相对项目根），不写 token 本身进配置
    access_token_file: str = ".protocol_token"
    # 只监听这些群，留空表示全部（私聊永远不监控）
    watch_groups: list[str] = field(default_factory=list)
    watch_senders: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    rules: RuleConfig = field(default_factory=RuleConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    db_path: str = "messages.db"


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    return AppConfig(
        rules=RuleConfig(**data.get("rules", {})),
        notify=NotifyConfig(**data.get("notify", {})),
        source=SourceConfig(**data.get("source", {})),
        retention=RetentionConfig(**data.get("retention", {})),
        db_path=data.get("db_path", "messages.db"),
    )


def upgrade_config_file(path: Path = CONFIG_PATH) -> tuple[bool, str]:
    """就地补齐新增配置字段，保留用户已改过的值。返回 (是否有改动, 说明)。"""
    if not path.exists():
        return False, "配置文件不存在"
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = asdict(AppConfig())
    changed = False
    for section, default_values in defaults.items():
        if section == "db_path":
            if "db_path" not in data:
                data["db_path"] = default_values
                changed = True
            continue
        if section not in data:
            data[section] = default_values
            changed = True
            continue
        for key, value in default_values.items():
            if key not in data[section]:
                data[section][key] = value
                changed = True
    if changed:
        backup = path.with_suffix(".json.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, f"已补齐新字段，原配置备份至 {backup.name}"
    return False, "配置已是最新"
