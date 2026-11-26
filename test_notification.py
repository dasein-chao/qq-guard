"""通知入口的离线测试：不依赖手机，用构造的通知 JSON 验证解析逻辑。"""
from __future__ import annotations

import sys

from qq_guard.sources_ntf import classify_scope, parse_notification, _clean_title


def ntf(**kw):
    base = {
        "id": "1", "key": "1|g", "group": "g", "postTime": 1764200000000,
        "title": "", "content": "", "bigText": "", "channelId": "",
        "packageName": "com.tencent.mobileqq",
    }
    base.update(kw)
    return base


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" != {want!r}"))
    return ok


def main() -> int:
    ok = True
    H = ["group", "群", "qun"]

    # 1. 群渠道 + bigText 带发送者前缀
    m = parse_notification(ntf(channelId="qq_group_msg", title="21级软工3班",
                              bigText="张三：明天下午讲座改到3点，名额有限"), H)
    ok &= check("群消息被识别", bool(m), True)
    ok &= check("发送者拆出", m.sender_name if m else None, "张三")
    ok &= check("正文剥掉前缀", m.content if m else None, "明天下午讲座改到3点，名额有限")
    ok &= check("群名", m.group_name if m else None, "21级软工3班")

    # 2. 好友渠道 → 一律丢弃
    ok &= check("私聊被丢弃", parse_notification(
        ntf(channelId="qq_friend_msg", title="李四", content="在吗"), H), None)

    # 3. 非 QQ 包名 → 丢弃
    ok &= check("非QQ包名丢弃", parse_notification(
        ntf(packageName="com.tencent.mm", channelId="qq_group_msg", title="某群"), H), None)

    # 4. 标题带数量后缀要清掉
    ok &= check("标题净化", _clean_title("21级软工3班(3条新消息)"), "21级软工3班")
    ok &= check("标题净化2", _clean_title("科研通知群等3条消息"), "科研通知群")

    # 5. channelId 为空、标题也不像群 → 保守丢弃
    ok &= check("未知归属默认丢弃", parse_notification(
        ntf(title="王小明", content="通知：明天停课"), H), None)
    # 5b. 显式开启后当群处理
    m = parse_notification(ntf(title="王小明", content="通知：明天停课"), H,
                           assume_group_when_unknown=True)
    ok &= check("未知归属可放开", bool(m), True)
    # 5c. 没有 channelId 但标题含"群" → 判为群（channelId 优先判私聊，因此风险有限）
    ok &= check("标题含群判为群", classify_scope(ntf(title="摄影交流群"), H), "group")
    # 5d. channelId 明确是好友渠道时，标题里的"群"不能翻案
    ok &= check("好友渠道优先", classify_scope(
        ntf(channelId="qq_friend_msg", title="摄影交流群"), H), "private")

    # 6. 群白名单过滤
    ok &= check("白名单外丢弃", parse_notification(
        ntf(channelId="qq_group_msg", title="无关群", content="哈哈哈"), H,
        watch_groups=["21级软工3班"]), None)
    ok &= check("白名单内通过", bool(parse_notification(
        ntf(channelId="qq_group_msg", title="21级软工3班", content="通知"), H,
        watch_groups=["21级软工3班"])), True)

    # 7. 群名被塞进正文："群名: 昵称: 内容"
    m = parse_notification(ntf(channelId="qq_group_msg", title="21级软工3班",
                              bigText="21级软工3班: 王五: 明天上午交实验报告"), H)
    ok &= check("双层前缀-发送者", m.sender_name if m else None, "王五")
    ok &= check("双层前缀-正文", m.content if m else None, "明天上午交实验报告")

    # 8. 只有 content 没有 bigText
    m = parse_notification(ntf(channelId="qq_group_msg", title="竞赛群",
                              content="赵六: 挑战杯报名今晚24点截止"), H)
    ok &= check("content 兜底", m.content if m else None, "挑战杯报名今晚24点截止")

    # 9. 时间戳从毫秒归一
    m = parse_notification(ntf(channelId="qq_group_msg", title="群", content="x",
                              postTime=1764200000000), H)
    ok &= check("毫秒转秒", m.timestamp if m else None, 1764200000)

    # 10. 归属判定
    ok &= check("scope 群", classify_scope(ntf(channelId="qq_group_msg"), H), "group")
    ok &= check("scope 私聊", classify_scope(ntf(channelId="qq_friend_msg"), H), "private")
    ok &= check("scope 未知", classify_scope(ntf(), H), "unknown")

    # 11. 空通知不崩
    ok &= check("空通知", parse_notification({}, H), None)

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
