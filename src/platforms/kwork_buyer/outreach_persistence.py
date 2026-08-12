"""Durable SQLite implementation of the Buyer outreach persistence boundary.

The store persists promotion snapshots, immutable proposal versions, preflight
evidence, and the explicit outbox state used by :mod:`outreach_service`.  It
has no Kwork client and no method that can send a proposal.  A caller must
provide an explicit database path so Buyer Search data is never silently
attached to a process-global session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .attachments import (
    BuyerAttachmentContext,
    BuyerAttachmentMetadata,
    BuyerAttachmentOmission,
    BuyerAttachmentParseResult,
    BuyerAttachmentParseStatus,
    BuyerAttachmentType,
)
from .outreach import (
    BuyerProposalDraft,
    BuyerProposalPreflight,
    BuyerProposalSendIntent,
    BuyerProposalSendState,
)
from .outreach_service import (
    BuyerOutreachConfirmation,
    BuyerOutreachPromotion,
    BuyerOutreachStore,
)
from .proposal_composer import (
    BuyerComposedProposalDraft,
    BuyerProposalContextManifest,
    BuyerProposalPreflightDiagnostic,
    BuyerProposalPreflightResult,
)


class BuyerOutreachPersistenceError(RuntimeError):
    """Base exception raised by the durable Buyer outreach store."""


class BuyerOutreachPersistenceConflictError(BuyerOutreachPersistenceError):
    """Raised when an immutable row would be changed or reassigned."""


class BuyerOutreachPersistenceNotFoundError(BuyerOutreachPersistenceError):
    """Raised when a dependent durable Buyer outreach row is absent."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_outreach_promotions (
    promotion_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    project_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform, run_id, project_id)
);

CREATE INDEX IF NOT EXISTS buyer_outreach_promotions_account_idx
    ON buyer_outreach_promotions(sender_account_registration_id, created_at DESC);

CREATE TABLE IF NOT EXISTS buyer_outreach_drafts (
    draft_id TEXT PRIMARY KEY,
    promotion_id TEXT NOT NULL REFERENCES buyer_outreach_promotions(promotion_id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_draft_id TEXT,
    context_hash TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(promotion_id, version)
);

CREATE INDEX IF NOT EXISTS buyer_outreach_drafts_promotion_idx
    ON buyer_outreach_drafts(promotion_id, version ASC, created_at ASC);

CREATE TABLE IF NOT EXISTS buyer_outreach_preflights (
    draft_id TEXT PRIMARY KEY REFERENCES buyer_outreach_drafts(draft_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buyer_outreach_send_intents (
    intent_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES buyer_outreach_drafts(draft_id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    context_hash TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS buyer_outreach_send_intents_draft_idx
    ON buyer_outreach_send_intents(draft_id, created_at ASC);

CREATE INDEX IF NOT EXISTS buyer_outreach_send_intents_account_idx
    ON buyer_outreach_send_intents(sender_account_registration_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS buyer_outreach_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES buyer_outreach_send_intents(intent_id) ON DELETE RESTRICT,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS buyer_outreach_confirmations_intent_idx
    ON buyer_outreach_confirmations(intent_id, confirmed_at ASC, confirmation_id ASC);

CREATE TABLE IF NOT EXISTS buyer_outreach_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS buyer_outreach_audit_events_project_idx
    ON buyer_outreach_audit_events(run_id, project_id, event_id ASC);
"""


_ALLOWED_INTENT_TRANSITIONS: dict[BuyerProposalSendState, frozenset[BuyerProposalSendState]] = {
    BuyerProposalSendState.DRAFT: frozenset({BuyerProposalSendState.PREFLIGHT}),
    BuyerProposalSendState.PREFLIGHT: frozenset({BuyerProposalSendState.PENDING_SEND, BuyerProposalSendState.FAILED}),
    BuyerProposalSendState.PENDING_SEND: frozenset({BuyerProposalSendState.SENDING}),
    BuyerProposalSendState.SENDING: frozenset(
        {BuyerProposalSendState.ACCEPTED, BuyerProposalSendState.UNKNOWN, BuyerProposalSendState.FAILED}
    ),
    BuyerProposalSendState.UNKNOWN: frozenset(
        {
            BuyerProposalSendState.ACCEPTED,
            BuyerProposalSendState.PENDING_SEND,
            BuyerProposalSendState.FAILED,
            BuyerProposalSendState.UNKNOWN,
        }
    ),
    BuyerProposalSendState.ACCEPTED: frozenset(),
    BuyerProposalSendState.FAILED: frozenset(),
}


class SQLiteBuyerOutreachStore(BuyerOutreachStore):
    """Account-bound, additive SQLite store for :class:`BuyerOutreachStore`.

    Connections are deliberately short lived.  This makes the store safe to
    inject alongside the Buyer Search repository while keeping its schema and
    lifecycle independent.  Every public operation initializes the explicit
    database path before using it.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the additive schema for the configured database path."""

        await self._ensure_initialized()

    async def close(self) -> None:
        """Close the store lifecycle; connections are opened per operation."""

        return None

    async def get_promotion(
        self,
        *,
        platform: str,
        run_id: str,
        project_id: str,
    ) -> BuyerOutreachPromotion | None:
        return await self._call(
            self._get_promotion_sync,
            _required_text(platform, "platform"),
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
        )

    async def create_promotion(self, promotion: BuyerOutreachPromotion) -> BuyerOutreachPromotion:
        if not isinstance(promotion, BuyerOutreachPromotion):
            raise TypeError("promotion must be a BuyerOutreachPromotion")
        return await self._call(self._create_promotion_sync, promotion)

    async def get_draft(self, draft_id: str) -> BuyerComposedProposalDraft | None:
        return await self._call(self._get_draft_sync, _required_text(draft_id, "draft_id"))

    async def list_drafts(self, *, promotion_id: str) -> Sequence[BuyerComposedProposalDraft]:
        return await self._call(self._list_drafts_sync, _required_text(promotion_id, "promotion_id"))

    async def create_draft(self, draft: BuyerComposedProposalDraft) -> BuyerComposedProposalDraft:
        if not isinstance(draft, BuyerComposedProposalDraft):
            raise TypeError("draft must be a BuyerComposedProposalDraft")
        return await self._call(self._create_draft_sync, draft)

    async def get_preflight(self, draft_id: str) -> BuyerProposalPreflightResult | None:
        return await self._call(self._get_preflight_sync, _required_text(draft_id, "draft_id"))

    async def save_preflight(
        self,
        draft_id: str,
        result: BuyerProposalPreflightResult,
    ) -> BuyerProposalPreflightResult:
        if not isinstance(result, BuyerProposalPreflightResult):
            raise TypeError("result must be a BuyerProposalPreflightResult")
        return await self._call(self._save_preflight_sync, _required_text(draft_id, "draft_id"), result)

    async def get_send_intent(self, intent_id: str) -> BuyerProposalSendIntent | None:
        return await self._call(self._get_send_intent_sync, _required_text(intent_id, "intent_id"))

    async def get_send_intent_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> BuyerProposalSendIntent | None:
        return await self._call(
            self._get_send_intent_by_idempotency_key_sync,
            _required_text(idempotency_key, "idempotency_key"),
        )

    async def create_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        if not isinstance(intent, BuyerProposalSendIntent):
            raise TypeError("intent must be a BuyerProposalSendIntent")
        return await self._call(self._create_send_intent_sync, intent)

    async def update_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        if not isinstance(intent, BuyerProposalSendIntent):
            raise TypeError("intent must be a BuyerProposalSendIntent")
        return await self._call(self._update_send_intent_sync, intent)

    async def record_confirmation(self, confirmation: BuyerOutreachConfirmation) -> BuyerOutreachConfirmation:
        if not isinstance(confirmation, BuyerOutreachConfirmation):
            raise TypeError("confirmation must be a BuyerOutreachConfirmation")
        return await self._call(self._record_confirmation_sync, confirmation)

    async def get_confirmation(self, confirmation_id: str) -> BuyerOutreachConfirmation | None:
        return await self._call(self._get_confirmation_sync, _required_text(confirmation_id, "confirmation_id"))

    async def append_audit_event(
        self,
        *,
        event_type: str,
        run_id: str,
        project_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        await self._call(
            self._append_audit_event_sync,
            _required_text(event_type, "event_type"),
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            _jsonable_mapping(payload, "payload"),
        )

    async def list_confirmations(self, *, intent_id: str) -> Sequence[BuyerOutreachConfirmation]:
        """Return confirmation evidence for one account-bound outbox item."""

        return await self._call(self._list_confirmations_sync, _required_text(intent_id, "intent_id"))

    async def list_audit_events(
        self,
        *,
        run_id: str,
        project_id: str,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Replay immutable audit evidence without exposing mutable internals."""

        return await self._call(
            self._list_audit_events_sync,
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            _non_negative_int(after_event_id, "after_event_id"),
            _bounded_limit(limit),
        )

    async def _call(self, function: Any, *args: Any) -> Any:
        await self._ensure_initialized()
        return await asyncio.to_thread(function, *args)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _get_promotion_sync(self, platform: str, run_id: str, project_id: str) -> BuyerOutreachPromotion | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM buyer_outreach_promotions
                WHERE platform = ? AND run_id = ? AND project_id = ?
                """,
                (platform, run_id, project_id),
            ).fetchone()
        return _promotion_from_row(row) if row is not None else None

    def _create_promotion_sync(self, promotion: BuyerOutreachPromotion) -> BuyerOutreachPromotion:
        payload = _promotion_payload(promotion)
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = connection.execute(
                """
                SELECT * FROM buyer_outreach_promotions
                WHERE platform = ? AND run_id = ? AND project_id = ?
                """,
                (promotion.platform, promotion.run_id, promotion.project_id),
            ).fetchone()
            if existing is not None:
                return _promotion_from_row(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_outreach_promotions (
                        promotion_id, platform, run_id, project_id,
                        sender_account_registration_id, project_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        promotion.promotion_id,
                        promotion.platform,
                        promotion.run_id,
                        promotion.project_id,
                        promotion.sender_account_registration_id,
                        promotion.project_hash,
                        _dump_json(payload),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerOutreachPersistenceConflictError(
                    "promotion identity is already bound to another record"
                ) from exc
        return promotion

    def _get_draft_sync(self, draft_id: str) -> BuyerComposedProposalDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_outreach_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return _draft_from_row(row) if row is not None else None

    def _list_drafts_sync(self, promotion_id: str) -> tuple[BuyerComposedProposalDraft, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_outreach_drafts
                WHERE promotion_id = ?
                ORDER BY version ASC, created_at ASC, draft_id ASC
                """,
                (promotion_id,),
            ).fetchall()
        return tuple(_draft_from_row(row) for row in rows)

    def _create_draft_sync(self, draft: BuyerComposedProposalDraft) -> BuyerComposedProposalDraft:
        payload = _draft_payload(draft)
        payload_json = _dump_json(payload)
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = connection.execute(
                "SELECT * FROM buyer_outreach_drafts WHERE draft_id = ?",
                (draft.draft_id,),
            ).fetchone()
            if existing is not None:
                stored = _draft_from_row(existing)
                if _dump_json(_draft_payload(stored)) != payload_json:
                    raise BuyerOutreachPersistenceConflictError(
                        "draft_id already belongs to a different immutable draft"
                    )
                return stored

            promotion_row = self._promotion_row_for_draft(connection, draft)
            promotion = _promotion_from_row(promotion_row)
            _validate_draft_promotion_binding(draft, promotion)

            same_version = connection.execute(
                """
                SELECT * FROM buyer_outreach_drafts
                WHERE promotion_id = ? AND version = ?
                """,
                (promotion.promotion_id, draft.version),
            ).fetchone()
            if same_version is not None:
                raise BuyerOutreachPersistenceConflictError(
                    "proposal version is already immutable for this promoted project"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_outreach_drafts (
                        draft_id, promotion_id, platform, run_id, project_id,
                        sender_account_registration_id, version, parent_draft_id,
                        context_hash, draft_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        promotion.promotion_id,
                        draft.draft.platform,
                        draft.draft.run_id,
                        draft.draft.project_id,
                        draft.sender_account_registration_id,
                        draft.version,
                        draft.parent_draft_id,
                        draft.draft.context_hash,
                        draft.draft.draft_hash,
                        payload_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerOutreachPersistenceConflictError("could not persist immutable proposal draft") from exc
        return draft

    def _get_preflight_sync(self, draft_id: str) -> BuyerProposalPreflightResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM buyer_outreach_preflights WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return _preflight_result_from_payload(_load_json_mapping(row["payload_json"], "preflight")) if row else None

    def _save_preflight_sync(
        self,
        draft_id: str,
        result: BuyerProposalPreflightResult,
    ) -> BuyerProposalPreflightResult:
        payload_json = _dump_json(result.to_payload())
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            draft = connection.execute(
                "SELECT draft_id FROM buyer_outreach_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise BuyerOutreachPersistenceNotFoundError(f"draft not found: {draft_id}")
            connection.execute(
                """
                INSERT INTO buyer_outreach_preflights (draft_id, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (draft_id, payload_json, now, now),
            )
        return result

    def _get_send_intent_sync(self, intent_id: str) -> BuyerProposalSendIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_outreach_send_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return _send_intent_from_row(row) if row is not None else None

    def _get_send_intent_by_idempotency_key_sync(self, idempotency_key: str) -> BuyerProposalSendIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_outreach_send_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _send_intent_from_row(row) if row is not None else None

    def _create_send_intent_sync(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        payload_json = _dump_json(intent.to_payload())
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing_by_key = connection.execute(
                "SELECT * FROM buyer_outreach_send_intents WHERE idempotency_key = ?",
                (intent.idempotency_key,),
            ).fetchone()
            if existing_by_key is not None:
                stored = _send_intent_from_row(existing_by_key)
                if not _same_send_identity(stored, intent):
                    raise BuyerOutreachPersistenceConflictError("idempotency key is bound to another proposal identity")
                return stored

            existing_by_id = connection.execute(
                "SELECT * FROM buyer_outreach_send_intents WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if existing_by_id is not None:
                stored = _send_intent_from_row(existing_by_id)
                if _dump_json(stored.to_payload()) != payload_json:
                    raise BuyerOutreachPersistenceConflictError("intent_id already belongs to different outbox state")
                return stored

            draft = self._draft_row_for_intent(connection, intent)
            _validate_intent_draft_binding(intent, _draft_from_row(draft))
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_outreach_send_intents (
                        intent_id, draft_id, platform, project_id, sender_account_registration_id,
                        idempotency_key, context_hash, draft_hash, state, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.intent_id,
                        intent.draft_id,
                        intent.platform,
                        intent.project_id,
                        intent.sender_account_registration_id,
                        intent.idempotency_key,
                        intent.context_hash,
                        intent.draft_hash,
                        intent.state.value,
                        payload_json,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerOutreachPersistenceConflictError("could not create Buyer proposal outbox item") from exc
        return intent

    def _update_send_intent_sync(self, intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        payload_json = _dump_json(intent.to_payload())
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            row = connection.execute(
                "SELECT * FROM buyer_outreach_send_intents WHERE intent_id = ?",
                (intent.intent_id,),
            ).fetchone()
            if row is None:
                raise BuyerOutreachPersistenceNotFoundError(f"send intent not found: {intent.intent_id}")
            stored = _send_intent_from_row(row)
            if not _same_send_identity(stored, intent):
                raise BuyerOutreachPersistenceConflictError("a send intent cannot change proposal or account identity")
            if _dump_json(stored.to_payload()) == payload_json:
                return stored
            allowed = _ALLOWED_INTENT_TRANSITIONS[stored.state]
            if intent.state not in allowed:
                raise BuyerOutreachPersistenceConflictError(
                    f"invalid durable send state transition: {stored.state.value} -> {intent.state.value}"
                )
            connection.execute(
                """
                UPDATE buyer_outreach_send_intents
                SET state = ?, payload_json = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (intent.state.value, payload_json, now, intent.intent_id),
            )
        return intent

    def _record_confirmation_sync(self, confirmation: BuyerOutreachConfirmation) -> BuyerOutreachConfirmation:
        payload_json = _dump_json(confirmation.to_payload())
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = connection.execute(
                "SELECT * FROM buyer_outreach_confirmations WHERE confirmation_id = ?",
                (confirmation.confirmation_id,),
            ).fetchone()
            if existing is not None:
                stored = _confirmation_from_row(existing)
                if _dump_json(stored.to_payload()) != payload_json:
                    raise BuyerOutreachPersistenceConflictError(
                        "confirmation_id is already immutable for another confirmation"
                    )
                return stored
            intent = connection.execute(
                "SELECT intent_id FROM buyer_outreach_send_intents WHERE intent_id = ?",
                (confirmation.intent_id,),
            ).fetchone()
            if intent is None:
                raise BuyerOutreachPersistenceNotFoundError(f"send intent not found: {confirmation.intent_id}")
            connection.execute(
                """
                INSERT INTO buyer_outreach_confirmations (
                    confirmation_id, intent_id, confirmed_by, confirmed_at, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    confirmation.confirmation_id,
                    confirmation.intent_id,
                    confirmation.confirmed_by,
                    confirmation.confirmed_at,
                    payload_json,
                    now,
                ),
            )
        return confirmation

    def _get_confirmation_sync(self, confirmation_id: str) -> BuyerOutreachConfirmation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_outreach_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
        return _confirmation_from_row(row) if row is not None else None

    def _append_audit_event_sync(
        self,
        event_type: str,
        run_id: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> None:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            promotion = connection.execute(
                """
                SELECT sender_account_registration_id FROM buyer_outreach_promotions
                WHERE platform = 'kwork' AND run_id = ? AND project_id = ?
                """,
                (run_id, project_id),
            ).fetchone()
            if promotion is None:
                raise BuyerOutreachPersistenceNotFoundError("audit events require an account-bound outreach promotion")
            connection.execute(
                """
                INSERT INTO buyer_outreach_audit_events (
                    event_type, run_id, project_id, sender_account_registration_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    run_id,
                    project_id,
                    str(promotion["sender_account_registration_id"]),
                    _dump_json(payload),
                    now,
                ),
            )

    def _list_confirmations_sync(self, intent_id: str) -> tuple[BuyerOutreachConfirmation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_outreach_confirmations
                WHERE intent_id = ?
                ORDER BY confirmed_at ASC, confirmation_id ASC
                """,
                (intent_id,),
            ).fetchall()
        return tuple(_confirmation_from_row(row) for row in rows)

    def _list_audit_events_sync(
        self,
        run_id: str,
        project_id: str,
        after_event_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_outreach_audit_events
                WHERE run_id = ? AND project_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (run_id, project_id, after_event_id, limit),
            ).fetchall()
        return [_audit_event_from_row(row) for row in rows]

    @staticmethod
    def _promotion_row_for_draft(
        connection: sqlite3.Connection,
        draft: BuyerComposedProposalDraft,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM buyer_outreach_promotions
            WHERE platform = ? AND run_id = ? AND project_id = ?
            """,
            (draft.draft.platform, draft.draft.run_id, draft.draft.project_id),
        ).fetchone()
        if row is None:
            raise BuyerOutreachPersistenceNotFoundError(
                "proposal draft requires a prior account-bound outreach promotion"
            )
        return row

    @staticmethod
    def _draft_row_for_intent(
        connection: sqlite3.Connection,
        intent: BuyerProposalSendIntent,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM buyer_outreach_drafts WHERE draft_id = ?",
            (intent.draft_id,),
        ).fetchone()
        if row is None:
            raise BuyerOutreachPersistenceNotFoundError("proposal send intent requires an immutable persisted draft")
        return row


def _promotion_payload(promotion: BuyerOutreachPromotion) -> dict[str, Any]:
    return {
        "promotion_id": promotion.promotion_id,
        "platform": promotion.platform,
        "run_id": promotion.run_id,
        "project_id": promotion.project_id,
        "sender_account_registration_id": promotion.sender_account_registration_id,
        "project": _jsonable_mapping(promotion.project, "promotion.project"),
        "project_hash": promotion.project_hash,
        "service_profile": _jsonable_mapping(promotion.service_profile, "promotion.service_profile"),
        "score": _jsonable_mapping(promotion.score, "promotion.score"),
        "additional_context": _jsonable_mapping(promotion.additional_context, "promotion.additional_context"),
        "attachment_context": _attachment_context_payload(promotion.attachment_context),
        "attachment_results": [_attachment_result_payload(result) for result in promotion.attachment_results],
    }


def _promotion_from_row(row: sqlite3.Row) -> BuyerOutreachPromotion:
    payload = _load_json_mapping(row["payload_json"], "promotion")
    attachment_context = _attachment_context_from_payload(payload.get("attachment_context"))
    attachment_results = tuple(
        _attachment_result_from_payload(item)
        for item in _jsonable_sequence(payload.get("attachment_results", []), "promotion.attachment_results")
    )
    if attachment_context is not None and attachment_results:
        raise BuyerOutreachPersistenceError("persisted promotion cannot contain both attachment context and results")
    try:
        promotion = BuyerOutreachPromotion(
            run_id=_required_text(payload.get("run_id"), "promotion.run_id"),
            project_id=_required_text(payload.get("project_id"), "promotion.project_id"),
            sender_account_registration_id=_required_text(
                payload.get("sender_account_registration_id"),
                "promotion.sender_account_registration_id",
            ),
            project=_jsonable_mapping(payload.get("project"), "promotion.project"),
            service_profile=_jsonable_mapping(payload.get("service_profile"), "promotion.service_profile"),
            platform=_required_text(payload.get("platform"), "promotion.platform"),
            score=_jsonable_mapping(payload.get("score", {}), "promotion.score"),
            additional_context=_jsonable_mapping(payload.get("additional_context", {}), "promotion.additional_context"),
            attachment_context=attachment_context,
            attachment_results=attachment_results,
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored outreach promotion is invalid") from exc
    if promotion.promotion_id != _required_text(payload.get("promotion_id"), "promotion.promotion_id"):
        raise BuyerOutreachPersistenceError("stored promotion ID does not match its account-bound identity")
    if promotion.project_hash != _required_text(payload.get("project_hash"), "promotion.project_hash"):
        raise BuyerOutreachPersistenceError("stored promotion project hash does not match its project snapshot")
    return promotion


def _draft_payload(draft: BuyerComposedProposalDraft) -> dict[str, Any]:
    return _jsonable_mapping(draft.to_payload(), "draft")


def _draft_from_row(row: sqlite3.Row) -> BuyerComposedProposalDraft:
    payload = _load_json_mapping(row["payload_json"], "draft")
    try:
        context_manifest = _context_manifest_from_payload(payload.get("context_manifest"))
        proposal_draft = BuyerProposalDraft(
            draft_id=_required_text(payload.get("draft_id"), "draft.draft_id"),
            run_id=_required_text(payload.get("run_id"), "draft.run_id"),
            project_id=_required_text(payload.get("project_id"), "draft.project_id"),
            body=_required_text(payload.get("body"), "draft.body"),
            price=payload.get("price"),
            delivery_days=payload.get("delivery_days"),
            context=_jsonable_mapping(payload.get("context"), "draft.context"),
            model_alias=_required_text(payload.get("model_alias"), "draft.model_alias"),
            platform=_required_text(payload.get("platform"), "draft.platform"),
            version=_positive_int(payload.get("version"), "draft.version"),
        )
        draft = BuyerComposedProposalDraft(
            draft=proposal_draft,
            context_manifest=context_manifest,
            sender_account_registration_id=_required_text(
                payload.get("sender_account_registration_id"),
                "draft.sender_account_registration_id",
            ),
            currency=_required_text(payload.get("currency"), "draft.currency"),
            resolved_provider=_required_text(payload.get("resolved_provider"), "draft.resolved_provider"),
            resolved_model=_required_text(payload.get("resolved_model"), "draft.resolved_model"),
            task=_required_text(payload.get("task"), "draft.task"),
            prompt_hash=_required_text(payload.get("prompt_hash"), "draft.prompt_hash"),
            parent_draft_id=_optional_text(payload.get("parent_draft_id"), "draft.parent_draft_id"),
            source=_required_text(payload.get("source"), "draft.source"),
            generated_body=_optional_text(payload.get("generated_body"), "draft.generated_body"),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored proposal draft is invalid") from exc
    if proposal_draft.context_hash != _required_text(payload.get("context_hash"), "draft.context_hash"):
        raise BuyerOutreachPersistenceError("stored draft context hash does not match immutable context")
    if proposal_draft.draft_hash != _required_text(payload.get("draft_hash"), "draft.draft_hash"):
        raise BuyerOutreachPersistenceError("stored draft hash does not match immutable proposal")
    return draft


def _context_manifest_from_payload(value: Any) -> BuyerProposalContextManifest:
    payload = _jsonable_mapping(value, "context_manifest")
    context = _jsonable_mapping(payload.get("context"), "context_manifest.context")
    attachment_context = _attachment_context_from_payload(context.get("attachments"))
    if attachment_context is None:
        raise BuyerOutreachPersistenceError("proposal context manifest must contain attachment evidence")
    try:
        manifest = BuyerProposalContextManifest(
            run_id=_required_text(payload.get("run_id"), "context_manifest.run_id"),
            project_id=_required_text(payload.get("project_id"), "context_manifest.project_id"),
            project=_jsonable_mapping(context.get("project"), "context_manifest.project"),
            service_profile=_jsonable_mapping(context.get("service_profile"), "context_manifest.service_profile"),
            attachment_context=attachment_context,
            score=_jsonable_mapping(context.get("score", {}), "context_manifest.score"),
            additional_context=_jsonable_mapping(
                context.get("additional_context", {}),
                "context_manifest.additional_context",
            ),
            prompt_version=_required_text(payload.get("prompt_version"), "context_manifest.prompt_version"),
            schema_version=_positive_int(payload.get("schema_version"), "context_manifest.schema_version"),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored proposal context manifest is invalid") from exc
    if manifest.context_hash != _required_text(payload.get("context_hash"), "context_manifest.context_hash"):
        raise BuyerOutreachPersistenceError("stored context manifest hash does not match its context")
    if manifest.project_hash != _required_text(payload.get("project_hash"), "context_manifest.project_hash"):
        raise BuyerOutreachPersistenceError("stored context manifest project hash does not match its project")
    return manifest


def _preflight_result_from_payload(value: Any) -> BuyerProposalPreflightResult:
    payload = _jsonable_mapping(value, "preflight")
    try:
        preflight = BuyerProposalPreflight(
            passed=_required_bool(payload.get("passed"), "preflight.passed"),
            checks={
                _required_text(key, "preflight.check"): _required_bool(item, f"preflight.checks.{key}")
                for key, item in _jsonable_mapping(payload.get("checks", {}), "preflight.checks").items()
            },
            failures=tuple(
                _required_text(item, "preflight.failure")
                for item in _jsonable_sequence(payload.get("failures", []), "preflight.failures")
            ),
        )
        diagnostics = tuple(
            BuyerProposalPreflightDiagnostic(
                code=_required_text(item.get("code"), "preflight.diagnostic.code"),
                passed=_required_bool(item.get("passed"), "preflight.diagnostic.passed"),
                message=_required_text(item.get("message"), "preflight.diagnostic.message"),
                severity=_required_text(item.get("severity"), "preflight.diagnostic.severity"),
            )
            for raw_item in _jsonable_sequence(payload.get("diagnostics", []), "preflight.diagnostics")
            for item in (_jsonable_mapping(raw_item, "preflight.diagnostic"),)
        )
        return BuyerProposalPreflightResult(
            preflight=preflight,
            diagnostics=diagnostics,
            current_project_hash=_optional_text(payload.get("current_project_hash"), "preflight.current_project_hash"),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored proposal preflight is invalid") from exc


def _send_intent_from_row(row: sqlite3.Row) -> BuyerProposalSendIntent:
    payload = _load_json_mapping(row["payload_json"], "send intent")
    preflight_payload = payload.get("preflight")
    preflight = _preflight_from_payload(preflight_payload) if preflight_payload is not None else None
    try:
        return BuyerProposalSendIntent(
            intent_id=_required_text(payload.get("intent_id"), "intent.intent_id"),
            draft_id=_required_text(payload.get("draft_id"), "intent.draft_id"),
            project_id=_required_text(payload.get("project_id"), "intent.project_id"),
            platform=_required_text(payload.get("platform"), "intent.platform"),
            sender_account_registration_id=_required_text(
                payload.get("sender_account_registration_id"),
                "intent.sender_account_registration_id",
            ),
            idempotency_key=_required_text(payload.get("idempotency_key"), "intent.idempotency_key"),
            context_hash=_required_text(payload.get("context_hash"), "intent.context_hash"),
            draft_hash=_required_text(payload.get("draft_hash"), "intent.draft_hash"),
            state=BuyerProposalSendState(_required_text(payload.get("state"), "intent.state")),
            preflight=preflight,
            send_attempts=_non_negative_int(payload.get("send_attempts"), "intent.send_attempts"),
            remote_receipt=_optional_text(payload.get("remote_receipt"), "intent.remote_receipt"),
            failure_reason=_optional_text(payload.get("failure_reason"), "intent.failure_reason"),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored proposal send intent is invalid") from exc


def _preflight_from_payload(value: Any) -> BuyerProposalPreflight:
    payload = _jsonable_mapping(value, "send_intent.preflight")
    try:
        return BuyerProposalPreflight(
            passed=_required_bool(payload.get("passed"), "send_intent.preflight.passed"),
            checks={
                _required_text(key, "send_intent.preflight.check"): _required_bool(
                    item,
                    f"send_intent.preflight.checks.{key}",
                )
                for key, item in _jsonable_mapping(payload.get("checks", {}), "send_intent.preflight.checks").items()
            },
            failures=tuple(
                _required_text(item, "send_intent.preflight.failure")
                for item in _jsonable_sequence(payload.get("failures", []), "send_intent.preflight.failures")
            ),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored send-intent preflight is invalid") from exc


def _confirmation_from_row(row: sqlite3.Row) -> BuyerOutreachConfirmation:
    payload = _load_json_mapping(row["payload_json"], "confirmation")
    try:
        return BuyerOutreachConfirmation(
            confirmation_id=_required_text(payload.get("confirmation_id"), "confirmation.confirmation_id"),
            intent_id=_required_text(payload.get("intent_id"), "confirmation.intent_id"),
            confirmed_by=_required_text(payload.get("confirmed_by"), "confirmation.confirmed_by"),
            confirmed_at=_required_text(payload.get("confirmed_at"), "confirmation.confirmed_at"),
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored proposal confirmation is invalid") from exc


def _audit_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": int(row["event_id"]),
        "event_type": str(row["event_type"]),
        "run_id": str(row["run_id"]),
        "project_id": str(row["project_id"]),
        "sender_account_registration_id": str(row["sender_account_registration_id"]),
        "payload": _load_json_mapping(row["payload_json"], "audit payload"),
        "created_at": str(row["created_at"]),
    }


def _validate_draft_promotion_binding(
    draft: BuyerComposedProposalDraft,
    promotion: BuyerOutreachPromotion,
) -> None:
    if (
        draft.draft.platform != promotion.platform
        or draft.draft.run_id != promotion.run_id
        or draft.draft.project_id != promotion.project_id
        or draft.sender_account_registration_id != promotion.sender_account_registration_id
    ):
        raise BuyerOutreachPersistenceConflictError("proposal draft is outside its promoted account scope")
    if draft.context_manifest.project_hash != promotion.project_hash:
        raise BuyerOutreachPersistenceConflictError(
            "proposal draft does not use the immutable promoted project snapshot"
        )


def _validate_intent_draft_binding(intent: BuyerProposalSendIntent, draft: BuyerComposedProposalDraft) -> None:
    if (
        intent.draft_id != draft.draft_id
        or intent.project_id != draft.draft.project_id
        or intent.platform != draft.draft.platform
        or intent.sender_account_registration_id != draft.sender_account_registration_id
        or intent.context_hash != draft.draft.context_hash
        or intent.draft_hash != draft.draft.draft_hash
    ):
        raise BuyerOutreachPersistenceConflictError("send intent is outside its immutable draft and account scope")


def _same_send_identity(left: BuyerProposalSendIntent, right: BuyerProposalSendIntent) -> bool:
    return (
        left.draft_id == right.draft_id
        and left.project_id == right.project_id
        and left.platform == right.platform
        and left.sender_account_registration_id == right.sender_account_registration_id
        and left.idempotency_key == right.idempotency_key
        and left.context_hash == right.context_hash
        and left.draft_hash == right.draft_hash
    )


def _attachment_context_payload(context: BuyerAttachmentContext | None) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "text": _require_string(context.text, "attachment_context.text"),
        "manifest": _jsonable_sequence(context.manifest, "attachment_context.manifest"),
        "context_hash": _required_text(context.context_hash, "attachment_context.context_hash"),
    }


def _attachment_context_from_payload(value: Any) -> BuyerAttachmentContext | None:
    if value is None:
        return None
    payload = _jsonable_mapping(value, "attachment_context")
    manifest = tuple(
        _jsonable_mapping(item, "attachment_context.manifest item")
        for item in _jsonable_sequence(payload.get("manifest", []), "attachment_context.manifest")
    )
    return BuyerAttachmentContext(
        text=_require_string(payload.get("text"), "attachment_context.text"),
        manifest=manifest,
        context_hash=_required_text(payload.get("context_hash"), "attachment_context.context_hash"),
    )


def _attachment_result_payload(result: BuyerAttachmentParseResult) -> dict[str, Any]:
    metadata = result.metadata
    return {
        "metadata": {
            "filename": metadata.filename,
            "content_type": metadata.content_type,
            "attachment_type": metadata.attachment_type.value if metadata.attachment_type is not None else None,
            "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256,
        },
        "status": result.status.value,
        "parser": result.parser,
        "text": result.text,
        "omissions": [{"reason": omission.reason, "detail": omission.detail} for omission in result.omissions],
    }


def _attachment_result_from_payload(value: Any) -> BuyerAttachmentParseResult:
    payload = _jsonable_mapping(value, "attachment_result")
    metadata_payload = _jsonable_mapping(payload.get("metadata"), "attachment_result.metadata")
    attachment_type_value = metadata_payload.get("attachment_type")
    attachment_type = (
        BuyerAttachmentType(_required_text(attachment_type_value, "attachment_result.attachment_type"))
        if attachment_type_value is not None
        else None
    )
    try:
        metadata = BuyerAttachmentMetadata(
            filename=_required_text(metadata_payload.get("filename"), "attachment_result.filename"),
            content_type=_optional_text(metadata_payload.get("content_type"), "attachment_result.content_type"),
            attachment_type=attachment_type,
            size_bytes=_non_negative_int(metadata_payload.get("size_bytes"), "attachment_result.size_bytes"),
            sha256=_required_text(metadata_payload.get("sha256"), "attachment_result.sha256"),
        )
        omissions = tuple(
            BuyerAttachmentOmission(
                reason=_required_text(item.get("reason"), "attachment_result.omission.reason"),
                detail=_optional_text(item.get("detail"), "attachment_result.omission.detail"),
            )
            for raw_item in _jsonable_sequence(payload.get("omissions", []), "attachment_result.omissions")
            for item in (_jsonable_mapping(raw_item, "attachment_result.omission"),)
        )
        return BuyerAttachmentParseResult(
            metadata=metadata,
            status=BuyerAttachmentParseStatus(_required_text(payload.get("status"), "attachment_result.status")),
            parser=_optional_text(payload.get("parser"), "attachment_result.parser"),
            text=_require_string(payload.get("text"), "attachment_result.text"),
            omissions=omissions,
        )
    except (TypeError, ValueError) as exc:
        raise BuyerOutreachPersistenceError("stored attachment parse result is invalid") from exc


def _dump_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value, "value"), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _load_json_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise BuyerOutreachPersistenceError(f"stored {name} payload is not JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BuyerOutreachPersistenceError(f"stored {name} payload is malformed") from exc
    return _jsonable_mapping(decoded, name)


def _jsonable_mapping(value: Any, name: str) -> dict[str, Any]:
    normalized = _jsonable(value, name)
    if not isinstance(normalized, dict):
        raise BuyerOutreachPersistenceError(f"{name} must be a mapping")
    return normalized


def _jsonable_sequence(value: Any, name: str) -> list[Any]:
    normalized = _jsonable(value, name)
    if not isinstance(normalized, list):
        raise BuyerOutreachPersistenceError(f"{name} must be a sequence")
    return normalized


def _jsonable(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{name} keys must be strings")
            normalized[key] = _jsonable(item, name)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, name) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} numbers must be finite")
        return value
    raise TypeError(f"unsupported {name} value type: {type(value).__name__}")


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BuyerOutreachPersistenceError(f"{name} cannot be blank")
    return normalized


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _required_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BuyerOutreachPersistenceError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuyerOutreachPersistenceError(f"{name} must be a non-negative integer")
    return value


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    return max(1, min(value, 5_000))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


BuyerOutreachSQLiteStore = SQLiteBuyerOutreachStore


__all__ = [
    "BuyerOutreachPersistenceConflictError",
    "BuyerOutreachPersistenceError",
    "BuyerOutreachPersistenceNotFoundError",
    "BuyerOutreachSQLiteStore",
    "SQLiteBuyerOutreachStore",
]
