"""Sliding-window rate limiting for per-user tool calls, persisted in SQLite."""

import logging
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger("cyberyinyu")

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "tool_rate_limits.sqlite3"


class HourlyLimiter:
    """Counts one user's usage of a feature inside a sliding time window.

    Usage is stored on disk so a bot restart does not hand out a fresh quota.
    """

    def __init__(self, feature: str, db_path: Path = DEFAULT_DB, window_seconds: int = 3600) -> None:
        self.feature = feature
        self.db_path = db_path
        self.window_seconds = max(60, int(window_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    feature TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_calls_feature_user "
                "ON tool_calls(feature, user_id, created_at)"
            )

    def try_consume(self, user_id: str, limit: int) -> tuple[bool, int, int]:
        """Reserve one call.

        Returns (allowed, used_count, retry_after_seconds). The reservation is
        rejected rather than granted when the store cannot be written, so a
        broken database cannot silently remove a paid feature's cost control.
        """
        now = time.time()
        cutoff = now - self.window_seconds
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM tool_calls WHERE created_at < ?", (cutoff,)
                )
                used, oldest = connection.execute(
                    "SELECT COUNT(*), MIN(created_at) FROM tool_calls "
                    "WHERE feature = ? AND user_id = ?",
                    (self.feature, user_id),
                ).fetchone()
                used = int(used or 0)
                if used >= limit:
                    retry_after = max(
                        1, int((float(oldest or now) + self.window_seconds) - now) + 1
                    )
                    connection.rollback()
                    return False, used, retry_after
                connection.execute(
                    "INSERT INTO tool_calls(id, feature, user_id, created_at) VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, self.feature, user_id, now),
                )
                connection.commit()
                return True, used + 1, 0
        except sqlite3.Error:
            logger.exception(
                "限流记录失败，已拒绝本次调用: feature=%s user=%s", self.feature, user_id
            )
            return False, 0, self.window_seconds

    def used(self, user_id: str) -> int:
        cutoff = time.time() - self.window_seconds
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) FROM tool_calls "
                    "WHERE feature = ? AND user_id = ? AND created_at >= ?",
                    (self.feature, user_id, cutoff),
                ).fetchone()
        except sqlite3.Error:
            logger.exception("读取限流计数失败: feature=%s user=%s", self.feature, user_id)
            return 0
        return int(row[0] if row else 0)


def format_retry_after(seconds: int) -> str:
    """Render a wait time the way a person would say it."""
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时"
