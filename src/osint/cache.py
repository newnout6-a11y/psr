"""SQLite-кэш OSINT-находок. Не парсим одного и того же человека дважды."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from src.paths import OSINT_CACHE_DB_FILE


class OSINTCache:
    """Кэш результатов OSINT с TTL."""

    def __init__(self, db_path: str = str(OSINT_CACHE_DB_FILE), ttl_days: int = 14):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.ttl = timedelta(days=ttl_days)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS osint_results (
                    key         TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data, updated_at FROM osint_results WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            ts = datetime.fromisoformat(row[1])
            if datetime.now() - ts > self.ttl:
                return None
            return json.loads(row[0])
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug(f"OSINTCache: {key} не распарсился: {e}")
            return None

    def set(self, key: str, data: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO osint_results (key, data, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(data, ensure_ascii=False, default=str), datetime.now().isoformat()),
            )
            conn.commit()

    def clear_expired(self) -> int:
        """Удалить протухшие записи. Возвращает число удалённых."""
        cutoff = (datetime.now() - self.ttl).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM osint_results WHERE updated_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
