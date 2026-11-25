"""验收测试：对抗性边界用例，专打开发自测覆盖不到的地方。"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")
from qq_guard.config import AppConfig, load_config
from qq_guard.engine import Classifier
from qq_guard.models import ExtractedInfo, Message, Verdict
from qq_guard.app import App
from qq_guard.store import Store

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def fresh_cfg():
    cfg = AppConfig()
    cfg.db_path = str(Path(tempfile.mkdtemp()) / "acc.db")
    cfg.notify.desktop_enabled = False
    cfg.notify.ntfy_enabled = False
    cfg.notify.email_enabled = False
    cfg.notify.webhook_enabled = False
    cfg.notify.summary_enabled = False
    return cfg


print("=== A. 私聊绝对不触碰（铁律）===")
cfg = fresh_cfg()
app = App(cfg)
# 私聊内容即使是明显的 P0 关键词，也绝不能提醒/摘要
for text in ["明天考试紧急", "今晚12点截止", "@全体成员 立刻提交", "出事了"]:
    v = app.handle(Message("p1", "u", "某人", text, is_private=True))
    check(f"私聊不提醒: {text[:10]}", not v.needs_notify and not v.needs_digest,
          f"level={v.level} notify={v.needs_notify}")
check("私聊绝不进摘要", len(app.store.pending_digest(["P0", "P1", "P2", "P3"])) == 0)


print("\n=== B. 异常输入不崩溃 ===")
c = Classifier(AppConfig().rules)
weird = [
    Message("e1", "u", "s", "", "g", "群"),                    # 空消息
    Message("e2", "u", "s", "   \n\t  ", "g", "群"),            # 纯空白
    Message("e3", "u", "s", "🚀" * 500, "g", "群"),             # 超长 emoji
    Message("e4", "u", "s", "中" * 20000, "g", "群"),           # 超长文本
    Message("e5", "u", "s", "截止<script>alert(1)</script>", "g", "群"),  # 注入串
    Message("e6", "u", "s", "%s %d {0} " * 100, "g", "群"),     # 格式化串
]
for m in weird:
    try:
        v = c.classify(m)
        check(f"异常输入不崩: {m.message_id}", v is not None and v.level in ("P0", "P1", "P2", "P3"),
              f"level={v.level}")
    except Exception as e:
        check(f"异常输入不崩: {m.message_id}", False, f"{type(e).__name__}: {e}")


print("\n=== C. 重复与去重边界 ===")
cfg = fresh_cfg()
app = App(cfg)
base = Message("d0", "u", "s", "明天下午2点开会", "g", "测试群")
app.handle(base)
# 完全相同
v1 = app.handle(Message("d1", "u", "s", "明天下午2点开会", "g", "测试群"))
check("完全相同判重复", v1.is_duplicate, f"reason={v1.reason}")
# 轻微差异
v2 = app.handle(Message("d2", "u", "s", "明天下午2点开会把", "g", "测试群"))
check("轻微改写判重复", v2.is_duplicate, f"reason={v2.reason}")
# 不同群同内容不该互相压制
v3 = app.handle(Message("d3", "u", "s", "明天下午2点开会", "g2", "另一个群"))
check("不同群不误压", not v3.is_duplicate, f"reason={v3.reason}")


print("\n=== D. 优先级边界（用户口径）===")
c = Classifier(AppConfig().rules)
cases = [
    ("明天下午讲座，名额有限", "P0", "名额有限需要现在决定"),
    ("今天截止提交", "P0", "今明截止"),
    ("明天考试", "P1", "无紧迫行动"),
    ("课题组招研究生", "P1", "重要机会"),
    ("分享一份复习资料", "P2", "一般价值"),
    ("哈哈哈哈哈", "P3", "闲聊"),
    ("签到", "P3", "水群"),
]
for text, expect, why in cases:
    v = c.classify(Message("x", "u", "s", text, "g", "群"))
    check(f"分级 [{text[:12]}]", v.level == expect, f"得{v.level} 期{expect} ({why})")


print("\n=== E. 配置损坏/缺失的健壮性 ===")
try:
    bad = Path(tempfile.mkdtemp()) / "bad.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    try:
        load_config(bad)
        check("损坏配置被捕获", False, "应当抛异常或降级，不能静默")
    except Exception:
        check("损坏配置被捕获", True, "明确报错，好过静默用错配置")
except Exception as e:
    check("损坏配置被捕获", False, str(e))

try:
    missing = Path(tempfile.mkdtemp()) / "none.json"
    cfg2 = load_config(missing)
    check("缺失配置自动创建", missing.exists() and cfg2.db_path, "首次运行能自举")
except Exception as e:
    check("缺失配置自动创建", False, str(e))


print("\n=== F. 存储与查询边界 ===")
cfg = fresh_cfg()
store = Store(cfg)
check("空库查询不崩", len(store.query(keyword="不存在")) == 0)
check("空库统计不崩", isinstance(store.stats(), dict))
check("空库摘要不崩", len(store.pending_digest(["P0"])) == 0)


print("\n=== G. 提醒格式完整性（P0）===")
cfg = fresh_cfg()
app = App(cfg)
from qq_guard.notifiers import Notifier
v = app.handle(Message("fmt", "u", "老师", "明天必须交材料", "g", "课程群"))
body = Notifier.format_alert(Message("fmt", "u", "老师", "明天必须交材料", "g", "课程群"), v)
required = ["优先级", "事项标题", "来源群聊", "消息时间", "重要原因", "需要采取的行动", "截止时间", "原始消息", "消息ID", "可信度"]
missing = [k for k in required if k not in body]
check("九要素齐全", not missing, f"缺={missing}")


print("\n=== 汇总 ===")
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"总计 {passed}/{len(results)} 通过，失败 {failed}")
if failed:
    print("失败项:")
    for n, ok, d in results:
        if not ok:
            print(f"  - {n}: {d}")
raise SystemExit(1 if failed else 0)
