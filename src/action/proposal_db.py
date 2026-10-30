"""SQLite база данных откликов, кандидатов и runtime-состояния."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from loguru import logger

from src.paths import PROPOSALS_DB_FILE


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _outbox_now() -> datetime:
    """Clock seam for deterministic retry/lease tests."""
    return datetime.now(timezone.utc)


def _outbox_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


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
        conn.execute("PRAGMA foreign_keys = ON")
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
            conn.execute("DROP TABLE IF EXISTS stats")
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
                    offers_count INTEGER DEFAULT 0,
                    client_hired_percent INTEGER DEFAULT 0,
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account_registration_id TEXT NOT NULL DEFAULT '',
                    remote_dialog_id TEXT,
                    project_title TEXT,
                    status TEXT DEFAULT 'new',
                    last_message_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    sender TEXT NOT NULL,
                    message_text TEXT,
                    platform_message_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_msg_dedup
                ON conversation_messages(conversation_id, platform_message_id)
                WHERE platform_message_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS earnings (
                    earning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER,
                    project_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    amount REAL,
                    currency TEXT DEFAULT 'RUB',
                    status TEXT DEFAULT 'pending',
                    paid_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_unique_candidate
                ON earnings(candidate_id) WHERE status IN ('pending', 'paid') AND candidate_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_user_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(client_user_id, platform)
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
                ("offers_count", "INTEGER DEFAULT 0"),
                ("client_hired_percent", "INTEGER DEFAULT 0"),
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

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_notification_outbox (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL DEFAULT 'candidate_notification',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'leased', 'delivered', 'dead', 'cancelled')),
                    payload TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(candidate_id, event_type)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candidate_notification_outbox_due
                ON candidate_notification_outbox(status, available_at, lease_until, event_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candidate_notification_outbox_delivered
                ON candidate_notification_outbox(status, delivered_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_event_journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    candidate_id INTEGER NOT NULL,
                    event_id INTEGER,
                    event_kind TEXT NOT NULL,
                    candidate_status TEXT,
                    delivery_status TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_event_journal_candidate ON candidate_event_journal(candidate_id, seq)"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO runtime_state(state_key, state_value, updated_at)
                VALUES ('candidate_events.stream_id', ?, ?)
                """,
                (uuid.uuid4().hex, _now()),
            )

            self._ensure_column(conn, "conversations", "account_registration_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "conversations", "remote_dialog_id", "TEXT")
            conn.execute("UPDATE conversations SET account_registration_id = '' WHERE account_registration_id IS NULL")
            conn.execute("DROP INDEX IF EXISTS idx_conv_project_platform")

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

    def insert_candidate_if_absent(self, project: Any, **fields: Any) -> tuple[int, bool]:
        """Atomically admit a discovery without overwriting an existing candidate.

        The unique (project_id, platform) constraint is the arbiter. A retry or
        another orchestrator cannot reset a queued, sent, or completed candidate
        to ``parsed`` merely because the page was fetched again.
        """
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
            "offers_count": int(getattr(project, "offers_count", 0) or 0),
            "client_hired_percent": int(getattr(project, "client_hired_percent", 0) or 0),
            "platform_data": _to_json(getattr(project, "platform_data", None)),
            "updated_at": _now(),
        }
        for key, value in fields.items():
            if key in {"skills", "vet_reasons", "vet_red_flags", "competitor_prices", "client_context", "platform_data"}:
                payload[key] = _to_json(value)
            else:
                payload[key] = value

        columns = list(payload)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            cur = conn.execute(
                f"INSERT INTO candidates ({', '.join(columns)}) VALUES ({placeholders}) "
                "ON CONFLICT(project_id, platform) DO NOTHING",
                tuple(payload[col] for col in columns),
            )
            inserted = cur.rowcount == 1
            row = conn.execute(
                "SELECT candidate_id FROM candidates WHERE project_id = ? AND platform = ?",
                (project.id, project.platform),
            ).fetchone()
            if row is None:
                raise RuntimeError("candidate was not found after conflict-safe insert")
            if inserted:
                candidate_status = str(payload.get("status") or "parsed")
                self._journal_candidate_event(
                    conn, candidate_id=int(row["candidate_id"]), event_id=None,
                    event_kind="candidate_status", candidate_status=candidate_status,
                    delivery_status=None,
                )
                if candidate_status in {"queued", "auto_ready"}:
                    self._insert_notification_event(
                        conn, candidate_id=int(row["candidate_id"]), candidate_status=candidate_status
                    )
            conn.commit()
            return int(row["candidate_id"]), inserted

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
            "offers_count": int(getattr(project, "offers_count", 0) or 0),
            "client_hired_percent": int(getattr(project, "client_hired_percent", 0) or 0),
            "platform_data": _to_json(getattr(project, "platform_data", None)),
            "updated_at": _now(),
        }
        for key, value in fields.items():
            if key in {
                "skills",
                "vet_reasons",
                "vet_red_flags",
                "competitor_prices",
                "client_context",
                "platform_data",
            }:
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
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT candidate_id, status FROM candidates WHERE project_id = ? AND platform = ?",
                (project.id, project.platform),
            ).fetchone()
            conn.execute(sql, tuple(payload[col] for col in columns))
            row = conn.execute(
                "SELECT candidate_id FROM candidates WHERE project_id = ? AND platform = ?",
                (project.id, project.platform),
            ).fetchone()
            candidate_id = int(row["candidate_id"])
            if previous is None or ("status" in payload and str(payload["status"]) != previous["status"]):
                new_status = str(payload.get("status") or "parsed") if previous is None else str(payload["status"])
                self._journal_candidate_event(
                    conn, candidate_id=candidate_id, event_id=None,
                    event_kind="candidate_status", candidate_status=new_status,
                    delivery_status=None,
                )
                if new_status in {"queued", "auto_ready"}:
                    self._insert_notification_event(
                        conn, candidate_id=candidate_id, candidate_status=new_status
                    )
            conn.commit()
            return candidate_id

    def update_candidate(self, candidate_id: int, **fields: Any) -> None:
        if not fields:
            return
        prepared = {}
        for key, value in fields.items():
            if key in {
                "skills",
                "vet_reasons",
                "vet_red_flags",
                "competitor_prices",
                "client_context",
                "platform_data",
            }:
                prepared[key] = _to_json(value)
            else:
                prepared[key] = value
        prepared["updated_at"] = _now()
        sets = ", ".join(f"{key} = ?" for key in prepared.keys())
        with self._connect() as conn:
            old_status = None
            if "status" in prepared:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
                ).fetchone()
                old_status = row["status"] if row else None
            conn.execute(
                f"UPDATE candidates SET {sets} WHERE candidate_id = ?",
                tuple(prepared.values()) + (candidate_id,),
            )
            if old_status is not None and old_status != prepared.get("status"):
                new_status = str(prepared["status"])
                self._journal_candidate_event(
                    conn, candidate_id=candidate_id, event_id=None,
                    event_kind="candidate_status", candidate_status=new_status,
                    delivery_status=None,
                )
                if new_status in {"queued", "auto_ready"}:
                    self._insert_notification_event(
                        conn, candidate_id=candidate_id, candidate_status=new_status
                    )
            conn.commit()

    def claim_candidate_for_sending(self, candidate_id: int) -> bool:
        """Atomic CAS: claim candidate for sending. Returns True if claimed, False if already taken.

        Compare-and-set: UPDATE status='sending' WHERE status IN ('queued','auto_ready').
        Prevents double-send on concurrent approve (Telegram double-tap, HTTP+Telegram, etc).
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE candidates SET status = 'sending', updated_at = ? WHERE candidate_id = ? AND status IN ('queued', 'auto_ready')",
                (_now(), candidate_id),
            )
            if cur.rowcount:
                self._journal_candidate_event(
                    conn, candidate_id=candidate_id, event_id=None,
                    event_kind="candidate_status", candidate_status="sending",
                    delivery_status=None,
                )
            conn.commit()
            return cur.rowcount > 0

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
        enqueue_notification: bool = True,
        **extra_fields: Any,
    ) -> None:
        self._set_candidate_status_with_outbox(
            candidate_id, status, actor=actor, reason=reason, record_action=record_action,
            enqueue_notification=enqueue_notification and status in {"queued", "auto_ready"},
            extra_fields=extra_fields,
        )

    def enqueue_candidate_notification(
        self,
        candidate_id: int,
        status: str,
        *,
        actor: str = "system",
        reason: Optional[str] = None,
        record_action: bool = True,
        **extra_fields: Any,
    ) -> tuple[int | None, bool]:
        """Atomically change candidate status and create one durable notification.

        Returns (event_id, newly_inserted). A candidate gets at most one
        notification event over its lifetime, including after historical
        cleanup and across process restarts.
        """
        if status not in {"queued", "auto_ready"}:
            raise ValueError("candidate notifications require queued or auto_ready status")
        return self._set_candidate_status_with_outbox(
            candidate_id, status, actor=actor, reason=reason, record_action=record_action,
            enqueue_notification=True, extra_fields=extra_fields,
        )

    def _outbox_stream_id(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT state_value FROM runtime_state WHERE state_key = 'candidate_events.stream_id'"
        ).fetchone()
        if row is None:
            stream_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO runtime_state(state_key, state_value, updated_at) VALUES (?, ?, ?)",
                ("candidate_events.stream_id", stream_id, _now()),
            )
            return stream_id
        return str(row["state_value"])

    def _journal_candidate_event(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_id: int,
        event_id: int | None,
        event_kind: str,
        candidate_status: str | None,
        delivery_status: str | None,
        detail: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO candidate_event_journal(
                stream_id, candidate_id, event_id, event_kind,
                candidate_status, delivery_status, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._outbox_stream_id(conn), candidate_id, event_id, event_kind,
                candidate_status, delivery_status, (detail or "")[:500],
                timestamp or _outbox_timestamp(_outbox_now()),
            ),
        )

    def _insert_notification_event(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_id: int,
        candidate_status: str,
        timestamp: str | None = None,
    ) -> tuple[int, bool]:
        stamp = timestamp or _outbox_timestamp(_outbox_now())
        cursor = conn.execute(
            """
            INSERT INTO candidate_notification_outbox(
                candidate_id, event_type, status, payload, available_at, created_at, updated_at
            ) VALUES (?, 'candidate_notification', 'pending', ?, ?, ?, ?)
            ON CONFLICT(candidate_id, event_type) DO NOTHING
            """,
            (
                candidate_id,
                _to_json({"candidate_id": candidate_id, "candidate_status": candidate_status}),
                stamp, stamp, stamp,
            ),
        )
        inserted = cursor.rowcount == 1
        event = conn.execute(
            """
            SELECT event_id FROM candidate_notification_outbox
            WHERE candidate_id = ? AND event_type = 'candidate_notification'
            """,
            (candidate_id,),
        ).fetchone()
        event_id = int(event["event_id"])
        if inserted:
            self._journal_candidate_event(
                conn, candidate_id=candidate_id, event_id=event_id,
                event_kind="notification", candidate_status=candidate_status,
                delivery_status="pending", timestamp=stamp,
            )
        return event_id, inserted

    def _set_candidate_status_with_outbox(
        self,
        candidate_id: int,
        status: str,
        *,
        actor: str,
        reason: str | None,
        record_action: bool,
        enqueue_notification: bool,
        extra_fields: dict[str, Any],
    ) -> tuple[int | None, bool]:
        fields = dict(extra_fields)
        fields["status"] = status
        fields["last_actor"] = actor
        if reason is not None:
            fields["decision_reason"] = reason
        if status in {"auto_sent", "manual_sent", "draft"}:
            fields.setdefault("sent_at", _now())
        prepared: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"skills", "vet_reasons", "vet_red_flags", "competitor_prices", "client_context", "platform_data"}:
                prepared[key] = _to_json(value)
            else:
                prepared[key] = value
        prepared["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in prepared)
        timestamp = _outbox_timestamp(_outbox_now())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if current is None:
                return None, False
            previous_status = str(current["status"])
            conn.execute(
                f"UPDATE candidates SET {assignments} WHERE candidate_id = ?",
                (*prepared.values(), candidate_id),
            )
            if record_action:
                conn.execute(
                    "INSERT INTO candidate_actions(candidate_id, action, actor, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, status, actor, _to_json({"reason": reason, **fields}), _now()),
                )
            if previous_status != status:
                self._journal_candidate_event(
                    conn, candidate_id=candidate_id, event_id=None,
                    event_kind="candidate_status", candidate_status=status,
                    delivery_status=None, detail=reason, timestamp=timestamp,
                )
            event_id: int | None = None
            inserted = False
            if enqueue_notification:
                event_id, inserted = self._insert_notification_event(
                    conn, candidate_id=candidate_id, candidate_status=status, timestamp=timestamp,
                )
            conn.commit()
            return event_id, inserted

    def claim_due_notifications(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 60,
        max_attempts: int = 5,
    ) -> list[dict[str, Any]]:
        """Claim due events with a transaction-wide SQLite write lock.

        Each lease issues a fresh token; stale workers cannot ACK or fail a
        newer claim. Expired leases are eligible for retry up to max_attempts.
        """
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("lease_seconds and max_attempts must be positive")
        limit = max(0, min(int(limit), 100))
        if not limit:
            return []
        now = _outbox_now()
        stamp = _outbox_timestamp(now)
        until = _outbox_timestamp(now + timedelta(seconds=lease_seconds))
        claimed: list[dict[str, Any]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Previous worker could crash after the final allowed attempt.
            expired = conn.execute(
                """
                SELECT o.event_id, o.candidate_id, c.status AS candidate_status
                FROM candidate_notification_outbox AS o
                JOIN candidates AS c ON c.candidate_id = o.candidate_id
                WHERE o.attempts >= ?
                  AND (o.status = 'pending'
                       OR (o.status = 'leased' AND o.lease_until <= ?))
                """,
                (max_attempts, stamp),
            ).fetchall()
            if expired:
                conn.execute(
                    """
                    UPDATE candidate_notification_outbox
                    SET status = 'dead', lease_owner = NULL, lease_token = NULL,
                        lease_until = NULL, updated_at = ?,
                        last_error = coalesce(last_error, 'lease expired at max attempts')
                    WHERE attempts >= ?
                      AND (status = 'pending'
                           OR (status = 'leased' AND lease_until <= ?))
                    """,
                    (stamp, max_attempts, stamp),
                )
                for event in expired:
                    self._journal_candidate_event(
                        conn, candidate_id=int(event["candidate_id"]), event_id=int(event["event_id"]),
                        event_kind="notification", candidate_status=event["candidate_status"],
                        delivery_status="failed", detail="max attempts exhausted", timestamp=stamp,
                    )

            due = conn.execute(
                """
                SELECT o.*, c.status AS candidate_status
                FROM candidate_notification_outbox AS o
                JOIN candidates AS c ON c.candidate_id = o.candidate_id
                WHERE o.attempts < ? AND o.available_at <= ?
                  AND (
                    o.status = 'pending'
                    OR (o.status = 'leased' AND o.lease_until <= ?)
                  )
                ORDER BY o.available_at, o.event_id LIMIT ?
                """,
                (max_attempts, stamp, stamp, limit),
            ).fetchall()
            for event in due:
                token = uuid.uuid4().hex
                attempts = int(event["attempts"]) + 1
                conn.execute(
                    """
                    UPDATE candidate_notification_outbox
                    SET status = 'leased', attempts = ?, lease_owner = ?,
                        lease_token = ?, lease_until = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (attempts, worker_id, token, until, stamp, event["event_id"]),
                )
                self._journal_candidate_event(
                    conn, candidate_id=int(event["candidate_id"]), event_id=int(event["event_id"]),
                    event_kind="notification", candidate_status=event["candidate_status"],
                    delivery_status="leased", timestamp=stamp,
                )
                record = dict(event)
                record.update(
                    status="leased", attempts=attempts, lease_owner=worker_id,
                    lease_token=token, lease_until=until, updated_at=stamp,
                    payload=_from_json(event["payload"], {}),
                )
                claimed.append(record)
            conn.commit()
        return claimed

    def _finish_notification(
        self,
        event_id: int,
        lease_token: str,
        *,
        worker_id: str,
        target_status: str,
        error: str | None = None,
        base_delay_seconds: float = 2,
        max_delay_seconds: float = 300,
        max_attempts: int = 5,
    ) -> bool:
        if target_status not in {"delivered", "pending", "dead", "cancelled"}:
            raise ValueError("invalid notification status")
        if not lease_token or not worker_id:
            return False
        if base_delay_seconds < 0 or max_delay_seconds < 0 or max_attempts < 1:
            raise ValueError("backoff limits must be nonnegative and max_attempts positive")
        now = _outbox_now()
        stamp = _outbox_timestamp(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = conn.execute(
                """
                SELECT o.*, c.status AS candidate_status
                FROM candidate_notification_outbox AS o
                JOIN candidates AS c ON c.candidate_id = o.candidate_id
                WHERE o.event_id = ? AND o.status = 'leased'
                  AND o.lease_owner = ? AND o.lease_token = ?
                """,
                (event_id, worker_id, lease_token),
            ).fetchone()
            if event is None:
                return False
            final_status = target_status
            delay = 0.0
            if target_status == "pending":
                if int(event["attempts"]) >= max_attempts:
                    final_status = "dead"
                else:
                    delay = min(
                        max_delay_seconds, base_delay_seconds * (2 ** min(int(event["attempts"]) - 1, 30))
                    )
            next_time = _outbox_timestamp(now + timedelta(seconds=delay))
            conn.execute(
                """
                UPDATE candidate_notification_outbox
                SET status = ?, available_at = ?, lease_owner = NULL, lease_token = NULL,
                    lease_until = NULL, updated_at = ?, last_error = ?,
                    delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END
                WHERE event_id = ?
                """,
                (
                    final_status, next_time, stamp, (error or "")[:1000] if error is not None else None,
                    final_status, stamp, event_id,
                ),
            )
            state = {
                "delivered": "provider_accepted",
                "pending": "retry",
                "dead": "failed",
                "cancelled": "cancelled",
            }[final_status]
            self._journal_candidate_event(
                conn, candidate_id=int(event["candidate_id"]), event_id=int(event_id),
                event_kind="notification", candidate_status=event["candidate_status"],
                delivery_status=state, detail=error, timestamp=stamp,
            )
            conn.commit()
            return True

    def ack_notification(self, event_id: int, lease_token: str, *, worker_id: str) -> bool:
        """ACK only a still-owned event after provider accepted the message."""
        return self._finish_notification(
            event_id, lease_token, worker_id=worker_id, target_status="delivered",
        )

    def fail_notification(
        self,
        event_id: int,
        lease_token: str,
        error: str,
        *,
        worker_id: str,
        base_delay_seconds: float = 2,
        max_delay_seconds: float = 300,
        max_attempts: int = 5,
    ) -> bool:
        return self._finish_notification(
            event_id, lease_token, worker_id=worker_id, target_status="pending",
            error=error, base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds, max_attempts=max_attempts,
        )

    def cancel_notification(
        self, event_id: int, lease_token: str, *, worker_id: str, reason: str = "candidate no longer eligible"
    ) -> bool:
        """Mark a claimed notification obsolete when candidate status changed."""
        return self._finish_notification(
            event_id, lease_token, worker_id=worker_id, target_status="cancelled", error=reason,
        )

    def cleanup_notification_history(self, retention_days: int = 30) -> int:
        """Scrub completed details and prune journal older than retention.

        Deleting a completed outbox row would allow an old candidate to be
        notified again. This preserves those identity tombstones; journal seqs
        remain monotonic, and replay clients detect retention gaps via
        get_candidate_events_since(...).reset_required.

        Returns the number of outbox records scrubbed.
        """
        if retention_days < 0:
            raise ValueError("retention_days must be nonnegative")
        cutoff = _outbox_timestamp(_outbox_now() - timedelta(days=retention_days))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE candidate_notification_outbox
                SET payload = NULL, last_error = NULL
                WHERE status IN ('delivered', 'dead', 'cancelled')
                  AND updated_at < ?
                  AND (payload IS NOT NULL OR last_error IS NOT NULL)
                """,
                (cutoff,),
            )
            scrubbed = int(cursor.rowcount)
            conn.execute(
                "DELETE FROM candidate_event_journal WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
            return scrubbed

    def get_notification_summary(self) -> dict[str, Any]:
        now = _outbox_timestamp(_outbox_now())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM candidate_notification_outbox GROUP BY status"
            ).fetchall()
            due = conn.execute(
                """
                SELECT COUNT(*) AS count FROM candidate_notification_outbox
                WHERE (status = 'pending' OR (status = 'leased' AND lease_until <= ?))
                  AND available_at <= ?
                """,
                (now, now),
            ).fetchone()
            return {
                **{status: next((int(row["count"]) for row in rows if row["status"] == status), 0)
                   for status in ("pending", "leased", "delivered", "dead", "cancelled")},
                "due": int(due["count"]),
            }

    def get_candidate_events_since(self, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read a replayable journal, marking cursor gaps after retention.

        A client should discard its prior cursor if stream_id changes, and
        refresh its snapshot if reset_required is True.
        """
        limit = max(1, min(int(limit), 500))
        cursor = max(0, int(after_seq))
        with self._connect() as conn:
            stream_id = self._outbox_stream_id(conn)
            first = conn.execute("SELECT MIN(seq) AS earliest FROM candidate_event_journal").fetchone()
            if first["earliest"] is not None:
                earliest_seq = int(first["earliest"])
            else:
                history = conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'candidate_event_journal'"
                ).fetchone()
                earliest_seq = (int(history["seq"]) + 1) if history is not None else 1
            reset_required = cursor != 0 and cursor < earliest_seq - 1
            rows = conn.execute(
                """
                SELECT seq, stream_id, candidate_id, event_id, event_kind,
                       candidate_status, delivery_status, detail, created_at
                FROM candidate_event_journal WHERE seq > ?
                ORDER BY seq LIMIT ?
                """,
                (max(cursor, earliest_seq - 1), limit),
            ).fetchall()
            items = [dict(row) for row in rows]
            return {
                "stream_id": stream_id,
                "items": items,
                "earliest_seq": earliest_seq,
                "reset_required": reset_required,
                "next_cursor": items[-1]["seq"] if items else max(cursor, earliest_seq - 1),
            }

    def get_candidate(self, candidate_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

    def find_candidate_for_reply(
        self,
        platform: str,
        username: str,
        *,
        project_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return the best sent candidate matching an inbox reply.

        Kwork inbox payloads do not always contain a project identifier.  A
        project match is therefore preferred when present, with a stable
        username-only fallback ordered by most recently sent candidate.
        """
        normalized_platform = platform.strip()
        normalized_username = username.strip()
        if not normalized_platform or not normalized_username:
            return None

        with self._connect() as conn:
            if project_id:
                row = conn.execute(
                    """
                    SELECT * FROM candidates
                    WHERE platform = ?
                      AND project_id = ?
                      AND lower(coalesce(client_username, '')) = lower(?)
                    ORDER BY candidate_id DESC
                    LIMIT 1
                    """,
                    (normalized_platform, project_id, normalized_username),
                ).fetchone()
                if row:
                    return self._row_to_candidate(row)

            row = conn.execute(
                """
                SELECT * FROM candidates
                WHERE platform = ?
                  AND lower(coalesce(client_username, '')) = lower(?)
                ORDER BY
                    CASE WHEN sent_at IS NULL OR sent_at = '' THEN 1 ELSE 0 END,
                    datetime(sent_at) DESC,
                    candidate_id DESC
                LIMIT 1
                """,
                (normalized_platform, normalized_username),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

    def link_reply_to_candidate(
        self,
        candidate_id: int,
        *,
        reply_text: str,
        replied_at: Optional[str] = None,
        actor: str = "inbox_monitor",
        overwrite: bool = False,
    ) -> bool:
        """Persist the first inbound reply for a candidate exactly once."""
        text = reply_text.strip()
        if not text:
            return False

        timestamp = replied_at or _now()
        with self._connect() as conn:
            where = "candidate_id = ?"
            if not overwrite:
                where += " AND (reply_text IS NULL OR trim(reply_text) = '')"
            cursor = conn.execute(
                f"""
                UPDATE candidates
                SET replied_at = ?, reply_text = ?, updated_at = ?
                WHERE {where}
                """,
                (timestamp, text, _now(), candidate_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                INSERT INTO candidate_actions (candidate_id, action, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    "reply_linked",
                    actor,
                    _to_json({"reply_text": text[:200], "replied_at": timestamp}),
                    _now(),
                ),
            )
            conn.commit()
        return True

    def set_reply_classification(
        self,
        candidate_id: int,
        classification: str,
        *,
        actor: str = "reply_classifier",
    ) -> bool:
        """Store a classifier result and retain an audit event."""
        value = classification.strip()
        if not value:
            return False

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE candidates
                SET reply_classification = ?, reply_classified_at = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (value, _now(), _now(), candidate_id),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                INSERT INTO candidate_actions (candidate_id, action, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (candidate_id, "reply_classified", actor, _to_json({"classification": value}), _now()),
            )
            conn.commit()
        return True

    def set_candidate_outcome(
        self,
        candidate_id: int,
        *,
        won: Optional[bool] = None,
        revenue: Optional[float] = None,
        actor: str = "operator",
    ) -> bool:
        """Persist an optional win/loss outcome without changing send state."""
        if won is None and revenue is None:
            return False

        assignments: list[str] = []
        values: list[Any] = []
        if won is not None:
            assignments.append("won = ?")
            values.append(int(won))
        if revenue is not None:
            assignments.append("revenue = ?")
            values.append(float(revenue))
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(candidate_id)

        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE candidates SET {', '.join(assignments)} WHERE candidate_id = ?",
                tuple(values),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """
                INSERT INTO candidate_actions (candidate_id, action, actor, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    "outcome_recorded",
                    actor,
                    _to_json({"won": won, "revenue": revenue}),
                    _now(),
                ),
            )
            conn.commit()
        return True

    def get_candidate_by_project(self, project_id: str, platform: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE project_id = ? AND platform = ?",
                (project_id, platform),
            ).fetchone()
            return self._row_to_candidate(row) if row else None

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
            conn.execute("BEGIN IMMEDIATE")
            due = conn.execute(
                """
                SELECT candidate_id FROM candidates
                WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?
                """,
                (_now(),),
            ).fetchall()
            cur = conn.execute(
                """
                UPDATE candidates
                SET status = 'queued', snoozed_until = NULL, updated_at = ?
                WHERE status = 'snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?
                """,
                (_now(), _now()),
            )
            for row in due:
                self._journal_candidate_event(
                    conn, candidate_id=int(row["candidate_id"]), event_id=None,
                    event_kind="candidate_status", candidate_status="queued",
                    delivery_status=None, detail="snooze expired",
                )
            conn.commit()
            return cur.rowcount or 0

    def recover_stale_sending(self, max_age_minutes: int = 10) -> int:
        """Re-queue candidates stuck in 'sending' status for too long.

        If the process crashes after claim_candidate_for_sending but before
        the status is moved to a terminal value, the candidate is stranded.
        This method recovers them by setting status back to 'queued'.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale = conn.execute(
                """
                SELECT candidate_id FROM candidates
                WHERE status = 'sending' AND updated_at <= ?
                """,
                (cutoff,),
            ).fetchall()
            cur = conn.execute(
                "UPDATE candidates SET status = 'queued', updated_at = ? WHERE status = 'sending' AND updated_at <= ?",
                (_now(), cutoff),
            )
            for row in stale:
                self._journal_candidate_event(
                    conn, candidate_id=int(row["candidate_id"]), event_id=None,
                    event_kind="candidate_status", candidate_status="queued",
                    delivery_status=None, detail="stale sending recovered",
                )
            conn.commit()
            recovered = cur.rowcount or 0
            if recovered:
                logger.warning(f"ProposalDB: восстановлено {recovered} кандидатов из stale 'sending'")
            return recovered

    def blacklist_client(self, client_user_id: str, platform: str, reason: str = "") -> bool:
        """Add client to blacklist."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO client_blacklist (client_user_id, platform, reason, created_at) VALUES (?, ?, ?, ?)",
                    (str(client_user_id), platform, reason, _now()),
                )
                conn.commit()
                return True
        except Exception:
            return False

    def is_client_blacklisted(self, client_user_id: str, platform: str) -> bool:
        """Check if client is blacklisted."""
        if not client_user_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM client_blacklist WHERE client_user_id = ? AND platform = ?",
                (str(client_user_id), platform),
            ).fetchone()
            return row is not None

    def get_blacklisted_clients(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM client_blacklist ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def remove_from_blacklist(self, client_user_id: str, platform: str) -> bool:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM client_blacklist WHERE client_user_id = ? AND platform = ?",
                (str(client_user_id), platform),
            )
            conn.commit()
            return True

    def snooze_candidate(self, candidate_id: int, minutes: int, actor: str = "telegram") -> None:
        from datetime import timezone

        until = datetime.now(timezone.utc).timestamp() + max(minutes, 1) * 60
        snoozed_until = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.update_candidate_status(
            candidate_id,
            "snoozed",
            actor=actor,
            reason=f"snoozed for {minutes} minutes",
            snoozed_until=snoozed_until,
        )

    def get_candidate_status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS total FROM candidates GROUP BY status").fetchall()
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
                ) * CASE
                    WHEN last_used_at IS NULL THEN 0.5
                    WHEN last_used_at >= datetime('now', '-7 days') THEN 1.0
                    WHEN last_used_at >= datetime('now', '-30 days') THEN 0.7
                    WHEN last_used_at >= datetime('now', '-90 days') THEN 0.4
                    ELSE 0.2
                END AS rank_score
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
        from datetime import timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
                "SELECT id, platform, query_text FROM proposals WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE proposals SET response = ? WHERE id = ?",
                    (response_text, row["id"]),
                )
                candidate = conn.execute(
                    "SELECT candidate_id FROM candidates WHERE project_id = ? AND platform = ?",
                    (project_id, row["platform"]),
                ).fetchone()
                if candidate:
                    conn.execute(
                        """
                        INSERT INTO candidate_actions (candidate_id, action, actor, payload, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            candidate["candidate_id"],
                            "client_responded",
                            "inbox",
                            _to_json({"response": response_text[:200]}),
                            _now(),
                        ),
                    )
                conn.commit()
            else:
                return

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
    # Conversations
    # ------------------------------------------------------------------

    def get_or_create_conversation(
        self,
        project_id: str,
        platform: str,
        *,
        candidate_id: int | None = None,
        project_title: str = "",
        account_registration_id: str | None = None,
        remote_dialog_id: str | None = None,
    ) -> int:
        """Get one account-bound conversation or create it idempotently."""
        account_scope = (account_registration_id or "").strip()
        dialog_scope = (remote_dialog_id or "").strip() or None
        with self._connect() as conn:
            if dialog_scope is not None:
                row = conn.execute(
                    """
                    SELECT conversation_id FROM conversations
                    WHERE platform = ? AND account_registration_id = ? AND remote_dialog_id = ?
                    """,
                    (platform, account_scope, dialog_scope),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT conversation_id FROM conversations
                    WHERE project_id = ? AND platform = ? AND account_registration_id = ?
                    """,
                    (project_id, platform, account_scope),
                ).fetchone()
            if row:
                return int(row["conversation_id"])
            now = _now()
            conn.execute(
                """
                INSERT OR IGNORE INTO conversations (
                    candidate_id, project_id, platform, account_registration_id, remote_dialog_id,
                    project_title, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
                """,
                (candidate_id, project_id, platform, account_scope, dialog_scope, project_title, now, now),
            )
            conn.commit()
            if dialog_scope is not None:
                row = conn.execute(
                    """
                    SELECT conversation_id FROM conversations
                    WHERE platform = ? AND account_registration_id = ? AND remote_dialog_id = ?
                    """,
                    (platform, account_scope, dialog_scope),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT conversation_id FROM conversations
                    WHERE project_id = ? AND platform = ? AND account_registration_id = ?
                    """,
                    (project_id, platform, account_scope),
                ).fetchone()
            return int(row["conversation_id"])

    def add_conversation_message(
        self,
        conversation_id: int,
        *,
        sender: str,
        message_text: str,
        platform_message_id: str | None = None,
        created_at: str | None = None,
    ) -> bool:
        """Добавить сообщение в диалог. Возвращает True если новое, False если дубликат."""
        now = created_at or _now()
        with self._connect() as conn:
            if platform_message_id:
                existing = conn.execute(
                    "SELECT message_id FROM conversation_messages WHERE conversation_id = ? AND platform_message_id = ?",
                    (conversation_id, platform_message_id),
                ).fetchone()
                if existing:
                    return False
            try:
                conn.execute(
                    """
                    INSERT INTO conversation_messages (conversation_id, sender, message_text, platform_message_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (conversation_id, sender, message_text, platform_message_id, now),
                )
            except sqlite3.IntegrityError:
                return False
            if sender == "customer":
                conn.execute(
                    """
                    UPDATE conversations SET last_message_at = ?, updated_at = ?, status = 'awaiting_reply'
                    WHERE conversation_id = ?
                    """,
                    (now, now, conversation_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE conversations SET last_message_at = ?, updated_at = ?, status = 'replied'
                    WHERE conversation_id = ?
                    """,
                    (now, now, conversation_id),
                )
            conn.commit()
            return True

    def update_conversation_status(self, conversation_id: int, status: str) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE conversation_id = ?",
                (status, now, conversation_id),
            )
            conn.commit()

    def get_conversation(
        self,
        project_id: str,
        platform: str,
        *,
        account_registration_id: str | None = None,
    ) -> Optional[dict[str, Any]]:
        account_scope = (account_registration_id or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM conversations
                WHERE project_id = ? AND platform = ? AND account_registration_id = ?
                """,
                (project_id, platform, account_scope),
            ).fetchone()
            if not row:
                return None
            conv = dict(row)
            msgs = conn.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv["conversation_id"],),
            ).fetchall()
            conv["messages"] = [dict(m) for m in msgs]
            return conv

    def get_conversation_by_remote_dialog(
        self,
        platform: str,
        account_registration_id: str,
        remote_dialog_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return one conversation using its account-scoped remote identity."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM conversations
                WHERE platform = ? AND account_registration_id = ? AND remote_dialog_id = ?
                """,
                (platform, account_registration_id.strip(), remote_dialog_id.strip()),
            ).fetchone()
            if row is None:
                return None
            conv = dict(row)
            messages = conn.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv["conversation_id"],),
            ).fetchall()
            conv["messages"] = [dict(message) for message in messages]
            return conv

    def get_active_conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM conversations
                WHERE status NOT IN ('completed', 'declined')
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_conversations_since(self, since_ts: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, cm.message_text, cm.sender, cm.created_at AS message_created_at
                FROM conversations c
                LEFT JOIN conversation_messages cm ON cm.conversation_id = c.conversation_id
                WHERE c.updated_at >= ?
                ORDER BY c.updated_at DESC, cm.created_at ASC
                LIMIT ?
                """,
                (since_ts, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Lifecycle: hired / completed / paid
    # ------------------------------------------------------------------

    def mark_candidate_hired(self, candidate_id: int, actor: str = "system") -> None:
        self.update_candidate_status(candidate_id, "hired", actor=actor, reason="client hired")
        candidate = self.get_candidate(candidate_id)
        if candidate:
            self.update_conversation_status_by_candidate(candidate_id, "confirmed")

    def mark_candidate_declined(self, candidate_id: int, actor: str = "system", reason: str = "") -> None:
        self.update_candidate_status(candidate_id, "declined", actor=actor, reason=reason or "client declined")
        self.update_conversation_status_by_candidate(candidate_id, "declined")

    def mark_candidate_completed(self, candidate_id: int, actor: str = "system") -> None:
        self.update_candidate_status(candidate_id, "completed", actor=actor, reason="work delivered")
        self.update_conversation_status_by_candidate(candidate_id, "completed")

    def update_conversation_status_by_candidate(self, candidate_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE candidate_id = ?",
                (status, _now(), candidate_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Earnings
    # ------------------------------------------------------------------

    def record_earning(
        self,
        *,
        candidate_id: int | None = None,
        project_id: str,
        platform: str,
        amount: float,
        currency: str = "RUB",
        status: str = "pending",
    ) -> int:
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO earnings (candidate_id, project_id, platform, amount, currency, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (candidate_id, project_id, platform, amount, currency, status, now, now),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def update_earning_status(self, earning_id: int, status: str, *, paid_at: str | None = None) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE earnings SET status = ?, paid_at = ?, updated_at = ? WHERE earning_id = ?",
                (status, paid_at or (now if status == "paid" else None), now, earning_id),
            )
            conn.commit()

    def get_earnings_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) AS paid_amount,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0) AS pending_amount,
                    COALESCE(SUM(amount), 0) AS total_amount
                FROM earnings
                """
            ).fetchone()
            if not row:
                return {
                    "total": 0,
                    "paid_count": 0,
                    "pending_count": 0,
                    "paid_amount": 0,
                    "pending_amount": 0,
                    "total_amount": 0,
                }
            return {
                "total": int(row["total"] or 0),
                "paid_count": int(row["paid_count"] or 0),
                "pending_count": int(row["pending_count"] or 0),
                "paid_amount": float(row["paid_amount"] or 0),
                "pending_amount": float(row["pending_amount"] or 0),
                "total_amount": float(row["total_amount"] or 0),
            }

    def get_earnings(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM earnings ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Conversion funnel
    # ------------------------------------------------------------------

    def get_recent_proposal_texts(self, limit: int = 20) -> list[str]:
        """Get recent proposal texts for dedup check."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT proposal_text FROM proposals WHERE proposal_text IS NOT NULL AND proposal_text != '' ORDER BY sent_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [row["proposal_text"] for row in rows if row["proposal_text"]]

    def get_conversion_funnel(self, days: int = 30) -> dict[str, int]:
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            sent = conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE status IN ('auto_sent', 'manual_sent', 'hired', 'declined', 'completed') AND sent_at IS NOT NULL AND sent_at >= ?",
                (cutoff,),
            ).fetchone()
            responded = conn.execute(
                """
                SELECT COUNT(DISTINCT c.candidate_id) AS n
                FROM candidates c
                JOIN conversations conv ON conv.candidate_id = c.candidate_id
                WHERE c.status IN ('auto_sent', 'manual_sent', 'hired', 'declined', 'completed')
                  AND conv.status != 'new'
                  AND c.sent_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            hired = conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE status = 'hired' AND updated_at >= ?", (cutoff,)
            ).fetchone()
            completed = conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE status = 'completed' AND updated_at >= ?", (cutoff,)
            ).fetchone()
            paid = conn.execute(
                "SELECT COUNT(*) AS n FROM earnings WHERE status = 'paid' AND created_at >= ?", (cutoff,)
            ).fetchone()
            return {
                "sent": int(sent["n"] or 0) if sent else 0,
                "responded": int(responded["n"] or 0) if responded else 0,
                "hired": int(hired["n"] or 0) if hired else 0,
                "completed": int(completed["n"] or 0) if completed else 0,
                "paid": int(paid["n"] or 0) if paid else 0,
            }

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
