"""兜底系统：完整备份、可读导出、新号迁移清单、一键恢复。

设计原则：
- 能自动的：配置、规则、历史消息、分级数据 → 全部打包，可一键恢复
- 不能自动的：QQ 好友/群关系（在腾讯服务器上）→ 生成清单，照着手动加
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .config import CONFIG_PATH, ROOT, AppConfig
from .store import Store

BACKUP_ROOT = ROOT / "backups"
LATEST_POINTER = BACKUP_ROOT / "LATEST.json"
VERSION = "1.0"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000 if ts > 1e12 else ts))


def export_readable(store: Store, out_dir: Path, cfg: AppConfig) -> dict[str, Path]:
    """导出人类可读/可迁移的格式。返回各文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    items = store.all_messages()
    paths: dict[str, Path] = {}

    # CSV：任何工具都能打开
    csv_path = out_dir / "messages.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "优先级", "类别", "分数", "来源", "发送者ID", "发送者",
                    "群ID", "群名", "标题", "截止时间", "需行动", "可信度",
                    "内容", "命中原因", "消息ID"])
        for m, v in items:
            w.writerow([
                _fmt(m.timestamp), v.level, v.info.category, v.score,
                "私聊" if m.is_private else "群聊", m.sender_id, m.sender_name,
                m.group_id, m.group_name, v.info.title, v.info.deadline,
                v.info.needs_action, v.info.confidence, m.content, v.reason, m.message_id,
            ])
    paths["csv"] = csv_path

    # Markdown：直接可读的报告
    md_path = out_dir / "messages.md"
    lines = ["# QQ 消息分级归档", "", f"导出时间：{_fmt(int(time.time()))}",
             f"消息总数：{len(items)}", ""]
    levels = {"P0": "P0 紧急", "P1": "P1 重要", "P2": "P2 一般有价值", "P3": "P3 低价值"}
    for lv, label in levels.items():
        group = [(m, v) for m, v in items if v.level == lv]
        if not group:
            continue
        lines += [f"## {label}（{len(group)} 条）", ""]
        for m, v in group:
            src = f"私聊 {m.sender_name}" if m.is_private else f"{m.group_name or m.group_id} / {m.sender_name}"
            lines.append(f"- **{_fmt(m.timestamp)}** `{src}` {m.content}")
            if v.info.deadline != "待核实" or v.info.needs_action == "是":
                lines.append(f"  - 截止：{v.info.deadline} | 行动：{v.info.action_required}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    paths["md"] = md_path

    # 规则清单：给新号重新配置时照抄
    rules_path = out_dir / "rules.txt"
    r = cfg.rules
    rules_lines = [
        "=== 筛选规则快照（换号后直接抄进新 config.json）===", "",
        f"P0 紧急词（{len(r.p0_keywords)}）：{', '.join(r.p0_keywords)}", "",
        f"P1 重要词（{len(r.p1_keywords)}）：{', '.join(r.p1_keywords)}", "",
        f"P2 一般价值词（{len(r.p2_keywords)}）：{', '.join(r.p2_keywords)}", "",
        f"P3 低价值词（{len(r.p3_keywords)}）：{', '.join(r.p3_keywords)}", "",
        f"（旧版兼容词）紧急：{', '.join(r.urgent_keywords)}", "",
        f"（旧版兼容词）重要：{', '.join(r.important_keywords)}", "",
        f"（旧版兼容词）水群：{', '.join(r.noise_keywords)}", "",
        f"重要联系人：{', '.join(r.important_senders) or '（未设置）'}", "",
        f"忽略的群：{', '.join(r.ignored_groups) or '（未设置）'}", "",
        f"我的标识（用于识别@我）：{', '.join(r.my_identities) or '（未设置）'}", "",
        f"紧急阈值：{r.urgent_threshold} / 重要阈值：{r.important_threshold}", "",
        f"摘要包含的等级：{', '.join(r.digest_levels)}", "",
        "注意：重要联系人、忽略的群、我的标识里如果写的是 QQ 号，",
        "换号后需要把与旧号相关的值改成新号对应的值。",
    ]
    rules_path.write_text("\n".join(rules_lines), encoding="utf-8")
    paths["rules"] = rules_path
    return paths


def make_migration_guide(backup_dir: Path, cfg: AppConfig, stats: dict[str, int]) -> Path:
    """生成新号迁移清单：区分自动完成和必须手动的步骤。"""
    r = cfg.rules
    # 检测配置里疑似 QQ 号（纯数字串），换号时重点检查
    numeric_fields = []
    for label, values in [
        ("重要联系人", r.important_senders),
        ("忽略的群", r.ignored_groups),
        ("我的标识", r.my_identities),
    ]:
        nums = [v for v in values if str(v).isdigit() and len(str(v)) >= 5]
        if nums:
            numeric_fields.append(f"- {label}：{', '.join(nums)}")

    guide = f"""# 换号迁移清单

本备份目录：`{backup_dir.name}`
生成时间：{_fmt(int(time.time()))}
备份时的消息统计：{json.dumps(stats, ensure_ascii=False)}

## A. 已自动完成（恢复时一键搞定）

- [x] 筛选规则 / 关键词 / 黑白名单 → `config.json`
- [x] 历史分级消息 → `messages.db`
- [x] 可读归档 → `export/messages.csv`、`export/messages.md`、`export/rules.txt`
- [x] 提醒配置（邮箱 / Webhook）→ `config.json.notify`

## B. 必须手动（腾讯服务器上，程序无法代劳）

- [ ] 注册新 QQ 号，完成实名和养号（正常使用几天，别新号直接挂）
- [ ] 用新号重新加入你要监控的所有群
      （群号清单见 `export/messages.csv` 的「群ID」「群名」列，去重后逐个加）
- [ ] 新号手机 QQ → 群设置 → 机器人 → 确认监控权限
- [ ] 旧手机 Termux 里重新跑安装脚本，扫码时**用新号扫**
- [ ] 把 `config.json.source.websocket_url` 指向新的协议端地址（如果 IP/端口变了）

## C. 换号后重点核对的配置项

下面这些字段如果写的是 QQ 号，换号后可能要改成新号对应的值：

{chr(10).join(numeric_fields) if numeric_fields else '- （自动检测未发现纯数字 QQ 号，但仍建议人工过一遍）'}

## D. 恢复命令

```bash
python main.py restore --from "{backup_dir.name}"
```

恢复会覆盖当前的 config.json 和 messages.db，执行前会再次确认。
覆盖前建议先跑一次 `python main.py backup` 把现状存下来。
"""
    path = backup_dir / "MIGRATION_GUIDE.md"
    path.write_text(guide, encoding="utf-8")
    return path


def create_backup(cfg: AppConfig, label: str = "") -> Path:
    """创建完整备份，返回备份目录。"""
    store = Store(cfg)
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}" + (f"_{label}" if label else "")
    backup_dir = BACKUP_ROOT / name
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 核心文件
    copied: list[str] = []
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, backup_dir / "config.json")
        copied.append("config.json")

    db_src = Path(cfg.db_path)
    if not db_src.is_absolute():
        db_src = ROOT / db_src
    if db_src.exists():
        # WAL 模式下不能直接 copy 文件：未 checkpoint 的事务在 -wal 里，
        # 只复制主库会得到一个索引页损坏的库。用 SQLite backup API 生成一致性快照。
        dst_db = backup_dir / "messages.db"
        src_conn = sqlite3.connect(str(db_src))
        dst_conn = sqlite3.connect(str(dst_db))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        with sqlite3.connect(str(dst_db)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份数据库完整性校验失败")
        copied.append("messages.db")

    # 可读导出
    exports = export_readable(store, backup_dir / "export", cfg)
    for p in exports.values():
        copied.append(f"export/{p.name}")

    # 清单
    stats = store.stats()
    make_migration_guide(backup_dir, cfg, stats)

    manifest = {
        "version": VERSION,
        "created_at": int(time.time()),
        "created_at_readable": _fmt(int(time.time())),
        "files": {f: _sha256(backup_dir / f) for f in copied},
        "stats": stats,
        "message_count": sum(stats.values()),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 指向最新备份
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    LATEST_POINTER.write_text(
        json.dumps({"latest": name, "created_at": manifest["created_at"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return backup_dir


def resolve_backup(name: str) -> Path:
    if name in ("latest", ""):
        if not LATEST_POINTER.exists():
            raise FileNotFoundError("没有找到任何备份")
        name = json.loads(LATEST_POINTER.read_text(encoding="utf-8"))["latest"]
    path = BACKUP_ROOT / name
    if not path.exists():
        raise FileNotFoundError(f"备份不存在：{path}")
    return path


def verify_backup(backup_dir: Path) -> tuple[bool, list[str]]:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        return False, ["缺少 manifest.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for rel, expected in manifest.get("files", {}).items():
        p = backup_dir / rel
        if not p.exists():
            problems.append(f"缺失：{rel}")
        elif _sha256(p) != expected:
            problems.append(f"校验失败（文件被改动）：{rel}")
    return (not problems), problems


def restore_backup(name: str, target_root: Path = ROOT) -> tuple[bool, str]:
    """从备份恢复配置与数据库。返回 (成功与否, 说明)。"""
    backup_dir = resolve_backup(name)
    ok, problems = verify_backup(backup_dir)
    if not ok:
        return False, "备份校验未通过：" + "；".join(problems)

    restored = []
    # 覆盖前先把现状存一份，防止误操作
    pre = create_backup(AppConfig(), label="restore前")
    restored.append(f"覆盖前现状已存至 {pre.name}")

    src_cfg = backup_dir / "config.json"
    if src_cfg.exists():
        shutil.copy2(src_cfg, target_root / "config.json")
        restored.append("config.json 已恢复")
    src_db = backup_dir / "messages.db"
    if src_db.exists():
        shutil.copy2(src_db, target_root / "messages.db")
        restored.append("messages.db 已恢复")

    guide = backup_dir / "MIGRATION_GUIDE.md"
    if guide.exists():
        restored.append(f"迁移清单：{guide}")
    return True, "\n".join(restored)


def list_backups() -> list[dict]:
    if not BACKUP_ROOT.exists():
        return []
    result = []
    for d in sorted(BACKUP_ROOT.iterdir()):
        if not d.is_dir() or not (d / "manifest.json").exists():
            continue
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        result.append({
            "name": d.name,
            "created": m.get("created_at_readable", ""),
            "messages": m.get("message_count", 0),
            "version": m.get("version", ""),
        })
    return result
