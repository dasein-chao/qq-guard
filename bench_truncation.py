"""截断实验：QQ 通知只给几十个字，分级还准不准？

这是对"通知只有几个字，你咋知道重不重要"的直接回答。
方法：同一批消息，分别用完整原文、截断到 N 字喂给分级引擎，比较判定结果。

指标定义（对用户真正重要的只有前两个）：
- P0 召回：真正紧急的消息，有没有被抓出来（漏报）
- P0 误报：不紧急的被标成 P0（狼来了，比漏报更毁体验）
"""
from __future__ import annotations

import sys

from qq_guard.config import AppConfig
from qq_guard.engine import Classifier
from qq_guard.models import Message

# (原文, 人工标注, 群名)  —— 标注口径：P0=现在不处理就会错过/效果打折
CASES: list[tuple[str, str, str]] = [
    ("【临时通知】高等数学考试改到明天上午8点，地点改到A301，请务必今天确认", "P0", "班级群"),
    ("大创申报材料截止今晚23:59，还没交的赶紧提交，逾期不候", "P0", "竞赛群"),
    ("明天下午2点有互联网+大赛讲座，地点图书馆报告厅，名额有限", "P0", "竞赛群"),
    ("辅导员：今晚10点前必须把确认表交到我这里，没交的明天不能参加答辩", "P0", "年级群"),
    ("明天上午的课取消，改到周五同一时间，教室不变", "P0", "课程群"),
    ("课题组招募本科生参与科研项目，需要3人，报名截止11月30日，有意者请联系", "P1", "科研群"),
    ("关于国家奖学金评选的通知，请符合条件的同学于周五前提交申请材料", "P1", "年级群"),
    ("@全体成员 下周三学院组织学术讲座，欢迎参加，地点在报告厅", "P1", "年级群"),
    ("挑战杯校内选拔开始报名，具体要求见群文件", "P1", "竞赛群"),
    ("实习内推：某厂招后端实习生，简历发我邮箱", "P1", "就业群"),
    ("分享一份高数期末复习资料和历年真题，需要的同学自取", "P2", "班级群"),
    ("这个知识点我整理了一下，讨论一下这道题的解法", "P2", "学习群"),
    ("课件我放群文件了，大家自己下载", "P2", "班级群"),
    ("推荐一个挺好用的笔记软件，写论文方便", "P2", "学习群"),
    ("复习经验总结：先把真题刷两遍，再回头看课本", "P2", "学习群"),
    ("哈哈哈哈 表情包 摸鱼 早安", "P3", "水群"),
    ("签到打卡接龙，第8楼了", "P3", "班级群"),
    ("中午吃什么，食堂三楼新开了一家", "P3", "水群"),
    ("有人打游戏吗，缺一个", "P3", "水群"),
    ("【图片】", "P3", "水群"),
    ("晚安各位，明天见", "P3", "水群"),
    ("接龙：明天去聚餐的扣1", "P3", "水群"),
    ("紧急！我住院了急需用钱，谁能先借我500", "?", "水群"),   # 存疑：可能是诈骗也可能是真事，不计入准确率
    ("公示：本学期助学金名单见群文件，有异议三天内提出", "P1", "年级群"),
]

CUTS = [None, 80, 50, 30, 20]


def classify_all(cut: int | None) -> list[tuple[str, str, str]]:
    cfg = AppConfig()
    clf = Classifier(cfg.rules)
    out = []
    for text, expect, group in CASES:
        body = text if cut is None else text[:cut]
        msg = Message(message_id="x", sender_id="1", sender_name="某人",
                      content=body, group_id="1", group_name=group, timestamp=1764200000)
        out.append((clf.classify(msg).level, expect, body))
    return out


def score(rows) -> dict:
    rows = [r for r in rows if r[1] != "?"]      # 存疑样本不计入
    total = len(rows)
    correct = sum(1 for got, exp, _ in rows if got == exp)
    p0_exp = [(g, e) for g, e, _ in rows if e == "P0"]
    p0_hit = sum(1 for g, _ in p0_exp if g == "P0")
    p0_false = sum(1 for g, e, _ in rows if g == "P0" and e != "P0")
    # 相邻一级以内算"可接受"（P0↔P1、P1↔P2、P2↔P3）
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    near = sum(1 for g, e, _ in rows if abs(order[g] - order[e]) <= 1)
    return {
        "acc": correct / total,
        "near": near / total,
        "p0_recall": p0_hit / max(1, len(p0_exp)),
        "p0_false": p0_false,
        "p0_total": len(p0_exp),
    }


def main() -> int:
    base = classify_all(None)
    print(f"用例 {len(CASES)} 条（P0 六条、P1 五条、P2 四条、P3 八条）\n")
    header = f"{'截断':<8}{'完全一致':<10}{'±1级内':<10}{'P0召回':<10}{'P0误报'}"
    print(header)
    print("-" * len(header) * 2)
    results = {}
    for cut in CUTS:
        rows = classify_all(cut)
        s = score(rows)
        results[cut] = (rows, s)
        name = "完整" if cut is None else f"{cut}字"
        print(f"{name:<8}{s['acc']:<10.0%}{s['near']:<10.0%}"
              f"{s['p0_recall']:<10.0%}{s['p0_false']}/{s['p0_total']}")

    # 先说最要紧的：即使拿到完整原文，引擎本身也会判错
    print("\n--- 完整原文下就判错的（与截断无关，是引擎本身的错）---")
    bad = [(b, e, g) for g, e, b in base if g != e]
    for body, exp, got in bad:
        print(f"  标{exp} → 判{got}  「{body[:46]}」")
    if not bad:
        print("  （无）")
    for cut in (50, 20):
        rows = results[cut][0]
        flips = [(b, e, g, be) for (g, e, b), (b0, e0, be) in zip(rows, base) if g != b0]
        if not flips:
            continue
        print(f"\n--- 截断到 {cut} 字后判定改变 ---")
        for body, exp, got, _ in flips:
            print(f"  标{exp} → 判{got}  「{body[:44]}」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
