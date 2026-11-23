"""本地 SQLite 存储：消息、分级结果、结构化信息、留存策略、历史查询。

数据库升级会自动迁移旧表（level: urgent→P0 / important→P1 / normal→P2 / noise→P3），
不丢历史数据。迁移前请先用 `python main.py backup` 备份。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .models import ExtractedInfo, LEGACY_LEVEL_MAP, Message, Verdict

if TYPE_CHECKING:
    from .config import AppConfig

COLUMNS = [
    "category TEXT DEFAULT '待核实'",
    "title TEXT DEFAULT '待核实'",
    "event_time TEXT DEFAULT '待核实'",
    "deadline TEXT DEFAULT '待核实'",
    "quota_limit TEXT DEFAULT '待核实'",
    "needs_action TEXT DEFAULT '待核实'",
    "action_required TEXT DEFAULT '待核实'",
    "importance TEXT DEFAULT '待核实'",
    "urgency TEXT DEFAULT '待核实'",
    "confidence TEXT DEFAULT '待核实'",
    "original_id TEXT DEFAULT ''",
    "is_duplicate INTEGER DEFAULT 0",
    "created_at INTEGER DEFAULT 0",
]

SELECT_CORE = (
    "SELECT id,sender_id,sender_name,content,group_id,group_name,"
    "ts,is_private,level,score,reason,notified,digested,"
    "category,title,event_time,deadline,quota_limit,needs_action,"
    "action_required,importance,urgency,confidence,original_id,is_duplicate,created_at "
    "FROM messages"
)


class Store:
    def __init__(self, cfg: AppConfig):
        self.path = Path(cfg.db_path)
        if not self.path.is_absolute():
            self.path = cfg.db_path and Path(__file__).resolve().parent.parent / cfg.db_path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    ts INTEGER,
                    sender_id TEXT,
                    sender_name TEXT,
                    group_id TEXT,
                    group_name TEXT,
                    content TEXT,
                    is_private INTEGER,
                    level TEXT,
                    score INTEGER,
                    reason TEXT,
                    notified INTEGER DEFAULT 0,
                    digested INTEGER DEFAULT 0
                )
            """)
            existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            for col_def in COLUMNS:
                name = col_def.split()[0]
                if name not in existing:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {col_def}")
            # 旧 level 值迁移到 P0-P3
            for old, new in LEGACY_LEVEL_MAP.items():
                conn.execute("UPDATE messages SET level=? WHERE level=?", (new, old))
            conn.execute("UPDATE messages SET created_at=? WHERE created_at=0 OR created_at IS NULL",
                         (int(time.time()),))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_level ON messages(level)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON messages(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON messages(created_at)")

    @staticmethod
    def _row_to_pair(row) -> tuple[Message, Verdict]:
        msg = Message(
            message_id=row[0], sender_id=row[1], sender_name=row[2], content=row[3],
            group_id=row[4], group_name=row[5], timestamp=row[6], is_private=bool(row[7]),
        )
        info = ExtractedInfo(
            category=row[13] or "待核实", title=row[14] or "待核实",
            event_time=row[15] or "待核实", deadline=row[16] or "待核实",
            quota_limit=row[17] or "待核实", needs_action=row[18] or "待核实",
            action_required=row[19] or "待核实", importance=row[20] or "待核实",
            urgency=row[21] or "待核实", confidence=row[22] or "待核实",
            original_id=row[23] or "",
        )
        verdict = Verdict(
            level=row[8] or "P3", score=row[9] or 0, reason=row[10] or "",
            needs_notify=bool(row[11]), needs_digest=bool(row[12]),
            info=info, is_duplicate=bool(row[24]),
        )
        return msg, verdict

    def insert(self, msg: Message, verdict: Verdict, notified: bool = False) -> None:
        info = verdict.info
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO messages
                (id, ts, sender_id, sender_name, group_id, group_name, content,
                 is_private, level, score, reason, notified, digested,
                 category, title, event_time, deadline, quota_limit, needs_action,
                 action_required, importance, urgency, confidence, original_id,
                 is_duplicate, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                msg.message_id, msg.timestamp, msg.sender_id, msg.sender_name,
                msg.group_id, msg.group_name, msg.content, int(msg.is_private),
                verdict.level, verdict.score, verdict.reason, int(notified),
                info.category, info.title, info.event_time, info.deadline,
                info.quota_limit, info.needs_action, info.action_required,
                info.importance, info.urgency, info.confidence, info.original_id,
                int(verdict.is_duplicate), int(time.time()),
            ))

    def pending_digest(self, levels: list[str] | None = None) -> list[tuple[Message, Verdict]]:
        levels = levels or ["P0", "P1", "P2"]
        placeholders = ",".join("?" for _ in levels)
        with self._connect() as conn:
            rows = conn.execute(
                f"{SELECT_CORE} WHERE digested=0 AND level IN ({placeholders}) ORDER BY ts",
                levels,
            ).fetchall()
        return [self._row_to_pair(r) for r in rows]

    def mark_digested(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany("UPDATE messages SET digested=1 WHERE id=?", [(i,) for i in ids])

    def all_messages(self) -> list[tuple[Message, Verdict]]:
        with self._connect() as conn:
            rows = conn.execute(f"{SELECT_CORE} ORDER BY ts").fetchall()
        return [self._row_to_pair(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT level, COUNT(*) FROM messages GROUP BY level").fetchall()
        return {r[0]: r[1] for r in rows}

    def recent(self, limit: int = 20) -> list[tuple[Message, Verdict]]:
        with self._connect() as conn:
            rows = conn.execute(f"{SELECT_CORE} ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_pair(r) for r in rows]

    def query(
        self,
        group: str = "",
        date: str = "",
        keyword: str = "",
        category: str = "",
        priority: str = "",
        limit: int = 100,
    ) -> list[tuple[Message, Verdict]]:
        """按群聊/日期/关键词/类别/优先级查询历史。日期格式 YYYY-MM-DD。"""
        clauses: list[str] = []
        params: list = []
        if group:
            clauses.append("(group_id LIKE ? OR group_name LIKE ?)")
            params += [f"%{group}%", f"%{group}%"]
        if date:
            # ts 以秒或毫秒存储，这里用字符串前缀匹配日期，兼容两种单位
            clauses.append("(strftime('%Y-%m-%d', ts, 'unixepoch')=? OR "
                           "strftime('%Y-%m-%d', ts/1000, 'unixepoch')=?)")
            params += [date, date]
        if keyword:
            clauses.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if category:
            clauses.append("category LIKE ?")
            params.append(f"%{category}%")
        if priority:
            clauses.append("level=?")
            params.append(priority.upper())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"{SELECT_CORE}{where} ORDER BY ts DESC LIMIT ?", params + [limit]
            ).fetchall()
        return [self._row_to_pair(r) for r in rows]

    def cleanup(self, days: int = 3, important_days: int = 60,
                dry_run: bool = False) -> int:
        """分级清理：P2/P3 按 days，P0/P1 按 important_days（0 表示永久保留）。
        返回删除条数。"""
        now = int(time.time())
        conditions = ["(level IN ('P2','P3') AND created_at<?)"]
        params: list = [now - days * 86400]
        if important_days and important_days > 0:
            conditions.append("(level IN ('P0','P1') AND created_at<?)")
            params.append(now - important_days * 86400)
        where = " OR ".join(conditions)
        with self._connect() as conn:
            if dry_run:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {where}", params
                ).fetchone()
                return int(row[0])
            cur = conn.execute(f"DELETE FROM messages WHERE {where}", params)
            return cur.rowcount
