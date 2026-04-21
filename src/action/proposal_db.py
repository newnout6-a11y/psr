"""
SQLite база данных для отслеживания отправленных откликов.
"""

import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from src.paths import PROPOSALS_DB_FILE


class ProposalDB:
    """
    База данных отправленных откликов.
    """
    
    def __init__(self, db_path: str = str(PROPOSALS_DB_FILE)):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    budget TEXT,
                    skills TEXT,
                    proposal_text TEXT NOT NULL,
                    status TEXT DEFAULT 'sent',
                    sent_at TEXT NOT NULL,
                    response TEXT,
                    url TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    sent_count INTEGER DEFAULT 0,
                    responded_count INTEGER DEFAULT 0
                )
            """)
            conn.commit()
    
    def save_proposal(
        self,
        project_id: str,
        platform: str,
        title: str,
        proposal_text: str,
        budget: Optional[str] = None,
        skills: Optional[str] = None,
        url: Optional[str] = None,
        status: str = "sent",
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO proposals
                   (project_id, platform, title, budget, skills, proposal_text, status, sent_at, url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id, platform, title,
                    budget, skills, proposal_text,
                    status,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    url,
                ),
            )
            conn.commit()
            return cursor.lastrowid
    
    def get_sent_proposals(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Получить отправленные отклики."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
    
    def get_platform_stats(self) -> List[Dict[str, Any]]:
        """Статистика по платформам."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT platform, COUNT(*) as total, 
                   COUNT(CASE WHEN response IS NOT NULL THEN 1 END) as responded
                   FROM proposals GROUP BY platform"""
            ).fetchall()
            return [dict(row) for row in rows]
    
    def count_today_sent(self) -> int:
        """Сколько отправлено сегодня."""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE sent_at LIKE ?",
                (f"{today}%",),
            ).fetchone()
            return row[0] if row else 0
    
    def get_summary(self) -> Dict[str, Any]:
        """Общая сводка."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, COUNT(DISTINCT platform) as platforms FROM proposals"
            ).fetchone()
            return {
                "total_sent": row[0] if row else 0,
                "platforms_used": row[1] if row else 0,
                "today_sent": self.count_today_sent(),
            }

    def mark_response(self, project_id: str, response_text: str):
        """Отметить ответ от заказчика."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE proposals SET response = ? WHERE project_id = ?",
                (response_text, project_id)
            )
            conn.commit()
