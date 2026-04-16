"""
Структурированное логирование в SQLite.
Заменяет текстовые логи на запросы к БД с аналитикой.
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
from loguru import logger


class LogDB:
    """База данных для логов и аналитики."""
    
    def __init__(self, db_path: str = "data/logs.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Логи парсинга
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parse_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    projects_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    duration_ms INTEGER
                )
            """)
            
            # Логи фильтрации
            conn.execute("""
                CREATE TABLE IF NOT EXISTS filter_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    input_count INTEGER,
                    output_count INTEGER,
                    details TEXT
                )
            """)
            
            # Логи отправки
            conn.execute("""
                CREATE TABLE IF NOT EXISTS send_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    duration_ms INTEGER
                )
            """)
            
            # Ошибки
            conn.execute("""
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    module TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    stack_trace TEXT
                )
            """)
            
            conn.commit()
    
    def log_parse(self, platform: str, status: str, projects_count: int = 0, 
                  error_message: str = None, duration_ms: int = None):
        """Залогировать результат парсинга."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO parse_logs 
                   (timestamp, platform, status, projects_count, error_message, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), platform, status, projects_count, 
                 error_message, duration_ms)
            )
            conn.commit()
    
    def log_filter(self, stage: str, input_count: int, output_count: int, details: str = None):
        """Залогировать результат фильтрации."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO filter_logs 
                   (timestamp, stage, input_count, output_count, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), stage, input_count, output_count, details)
            )
            conn.commit()
    
    def log_send(self, platform: str, project_id: str, status: str, 
                 error_message: str = None, duration_ms: int = None):
        """Залогировать результат отправки."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO send_logs 
                   (timestamp, platform, project_id, status, error_message, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), platform, project_id, status, 
                 error_message, duration_ms)
            )
            conn.commit()
    
    def log_error(self, module: str, error_type: str, message: str, stack_trace: str = None):
        """Залогировать ошибку."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO errors 
                   (timestamp, module, error_type, message, stack_trace)
                   VALUES (?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), module, error_type, message, stack_trace)
            )
            conn.commit()
    
    def get_parse_stats(self, days: int = 7) -> List[Dict[str, Any]]:
        """Статистика парсинга по платформам."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT 
                    platform,
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    AVG(projects_count) as avg_projects,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count
                FROM parse_logs
                WHERE timestamp >= datetime('now', '-{} days')
                GROUP BY platform
            """.format(days)).fetchall()
            return [dict(row) for row in rows]
    
    def get_error_stats(self, days: int = 7) -> List[Dict[str, Any]]:
        """Статистика ошибок по модулям."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT 
                    module,
                    error_type,
                    COUNT(*) as count
                FROM errors
                WHERE timestamp >= datetime('now', '-{} days')
                GROUP BY module, error_type
                ORDER BY count DESC
            """.format(days)).fetchall()
            return [dict(row) for row in rows]
    
    def get_success_rate(self, days: int = 7) -> float:
        """Общий процент успеха отправок."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT 
                    CAST(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*)
                FROM send_logs
                WHERE timestamp >= datetime('now', '-{} days')
            """.format(days)).fetchone()
            return round(row[0] * 100, 2) if row and row[0] else 0.0


# Глобальный инстанс
_log_db: Optional[LogDB] = None


def get_log_db() -> LogDB:
    global _log_db
    if _log_db is None:
        _log_db = LogDB()
    return _log_db
