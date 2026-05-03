"""SQLite база данных откликов, кандидатов и runtime-состояния."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.paths import PROPOSALS_DB_FILE


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _from_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


class ProposalDB:
    """Операционная БД: отклики, очередь кандидатов, решения и runtime-state."""

    def __init__(self, db_path: str = str(PROPOSALS_DB_FILE)):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
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
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    sent_count INTEGER DEFAULT 0,
                    responded_count INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    budget REAL,
                    currency TEXT DEFAULT 'RUB',
                    skills TEXT,
                    url TEXT,
                    created_at TEXT,
                    search_query TEXT,
                    stage TEXT DEFAULT 'parsed',
                    status TEXT DEFAULT 'parsed',
                    execution_mode TEXT DEFAULT 'semi_auto',
                    ai_pre_score INTEGER,
                    ai_score INTEGER,
                    ai_score_source TEXT,
                    ai_reason TEXT,
                    vet_score INTEGER,
                    vet_passed INTEGER,
                    vet_reasons TEXT,
                    vet_red_flags TEXT,
                    decision_reason TEXT,
                    auto_eligible INTEGER DEFAULT 0,
                    risk_level TEXT DEFAULT 'medium',
                    priority INTEGER DEFAULT 0,
                    provider TEXT,
                    proposal_text TEXT,
                    competitor_prices TEXT,
                    client_context TEXT,
                    platform_data TEXT,
                    chosen_price TEXT,
                    manual_override INTEGER DEFAULT 0,
                    dry_run INTEGER DEFAULT 0,
                    last_actor TEXT DEFAULT 'system',
                    snoozed_until TEXT,
                    sent_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, platform)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_memory (
                    platform TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    runs INTEGER DEFAULT 0,
                    parsed_projects INTEGER DEFAULT 0,
                    shortlisted_count INTEGER DEFAULT 0,
                    auto_ready_count INTEGER DEFAULT 0,
                    sent_count INTEGER DEFAULT 0,
                    responded_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    user_preferred INTEGER DEFAULT 0,
                    last_used_at TEXT,
                    PRIMARY KEY (platform, query_text)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            for column, ddl in [
                ("candidate_id", "INTEGER"),
                ("decision_source", "TEXT"),
                ("ai_score", "INTEGER"),
                ("vet_score", "INTEGER"),
                ("decision_reason", "TEXT"),
                ("offers_count", "INTEGER DEFAULT 0"),
                ("client_hired_percent", "INTEGER DEFAULT 0"),
                ("provider", "TEXT"),
                ("query_text", "TEXT"),
                ("manual_override", "INTEGER DEFAULT 0"),
            ]:
                self._ensure_column(conn, "proposals", column, ddl)

            for column, ddl in [
                ("platform_data", "TEXT"),
                # Phase 3 — feedback loop fields. Все nullable / с defaults,
                # чтобы старые БД мигрировали идемпотентно.
                ("client_username", "TEXT"),
                ("replied_at", "TEXT"),
                ("reply_text", "TEXT"),
                ("reply_classification", "TEXT"),
                ("reply_classified_at", "TEXT"),
                ("won", "INTEGER DEFAULT 0"),
                ("revenue", "REAL"),
                ("prompt_variant", "TEXT"),
            ]:
                self._ensure_column(conn, "candidates", column, ddl)

            # Индекс для быстрого поиска кандидатов по нику клиента
            # (используется reply_linker, может тащить большие выборки).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_client_username "
                "ON candidates(platform, client_username)"
            )

            conn.commit()

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    def get_runtime_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?",
                (key,),
            ).fetchone()
            return row["state_value"] if row else default

    def set_runtime_state(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (key, str(value), _now()),
            )
            conn.commit()

    def get_runtime_mode(self, default: str = "semi_auto") -> str:
        return (self.get_runtime_state("execution_mode", default) or default).strip().lower()

    def set_runtime_mode(self, mode: str) -> None:
        self.set_runtime_state("execution_mode", mode)

    def set_platform_paused(self, platform: str, paused: bool) -> None:
        self.set_runtime_state(f"platform.pause.{platform}", "1" if paused else "0")

    def is_platform_paused(self, platform: str) -> bool:
        return self.get_runtime_state(f"platform.pause.{platform}", "0") == "1"

    def get_paused_platforms(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state_key FROM runtime_state
                WHERE state_key LIKE 'platform.pause.%' AND state_value = '1'
                ORDER BY state_key
                """
            ).fetchall()
            return [row["state_key"].split(".", 2)[-1] for row in rows]

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    def upsert_candidate(self, project: Any, **fields: Any) -> int:
        payload = {
            "project_id": project.id,
            "platform": project.platform,
            "title": project.title,
            "description": project.description,
            "budget": project.budget,
            "currency": project.currency,
            "skills": _to_json(project.skills or []),
            "url": project.url,
            "created_at": project.created_at,
            "search_query": getattr(project, "search_query", None),
            "platform_data": _to_json(getattr(project, "platform_data", None)),
            "updated_at": _now(),
        }
        for key, value in fields.items():
            if key in {"skills", "vet_reasons", "vet_red_flags", "competitor_prices", "client_context", "platform_data"}:
                payload[key] = _to_json(value)
            else:
                payload[key] = value

        columns = list(payload.keys())
        assignments = ", ".join(f"{col} = excluded.{col}" for col in columns if col not in {"project_id", "platform"})
        placeholders = ", ".join("?" for _ in columns)
        sql = f"""
            INSERT INTO candidates ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(project_id, platform) DO UPDATE SET
                {assignments}
        """
        with self._connect() as conn:
            conn.execute(sql, tuple(payload[col] for col in columns))
            row = conn.execute(
                "SELECT candidate_id FROM candidates WHERE project_id = ? AND platform = ?",
                (project.id, project.platform),
            ).fetchone()
            conn.commit()
            return int(row["candidate_id"])

    def update_candidate(self, candidate_id: int, **fields: Any) -> None:
        if not fields:
            return
        prepared = {}
        for key, value in fields.items():
            if key in {"skills", "vet_reasons", "vet_red_flags", "competitor_prices", "client_context", "platform_data"}:
                prepared[key] = _to_json(value)
            else:
                prepared[key] = value
        prepared["updated_at"] = _now()
        sets = ", ".join(f"{key} = ?" for key in prepared.keys())
        with self._connect() as conn:
            conn.execute(
                f"UPDATE candidates SET {sets} WHERE candidate_id = ?",
                tuple(prepared.values()) + (candidate_id,),
            )
            conn.commit()

    def record_candidate_action(
        self,
        candidate_id: int,
        action: str,
        actor: str = "system",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO candidate_actions (candidate_id, action, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (candidate_id, action, actor, _to_json(payload), _now()),
            )
            conn.commit()

    def update_candidate_status(
        self,
        candidate_id: int,
        status: str,
        *,
        actor: str = "system",
        reason: Optional[str] = None,
        record_action: bool = True,
        **extra_fields: Any,
    ) -> None:
        extra_fields["status"] = status
        extra_fields["last_actor"] = actor
        if reason is not None:
            extra_fields["decision_reason"] = reason
        if status in {"auto_sent", "manual_sent", "draft"}:
            extra_fields.setdefault("sent_at", _now())
        self.update_candidate(candidate_id, **extra_fields)
        if record_action:
            self.record_candidate_action(candidate_id, status, actor=actor, payload={"reason": reason, **extra_fields})

    def get_candidate(self, candidate_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

    def get_candidate_by_project(self, project_id: str, platform: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE project_id = ? AND platform = ?",
                (project_id, platform),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

    # ------------------------------------------------------------------
    # Phase 3 — feedback loop helpers
    # ------------------------------------------------------------------

    # Статусы, при которых имеет смысл связывать входящее сообщение с
    # кандидатом: отклик ушёл/готовится, либо мы уже в переписке.
    _REPLY_LINKABLE_STATUSES = (
        "auto_sent",
        "manual_sent",
        "draft",
        "queued",
        "auto_ready",
        "snoozed",
    )

    def find_candidate_for_reply(
        self,
        platform: str,
        username: str,
        project_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Найти кандидата для авто-связки реплая.

        Стратегия (от наиболее точного к наименее):
            1. (platform, project_id, client_username);
            2. (platform, client_username) среди отправленных/queued, latest first.

        Возвращает None если ни одного не нашлось — вызывающий должен
        корректно деградировать (например, оставить только notify).
        """
        username = (username or "").strip()
        if not platform or not username:
            return None

        placeholders = ", ".join("?" for _ in self._REPLY_LINKABLE_STATUSES)
        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    f"""
                    SELECT * FROM candidates
                    WHERE platform = ? AND project_id = ? AND client_username = ?
                          AND status IN ({placeholders})
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (platform, project_id, username, *self._REPLY_LINKABLE_STATUSES),
                ).fetchone()
                if row:
                    return self._row_to_candidate(row)

            row = conn.execute(
                f"""
                SELECT * FROM candidates
                WHERE platform = ? AND client_username = ?
                      AND status IN ({placeholders})
                ORDER BY
                    CASE WHEN sent_at IS NOT NULL THEN 0 ELSE 1 END,
                    sent_at DESC,
                    updated_at DESC
                LIMIT 1
                """,
                (platform, username, *self._REPLY_LINKABLE_STATUSES),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

    def link_reply_to_candidate(
        self,
        candidate_id: int,
        *,
        reply_text: str,
        replied_at: Optional[str] = None,
        actor: str = "inbox_monitor",
        record_action: bool = True,
        overwrite: bool = False,
    ) -> bool:
        """Сохранить факт реплая клиента на кандидате.

        По умолчанию НЕ перетирает уже сохранённый первый реплай —
        мы хотим зафиксировать именно «первое касание». Возвращает
        True если поля действительно были обновлены.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT replied_at, reply_text FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if not row:
                return False
            already_linked = bool(row["replied_at"] or row["reply_text"])
            if already_linked and not overwrite:
                return False

        ts = replied_at or _now()
        text = (reply_text or "").strip()[:4000]
        self.update_candidate(
            candidate_id,
            replied_at=ts,
            reply_text=text,
        )
        if record_action:
            self.record_candidate_action(
                candidate_id,
                "customer_replied",
                actor=actor,
                payload={"reply_text": text[:280], "replied_at": ts},
            )
        return True

    def set_reply_classification(
        self,
        candidate_id: int,
        classification: str,
        *,
        actor: str = "reply_classifier",
        record_action: bool = True,
    ) -> None:
        classification = (classification or "").strip().lower()
        if not classification:
            return
        self.update_candidate(
            candidate_id,
            reply_classification=classification,
            reply_classified_at=_now(),
        )
        if record_action:
            self.record_candidate_action(
                candidate_id,
                "reply_classified",
                actor=actor,
                payload={"classification": classification},
            )

    def set_candidate_outcome(
        self,
        candidate_id: int,
        *,
        won: Optional[bool] = None,
        revenue: Optional[float] = None,
        actor: str = "system",
        record_action: bool = True,
    ) -> None:
        """Зафиксировать финальный исход (взяли/не взяли, доход)."""
        fields: dict[str, Any] = {}
        if won is not None:
            fields["won"] = 1 if won else 0
        if revenue is not None:
            fields["revenue"] = float(revenue)
        if not fields:
            return
        self.update_candidate(candidate_id, **fields)
        if record_action:
            self.record_candidate_action(
                candidate_id,
                "outcome_recorded",
                actor=actor,
                payload={"won": won, "revenue": revenue},
            )

    def get_candidates_by_status(
        self,
        statuses: Iterable[str],
        *,
        limit: int = 50,
        include_snoozed: bool = False,
    ) -> list[dict[str, Any]]:
        status_list = list(statuses)
        placeholders = ", ".join("?" for _ in status_list)
        query = f"""
            SELECT * FROM candidates
            WHERE status IN ({placeholders})
        """
        params: list[Any] = list(status_list)
        if not include_snoozed:
            query += " AND (snoozed_until IS NULL OR snoozed_until <= ?)"
            params.append(_now())
        query += " ORDER BY priority DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [self._row_to_candidate(row) for row in rows]

    def get_queue(self, page: int = 0, page_size: int = 5) -> list[dict[str, Any]]:
        offset = max(page, 0) * max(page_size, 1)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM candidates
                WHERE status IN ('queued', 'snoozed')
                ORDER BY
                    CASE status WHEN 'queued' THEN 0 ELSE 1 END,
                    priority DESC,
                    updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
            return [self._row_to_candidate(row) for row in rows]

    def count_candidates(self, statuses: Optional[Iterable[str]] = None) -> int:
        with self._connect() as conn:
            if statuses:
                status_list = list(statuses)
                placeholders = ", ".join("?" for _ in status_list)
                row = conn.execute(
                    f"SELECT COUNT(*) AS total FROM candidates WHERE status IN ({placeholders})",
                    tuple(status_list),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS total FROM candidates").fetchone()
            return int(row["total"]) if row else 0

    def requeue_due_candidates(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE candidates
                SET status = 'queued', snoozed_until = NULL, updated_at = ?
                WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?
                """,
                (_now(), _now()),
            )
            conn.commit()
            return cur.rowcount or 0

    def snooze_candidate(self, candidate_id: int, minutes: int, actor: str = "telegram") -> None:
        until = datetime.now().timestamp() + max(minutes, 1) * 60
        snoozed_until = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")
        self.update_candidate_status(
            candidate_id,
            "snoozed",
            actor=actor,
            reason=f"snoozed for {minutes} minutes",
            snoozed_until=snoozed_until,
        )

    def get_candidate_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM candidates GROUP BY status"
            ).fetchall()
            return {row["status"]: int(row["total"]) for row in rows}

    def get_candidate_actions_since(self, since_ts: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.created_at, a.action, a.actor, a.payload, c.title, c.platform, c.project_id
                FROM candidate_actions a
                JOIN candidates c ON c.candidate_id = a.candidate_id
                WHERE a.created_at > ?
                ORDER BY a.created_at ASC
                LIMIT ?
                """,
                (since_ts, limit),
            ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = _from_json(item.get("payload"), {})
                out.append(item)
            return out

    # ------------------------------------------------------------------
    # Query learning
    # ------------------------------------------------------------------

    def record_query_run(self, platform: str, query_text: str, parsed_projects: int) -> None:
        if not query_text:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO query_memory (platform, query_text, runs, parsed_projects, last_used_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(platform, query_text) DO UPDATE SET
                    runs = runs + 1,
                    parsed_projects = parsed_projects + excluded.parsed_projects,
                    last_used_at = excluded.last_used_at
                """,
                (platform, query_text, parsed_projects, _now()),
            )
            conn.commit()

    def record_query_signal(self, platform: str, query_text: str, signal: str, amount: int = 1) -> None:
        if not query_text:
            return
        allowed = {
            "shortlisted": "shortlisted_count",
            "auto_ready": "auto_ready_count",
            "sent": "sent_count",
            "responded": "responded_count",
            "skipped": "skipped_count",
        }
        column = allowed.get(signal)
        if not column:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO query_memory (platform, query_text, last_used_at, runs, parsed_projects, shortlisted_count, auto_ready_count, sent_count, responded_count, skipped_count)
                VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0)
                ON CONFLICT(platform, query_text) DO NOTHING
                """,
                (platform, query_text, _now()),
            )
            conn.execute(
                f"""
                UPDATE query_memory
                SET {column} = {column} + ?, last_used_at = ?
                WHERE platform = ? AND query_text = ?
                """,
                (amount, _now(), platform, query_text),
            )
            conn.commit()

    def mark_query_preferred(self, platform: str, query_text: str, preferred: bool = True) -> None:
        if not query_text:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO query_memory (platform, query_text, user_preferred, last_used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(platform, query_text) DO UPDATE SET
                    user_preferred = excluded.user_preferred,
                    last_used_at = excluded.last_used_at
                """,
                (platform, query_text, 1 if preferred else 0, _now()),
            )
            conn.commit()

    def get_query_memory(
        self,
        platform: str,
        *,
        limit: int = 20,
        preferred_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                platform,
                query_text,
                runs,
                parsed_projects,
                shortlisted_count,
                auto_ready_count,
                sent_count,
                responded_count,
                skipped_count,
                user_preferred,
                last_used_at,
                (
                    user_preferred * 100
                    + responded_count * 20
                    + sent_count * 8
                    + shortlisted_count * 4
                    + auto_ready_count * 2
                    - skipped_count * 2
                    - CASE WHEN runs > 0 AND shortlisted_count = 0 AND sent_count = 0 THEN MIN(runs, 5) ELSE 0 END
                ) AS rank_score
            FROM query_memory
            WHERE platform = ?
        """
        params: list[Any] = [platform]
        if preferred_only:
            query += " AND user_preferred = 1"
        query += " ORDER BY rank_score DESC, responded_count DESC, sent_count DESC, last_used_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Proposals
    # ------------------------------------------------------------------

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
        *,
        candidate_id: Optional[int] = None,
        decision_source: Optional[str] = None,
        ai_score: Optional[int] = None,
        vet_score: Optional[int] = None,
        decision_reason: Optional[str] = None,
        offers_count: int = 0,
        client_hired_percent: int = 0,
        provider: Optional[str] = None,
        query_text: Optional[str] = None,
        manual_override: bool = False,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO proposals
                    (project_id, platform, title, budget, skills, proposal_text, status, sent_at, url,
                     candidate_id, decision_source, ai_score, vet_score, decision_reason,
                     offers_count, client_hired_percent, provider, query_text, manual_override)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    platform,
                    title,
                    budget,
                    skills,
                    proposal_text,
                    status,
                    _now(),
                    url,
                    candidate_id,
                    decision_source,
                    ai_score,
                    vet_score,
                    decision_reason,
                    offers_count,
                    client_hired_percent,
                    provider,
                    query_text,
                    1 if manual_override else 0,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_sent_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_platform_stats(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    platform,
                    COUNT(*) AS total,
                    COUNT(CASE WHEN response IS NOT NULL AND response != '' THEN 1 END) AS responded
                FROM proposals
                GROUP BY platform
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def count_today_sent(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM proposals WHERE sent_at LIKE ?",
                (f"{today}%",),
            ).fetchone()
            return int(row["total"]) if row else 0

    def get_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT platform) AS platforms FROM proposals"
            ).fetchone()
            return {
                "total_sent": int(row["total"]) if row else 0,
                "platforms_used": int(row["platforms"]) if row else 0,
                "today_sent": self.count_today_sent(),
                "queue_count": self.count_candidates(["queued", "snoozed"]),
            }

    def mark_response(self, project_id: str, response_text: str) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT platform, query_text FROM proposals WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            conn.execute(
                "UPDATE proposals SET response = ? WHERE project_id = ?",
                (response_text, project_id),
            )
            conn.commit()

        if row and row["query_text"]:
            self.record_query_signal(row["platform"], row["query_text"], "responded")

    # ------------------------------------------------------------------
    # Digest helpers
    # ------------------------------------------------------------------

    def build_digest(self, since_ts: str, limit: int = 50) -> dict[str, Any]:
        actions = self.get_candidate_actions_since(since_ts, limit=limit)
        counts: dict[str, int] = {}
        lines: list[str] = []
        for item in actions:
            action = item["action"]
            counts[action] = counts.get(action, 0) + 1
            title = (item["title"] or "")[:55]
            lines.append(f"{item['created_at']} [{item['platform']}] {action}: {title}")
        return {"counts": counts, "lines": lines}

    # ------------------------------------------------------------------
    # Row helpers
    # ------------------------------------------------------------------

    def _row_to_candidate(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["skills"] = _from_json(item.get("skills"), [])
        item["vet_reasons"] = _from_json(item.get("vet_reasons"), [])
        item["vet_red_flags"] = _from_json(item.get("vet_red_flags"), [])
        item["competitor_prices"] = _from_json(item.get("competitor_prices"), [])
        item["client_context"] = _from_json(item.get("client_context"), {})
        item["platform_data"] = _from_json(item.get("platform_data"), {})
        item["manual_override"] = bool(item.get("manual_override", 0))
        item["auto_eligible"] = bool(item.get("auto_eligible", 0))
        item["vet_passed"] = bool(item.get("vet_passed", 0))
        item["dry_run"] = bool(item.get("dry_run", 0))
        item["won"] = bool(item.get("won", 0))
        return item
