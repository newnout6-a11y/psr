"""Durable, account-bound SQLite storage for Buyer Search conversations.

The store accepts immutable conversation records and keeps remote inbox state
separate from proposal/outreach storage.  It deliberately has no Kwork client
or remote mutation operation.  A separately injected send controller can use
its durable draft, intent, audit, and observed-message operations without
giving this store a network session.

Every instance requires an explicit database path.  Connections are short
lived and the schema is additive, so the store can be composed beside the
Buyer Search repository without sharing process-global state or credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from .conversations import (
    BuyerConversation,
    BuyerConversationAttachment,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationCursorConflict,
    BuyerConversationDraft,
    BuyerConversationError,
    BuyerConversationKey,
    BuyerConversationMessage,
    BuyerConversationSyncBatch,
    merge_account_cursors,
)
from .conversation_send import BuyerConversationSendError, BuyerConversationSendIntent, BuyerConversationSendState


class BuyerConversationPersistenceError(RuntimeError):
    """Base exception raised by the durable Buyer conversation store."""


class BuyerConversationPersistenceConflictError(BuyerConversationPersistenceError):
    """Raised when a cursor or immutable draft conflicts with stored state."""


class BuyerConversationPersistenceNotFoundError(BuyerConversationPersistenceError):
    """Raised when a required account-bound conversation is absent."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_conversation_cursors (
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    cursor TEXT,
    watermark TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    updated_at TEXT,
    persisted_at TEXT NOT NULL,
    PRIMARY KEY (platform, account_registration_id)
);

CREATE TABLE IF NOT EXISTS buyer_conversations (
    conversation_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    remote_dialog_id TEXT NOT NULL,
    context_json TEXT NOT NULL,
    remote_title TEXT,
    last_remote_message_at TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (platform, account_registration_id, remote_dialog_id),
    UNIQUE (conversation_id, platform, account_registration_id)
);

CREATE INDEX IF NOT EXISTS buyer_conversations_account_recent_idx
    ON buyer_conversations(platform, account_registration_id, last_remote_message_at DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS buyer_conversation_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    remote_message_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
    body TEXT,
    sender_account_registration_id TEXT,
    remote_sender_id TEXT,
    remote_created_at TEXT,
    observed_at TEXT,
    delivery_state TEXT NOT NULL,
    read_at TEXT,
    context_json TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, remote_message_id),
    FOREIGN KEY (conversation_id, platform, account_registration_id)
        REFERENCES buyer_conversations(conversation_id, platform, account_registration_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS buyer_conversation_messages_dialog_idx
    ON buyer_conversation_messages(conversation_id, remote_created_at ASC, observed_at ASC, message_id ASC);

CREATE TABLE IF NOT EXISTS buyer_conversation_attachments (
    attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL REFERENCES buyer_conversation_messages(message_id) ON DELETE RESTRICT,
    attachment_key TEXT NOT NULL,
    attachment_index INTEGER NOT NULL CHECK(attachment_index >= 0),
    attachment_json TEXT NOT NULL,
    attachment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (message_id, attachment_key)
);

CREATE INDEX IF NOT EXISTS buyer_conversation_attachments_message_idx
    ON buyer_conversation_attachments(message_id, attachment_index ASC, attachment_key ASC);

CREATE TABLE IF NOT EXISTS buyer_conversation_drafts (
    draft_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    body TEXT,
    context_json TEXT NOT NULL,
    source TEXT NOT NULL,
    draft_created_at TEXT,
    draft_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state = 'draft'),
    persisted_at TEXT NOT NULL,
    UNIQUE (conversation_id, draft_id),
    FOREIGN KEY (conversation_id, platform, account_registration_id)
        REFERENCES buyer_conversations(conversation_id, platform, account_registration_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS buyer_conversation_drafts_dialog_idx
    ON buyer_conversation_drafts(conversation_id, draft_created_at ASC, persisted_at ASC, draft_id ASC);

CREATE TABLE IF NOT EXISTS buyer_conversation_send_intents (
    intent_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    sender_account_registration_id TEXT NOT NULL,
    body TEXT NOT NULL,
    context_json TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    command_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('pending', 'sending', 'sent', 'unknown', 'failed')),
    send_attempts INTEGER NOT NULL CHECK(send_attempts >= 0),
    remote_message_id TEXT,
    remote_receipt TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, draft_id),
    FOREIGN KEY (conversation_id, platform, account_registration_id)
        REFERENCES buyer_conversations(conversation_id, platform, account_registration_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS buyer_conversation_send_intents_dialog_idx
    ON buyer_conversation_send_intents(conversation_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS buyer_conversation_send_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES buyer_conversation_send_intents(intent_id) ON DELETE RESTRICT,
    conversation_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_registration_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS buyer_conversation_send_audit_events_intent_idx
    ON buyer_conversation_send_audit_events(intent_id, event_id ASC);
"""


class SQLiteBuyerConversationStore:
    """A standalone, idempotent SQLite implementation for Buyer conversations.

    Read operations always require the platform/account scope or a
    ``BuyerConversationKey``.  That prevents an identical remote dialog ID
    belonging to a second account from being retrieved or overwritten through
    the first account's scope.
    """

    def __init__(self, db_path: str | Path) -> None:
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a string or Path")
        if not str(db_path).strip():
            raise BuyerConversationPersistenceError("db_path cannot be blank")
        self.db_path = Path(db_path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the additive schema for the configured explicit database path."""

        await self._ensure_initialized()

    async def close(self) -> None:
        """Close the logical lifecycle; each database operation owns its connection."""

        return None

    async def get_cursor(
        self,
        *,
        platform: str,
        account_registration_id: str,
    ) -> BuyerConversationCursor | None:
        """Return the opaque sync cursor for exactly one platform/account pair."""

        scope = _scope_cursor(platform, account_registration_id)
        return await self._call(self._get_cursor_sync, scope.platform, scope.account_registration_id)

    async def commit_sync_batch(self, batch: BuyerConversationSyncBatch) -> BuyerConversationSyncBatch:
        """Atomically persist a scoped remote inbox batch and merge its cursor.

        Remote conversations and messages are mutable observations, so a later
        observation for the same identity updates the stored projection.  The
        attached cursor never moves backwards; equal revisions with different
        cursor payloads are rejected as an ambiguous concurrent commit.
        """

        if not isinstance(batch, BuyerConversationSyncBatch):
            raise TypeError("batch must be a BuyerConversationSyncBatch")
        return await self._call(self._commit_sync_batch_sync, batch)

    async def get_conversation(self, key: BuyerConversationKey) -> BuyerConversation | None:
        """Return one dialog only when its complete account-bound key matches."""

        _require_key(key)
        return await self._call(self._get_conversation_sync, key)

    async def list_conversations(
        self,
        *,
        platform: str,
        account_registration_id: str,
        limit: int = 200,
    ) -> tuple[BuyerConversation, ...]:
        """List dialogs visible to a single account scope, newest observed first."""

        scope = _scope_cursor(platform, account_registration_id)
        return await self._call(
            self._list_conversations_sync,
            scope.platform,
            scope.account_registration_id,
            _bounded_limit(limit),
        )

    async def get_message(
        self,
        *,
        key: BuyerConversationKey,
        remote_message_id: str,
    ) -> BuyerConversationMessage | None:
        """Return one remote message only through its account-bound dialog key."""

        _require_key(key)
        return await self._call(self._get_message_sync, key, _required_text(remote_message_id, "remote_message_id"))

    async def list_messages(
        self,
        *,
        key: BuyerConversationKey,
        limit: int = 500,
    ) -> tuple[BuyerConversationMessage, ...]:
        """List an account-bound dialog's normalized history in chronological order."""

        _require_key(key)
        return await self._call(self._list_messages_sync, key, _bounded_limit(limit))

    async def save_draft(self, draft: BuyerConversationDraft) -> BuyerConversationDraft:
        """Persist an editor-only account-pinned reply draft idempotently.

        A draft ID is immutable.  Repeating the exact draft returns the stored
        record; changing its contents requires a new draft ID.  An explicit
        sender may later record an account-bound delivery intent, but the
        store itself never initiates a remote request.
        """

        if not isinstance(draft, BuyerConversationDraft):
            raise TypeError("draft must be a BuyerConversationDraft")
        return await self._call(self._save_draft_sync, draft)

    async def get_draft(
        self,
        *,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> BuyerConversationDraft | None:
        """Load a draft only when its dialog key and account scope both match."""

        _require_key(key)
        return await self._call(self._get_draft_sync, key, _required_text(draft_id, "draft_id"))

    async def list_drafts(
        self,
        *,
        key: BuyerConversationKey,
        limit: int = 200,
    ) -> tuple[BuyerConversationDraft, ...]:
        """List editor drafts for exactly one account-bound dialog."""

        _require_key(key)
        return await self._call(self._list_drafts_sync, key, _bounded_limit(limit))

    async def get_send_intent(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
    ) -> BuyerConversationSendIntent | None:
        """Return a remote delivery request only through its full account scope."""

        _require_key(key)
        return await self._call(self._get_send_intent_sync, key, _required_text(intent_id, "intent_id"))

    async def get_send_intent_by_draft(
        self,
        *,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> BuyerConversationSendIntent | None:
        """Return the single delivery intent associated with a local reply draft."""

        _require_key(key)
        return await self._call(self._get_send_intent_by_draft_sync, key, _required_text(draft_id, "draft_id"))

    async def create_send_intent(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        """Persist a pending account-bound send intent before remote delivery begins."""

        if not isinstance(intent, BuyerConversationSendIntent):
            raise TypeError("intent must be a BuyerConversationSendIntent")
        return await self._call(self._create_send_intent_sync, intent)

    async def update_send_intent(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        """Persist a monotonic state transition without changing immutable draft content."""

        if not isinstance(intent, BuyerConversationSendIntent):
            raise TypeError("intent must be a BuyerConversationSendIntent")
        return await self._call(self._update_send_intent_sync, intent)

    async def append_send_audit_event(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append immutable operator, provenance, and reconciliation evidence."""

        _require_key(key)
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        await self._call(
            self._append_send_audit_event_sync,
            key,
            _required_text(intent_id, "intent_id"),
            _required_text(event_type, "event_type"),
            dict(payload),
        )

    async def record_send_outcome(
        self,
        *,
        intent: BuyerConversationSendIntent,
        event_type: str,
        payload: Mapping[str, Any],
        outgoing_message: BuyerConversationMessage | None = None,
    ) -> BuyerConversationSendIntent:
        """Atomically save a send outcome and its remotely identified outgoing message."""

        if not isinstance(intent, BuyerConversationSendIntent):
            raise TypeError("intent must be a BuyerConversationSendIntent")
        if outgoing_message is not None and not isinstance(outgoing_message, BuyerConversationMessage):
            raise TypeError("outgoing_message must be a BuyerConversationMessage or None")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        return await self._call(
            self._record_send_outcome_sync,
            intent,
            _required_text(event_type, "event_type"),
            dict(payload),
            outgoing_message,
        )

    async def list_send_audit_events(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        """List immutable audit records for one account-bound delivery intent."""

        _require_key(key)
        return await self._call(
            self._list_send_audit_events_sync,
            key,
            _required_text(intent_id, "intent_id"),
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

    def _get_cursor_sync(self, platform: str, account_registration_id: str) -> BuyerConversationCursor | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT platform, account_registration_id, cursor, watermark, revision, updated_at
                FROM buyer_conversation_cursors
                WHERE platform = ? AND account_registration_id = ?
                """,
                (platform, account_registration_id),
            ).fetchone()
        return _cursor_from_row(row) if row is not None else None

    def _commit_sync_batch_sync(self, batch: BuyerConversationSyncBatch) -> BuyerConversationSyncBatch:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            merged_cursor = self._merge_cursor_sync(connection, batch.cursor, now)
            for conversation in batch.conversations:
                self._upsert_conversation_sync(connection, conversation, now)
            for message in batch.messages:
                self._ensure_conversation_sync(connection, message.key, message.context, now)
                self._upsert_message_sync(connection, message, now)
        return BuyerConversationSyncBatch(
            cursor=merged_cursor,
            conversations=batch.conversations,
            messages=batch.messages,
        )

    def _merge_cursor_sync(
        self,
        connection: sqlite3.Connection,
        candidate: BuyerConversationCursor,
        now: str,
    ) -> BuyerConversationCursor:
        row = connection.execute(
            """
            SELECT platform, account_registration_id, cursor, watermark, revision, updated_at
            FROM buyer_conversation_cursors
            WHERE platform = ? AND account_registration_id = ?
            """,
            candidate.account_scope,
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO buyer_conversation_cursors (
                    platform, account_registration_id, cursor, watermark, revision, updated_at, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.platform,
                    candidate.account_registration_id,
                    candidate.cursor,
                    candidate.watermark,
                    candidate.revision,
                    candidate.updated_at,
                    now,
                ),
            )
            return candidate

        current = _cursor_from_row(row)
        try:
            merged = merge_account_cursors(current, candidate)
        except BuyerConversationCursorConflict as exc:
            raise BuyerConversationPersistenceConflictError(
                "equally-versioned account cursor conflicts with stored state"
            ) from exc
        except BuyerConversationError as exc:
            raise BuyerConversationPersistenceConflictError("cursor is outside its stored account scope") from exc
        if merged != current:
            connection.execute(
                """
                UPDATE buyer_conversation_cursors
                SET cursor = ?, watermark = ?, revision = ?, updated_at = ?, persisted_at = ?
                WHERE platform = ? AND account_registration_id = ?
                """,
                (
                    merged.cursor,
                    merged.watermark,
                    merged.revision,
                    merged.updated_at,
                    now,
                    merged.platform,
                    merged.account_registration_id,
                ),
            )
        return merged

    def _get_conversation_sync(self, key: BuyerConversationKey) -> BuyerConversation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM buyer_conversations
                WHERE conversation_id = ? AND platform = ?
                    AND account_registration_id = ? AND remote_dialog_id = ?
                """,
                (key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id),
            ).fetchone()
        return _conversation_from_row(row) if row is not None else None

    def _list_conversations_sync(
        self,
        platform: str,
        account_registration_id: str,
        limit: int,
    ) -> tuple[BuyerConversation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_conversations
                WHERE platform = ? AND account_registration_id = ?
                ORDER BY COALESCE(last_remote_message_at, synced_at, updated_at) DESC, conversation_id ASC
                LIMIT ?
                """,
                (platform, account_registration_id, limit),
            ).fetchall()
        return tuple(_conversation_from_row(row) for row in rows)

    def _get_message_sync(self, key: BuyerConversationKey, remote_message_id: str) -> BuyerConversationMessage | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT messages.*, conversations.platform, conversations.account_registration_id,
                    conversations.remote_dialog_id
                FROM buyer_conversation_messages AS messages
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = messages.conversation_id
                WHERE messages.conversation_id = ?
                    AND conversations.platform = ?
                    AND conversations.account_registration_id = ?
                    AND conversations.remote_dialog_id = ?
                    AND messages.remote_message_id = ?
                """,
                (
                    key.conversation_id,
                    key.platform,
                    key.account_registration_id,
                    key.remote_dialog_id,
                    remote_message_id,
                ),
            ).fetchone()
            if row is None:
                return None
            attachments = self._attachments_for_message_sync(connection, row["message_id"])
        return _message_from_row(row, attachments)

    def _list_messages_sync(self, key: BuyerConversationKey, limit: int) -> tuple[BuyerConversationMessage, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT messages.*, conversations.platform, conversations.account_registration_id,
                    conversations.remote_dialog_id
                FROM buyer_conversation_messages AS messages
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = messages.conversation_id
                WHERE messages.conversation_id = ?
                    AND conversations.platform = ?
                    AND conversations.account_registration_id = ?
                    AND conversations.remote_dialog_id = ?
                ORDER BY COALESCE(messages.remote_created_at, messages.observed_at, messages.created_at) ASC,
                    messages.message_id ASC
                LIMIT ?
                """,
                (key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id, limit),
            ).fetchall()
            messages = tuple(
                _message_from_row(row, self._attachments_for_message_sync(connection, row["message_id"]))
                for row in rows
            )
        return messages

    def _save_draft_sync(self, draft: BuyerConversationDraft) -> BuyerConversationDraft:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            self._ensure_conversation_sync(connection, draft.key, draft.context, now)
            existing = connection.execute(
                """
                SELECT drafts.*, conversations.platform, conversations.account_registration_id,
                    conversations.remote_dialog_id
                FROM buyer_conversation_drafts AS drafts
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = drafts.conversation_id
                WHERE drafts.draft_id = ?
                """,
                (draft.draft_id,),
            ).fetchone()
            if existing is not None:
                stored = _draft_from_row(existing)
                if stored.to_payload() != draft.to_payload():
                    raise BuyerConversationPersistenceConflictError(
                        "draft_id already belongs to a different immutable account-bound draft"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_conversation_drafts (
                        draft_id, conversation_id, platform, account_registration_id,
                        sender_account_registration_id, body, context_json, source,
                        draft_created_at, draft_hash, state, persisted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                    """,
                    (
                        draft.draft_id,
                        draft.conversation_id,
                        draft.key.platform,
                        draft.key.account_registration_id,
                        draft.sender_account_registration_id,
                        draft.body,
                        _dump_json(draft.context.to_payload()),
                        draft.source,
                        draft.created_at,
                        draft.draft_hash,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerConversationPersistenceConflictError(
                    "could not persist immutable account-bound reply draft"
                ) from exc
        return draft

    def _get_draft_sync(self, key: BuyerConversationKey, draft_id: str) -> BuyerConversationDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT drafts.*, conversations.platform, conversations.account_registration_id,
                    conversations.remote_dialog_id
                FROM buyer_conversation_drafts AS drafts
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = drafts.conversation_id
                WHERE drafts.draft_id = ? AND drafts.conversation_id = ?
                    AND conversations.platform = ? AND conversations.account_registration_id = ?
                    AND conversations.remote_dialog_id = ?
                """,
                (draft_id, key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id),
            ).fetchone()
        return _draft_from_row(row) if row is not None else None

    def _list_drafts_sync(self, key: BuyerConversationKey, limit: int) -> tuple[BuyerConversationDraft, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT drafts.*, conversations.platform, conversations.account_registration_id,
                    conversations.remote_dialog_id
                FROM buyer_conversation_drafts AS drafts
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = drafts.conversation_id
                WHERE drafts.conversation_id = ? AND conversations.platform = ?
                    AND conversations.account_registration_id = ? AND conversations.remote_dialog_id = ?
                ORDER BY COALESCE(drafts.draft_created_at, drafts.persisted_at) ASC, drafts.draft_id ASC
                LIMIT ?
                """,
                (key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id, limit),
            ).fetchall()
        return tuple(_draft_from_row(row) for row in rows)

    def _get_send_intent_sync(
        self,
        key: BuyerConversationKey,
        intent_id: str,
    ) -> BuyerConversationSendIntent | None:
        with self._connect() as connection:
            row = self._send_intent_row_for_key_sync(connection, key, intent_id)
        return _send_intent_from_row(row) if row is not None else None

    def _get_send_intent_by_draft_sync(
        self,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> BuyerConversationSendIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT intents.*, conversations.remote_dialog_id
                FROM buyer_conversation_send_intents AS intents
                INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = intents.conversation_id
                    AND conversations.platform = intents.platform
                    AND conversations.account_registration_id = intents.account_registration_id
                WHERE intents.conversation_id = ? AND intents.platform = ?
                    AND intents.account_registration_id = ? AND conversations.remote_dialog_id = ?
                    AND intents.draft_id = ?
                """,
                (key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id, draft_id),
            ).fetchone()
        return _send_intent_from_row(row) if row is not None else None

    def _create_send_intent_sync(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            draft = self._draft_for_send_intent_sync(connection, intent.key, intent.draft_id)
            if draft is None:
                raise BuyerConversationPersistenceNotFoundError("conversation send intent draft was not found")
            _validate_send_intent_draft(intent, draft)
            existing = self._send_intent_row_for_id_sync(connection, intent.intent_id)
            if existing is not None:
                stored = _send_intent_from_row(existing)
                if stored.to_payload() != intent.to_payload():
                    raise BuyerConversationPersistenceConflictError(
                        "send intent ID already belongs to a different immutable account-bound delivery"
                    )
                return stored
            existing_for_draft = self._send_intent_row_for_draft_sync(connection, intent.key, intent.draft_id)
            if existing_for_draft is not None:
                return _send_intent_from_row(existing_for_draft)
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_conversation_send_intents (
                        intent_id, conversation_id, platform, account_registration_id, draft_id,
                        sender_account_registration_id, body, context_json, draft_hash,
                        requested_by, command_id, requested_at, idempotency_key, state,
                        send_attempts, remote_message_id, remote_receipt, failure_reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.intent_id,
                        intent.key.conversation_id,
                        intent.key.platform,
                        intent.key.account_registration_id,
                        intent.draft_id,
                        intent.sender_account_registration_id,
                        intent.body,
                        _dump_json(intent.context.to_payload()),
                        intent.draft_hash,
                        intent.requested_by,
                        intent.command_id,
                        intent.requested_at,
                        intent.idempotency_key,
                        intent.state.value,
                        intent.send_attempts,
                        intent.remote_message_id,
                        intent.remote_receipt,
                        intent.failure_reason,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerConversationPersistenceConflictError(
                    "could not persist immutable account-bound conversation send intent"
                ) from exc
        return intent

    def _update_send_intent_sync(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = self._send_intent_row_for_id_sync(connection, intent.intent_id)
            if existing is None:
                raise BuyerConversationPersistenceNotFoundError("conversation send intent was not found")
            stored = _send_intent_from_row(existing)
            _validate_send_intent_transition(stored, intent)
            self._write_send_intent_sync(connection, intent, now)
        return intent

    def _append_send_audit_event_sync(
        self,
        key: BuyerConversationKey,
        intent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            if self._send_intent_row_for_key_sync(connection, key, intent_id) is None:
                raise BuyerConversationPersistenceNotFoundError("conversation send intent was not found")
            self._append_send_audit_event_in_connection(
                connection,
                key=key,
                intent_id=intent_id,
                event_type=event_type,
                payload=payload,
                now=now,
            )

    def _record_send_outcome_sync(
        self,
        intent: BuyerConversationSendIntent,
        event_type: str,
        payload: Mapping[str, Any],
        outgoing_message: BuyerConversationMessage | None,
    ) -> BuyerConversationSendIntent:
        now = _now()
        with self._connect() as connection, self._transaction(connection):
            existing = self._send_intent_row_for_id_sync(connection, intent.intent_id)
            if existing is None:
                raise BuyerConversationPersistenceNotFoundError("conversation send intent was not found")
            stored = _send_intent_from_row(existing)
            _validate_send_intent_transition(stored, intent)
            if outgoing_message is not None:
                if (
                    intent.state is not BuyerConversationSendState.SENT
                    or outgoing_message.key != intent.key
                    or outgoing_message.remote_message_id != intent.remote_message_id
                    or outgoing_message.sender_account_registration_id != intent.sender_account_registration_id
                    or outgoing_message.body != intent.body
                    or outgoing_message.context != intent.context
                ):
                    raise BuyerConversationPersistenceConflictError(
                        "outgoing message does not match its account-bound conversation send intent"
                    )
                self._ensure_conversation_sync(connection, intent.key, intent.context, now)
                self._upsert_message_sync(connection, outgoing_message, now)
            self._write_send_intent_sync(connection, intent, now)
            self._append_send_audit_event_in_connection(
                connection,
                key=intent.key,
                intent_id=intent.intent_id,
                event_type=event_type,
                payload=payload,
                now=now,
            )
        return intent

    def _list_send_audit_events_sync(
        self,
        key: BuyerConversationKey,
        intent_id: str,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            if self._send_intent_row_for_key_sync(connection, key, intent_id) is None:
                raise BuyerConversationPersistenceNotFoundError("conversation send intent was not found")
            rows = connection.execute(
                """
                SELECT event_id, intent_id, conversation_id, platform, account_registration_id,
                    event_type, payload_json, created_at
                FROM buyer_conversation_send_audit_events
                WHERE intent_id = ? AND conversation_id = ? AND platform = ?
                    AND account_registration_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (intent_id, key.conversation_id, key.platform, key.account_registration_id, limit),
            ).fetchall()
        return tuple(_send_audit_event_from_row(row) for row in rows)

    def _send_intent_row_for_key_sync(
        self,
        connection: sqlite3.Connection,
        key: BuyerConversationKey,
        intent_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT intents.*, conversations.remote_dialog_id
            FROM buyer_conversation_send_intents AS intents
            INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = intents.conversation_id
                AND conversations.platform = intents.platform
                AND conversations.account_registration_id = intents.account_registration_id
            WHERE intents.intent_id = ? AND intents.conversation_id = ?
                AND intents.platform = ? AND intents.account_registration_id = ?
                AND conversations.remote_dialog_id = ?
            """,
            (intent_id, key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id),
        ).fetchone()

    def _send_intent_row_for_id_sync(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT intents.*, conversations.remote_dialog_id
            FROM buyer_conversation_send_intents AS intents
            INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = intents.conversation_id
                AND conversations.platform = intents.platform
                AND conversations.account_registration_id = intents.account_registration_id
            WHERE intents.intent_id = ?
            """,
            (intent_id,),
        ).fetchone()

    def _send_intent_row_for_draft_sync(
        self,
        connection: sqlite3.Connection,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT intents.*, conversations.remote_dialog_id
            FROM buyer_conversation_send_intents AS intents
            INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = intents.conversation_id
                AND conversations.platform = intents.platform
                AND conversations.account_registration_id = intents.account_registration_id
            WHERE intents.conversation_id = ? AND intents.platform = ?
                AND intents.account_registration_id = ? AND conversations.remote_dialog_id = ?
                AND intents.draft_id = ?
            """,
            (key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id, draft_id),
        ).fetchone()

    def _draft_for_send_intent_sync(
        self,
        connection: sqlite3.Connection,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> BuyerConversationDraft | None:
        row = connection.execute(
            """
            SELECT drafts.*, conversations.platform, conversations.account_registration_id,
                conversations.remote_dialog_id
            FROM buyer_conversation_drafts AS drafts
            INNER JOIN buyer_conversations AS conversations ON conversations.conversation_id = drafts.conversation_id
            WHERE drafts.draft_id = ? AND drafts.conversation_id = ?
                AND conversations.platform = ? AND conversations.account_registration_id = ?
                AND conversations.remote_dialog_id = ?
            """,
            (draft_id, key.conversation_id, key.platform, key.account_registration_id, key.remote_dialog_id),
        ).fetchone()
        return _draft_from_row(row) if row is not None else None

    def _write_send_intent_sync(
        self,
        connection: sqlite3.Connection,
        intent: BuyerConversationSendIntent,
        now: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE buyer_conversation_send_intents
            SET state = ?, send_attempts = ?, remote_message_id = ?, remote_receipt = ?,
                failure_reason = ?, updated_at = ?
            WHERE intent_id = ? AND conversation_id = ? AND platform = ? AND account_registration_id = ?
            """,
            (
                intent.state.value,
                intent.send_attempts,
                intent.remote_message_id,
                intent.remote_receipt,
                intent.failure_reason,
                now,
                intent.intent_id,
                intent.key.conversation_id,
                intent.key.platform,
                intent.key.account_registration_id,
            ),
        )
        if cursor.rowcount != 1:
            raise BuyerConversationPersistenceNotFoundError("conversation send intent was not found")

    def _append_send_audit_event_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO buyer_conversation_send_audit_events (
                    intent_id, conversation_id, platform, account_registration_id,
                    event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    key.conversation_id,
                    key.platform,
                    key.account_registration_id,
                    event_type,
                    _dump_json(payload),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BuyerConversationPersistenceConflictError(
                "could not append account-bound conversation send audit event"
            ) from exc

    def _upsert_conversation_sync(
        self,
        connection: sqlite3.Connection,
        conversation: BuyerConversation,
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO buyer_conversations (
                    conversation_id, platform, account_registration_id, remote_dialog_id,
                    context_json, remote_title, last_remote_message_at, synced_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    context_json = excluded.context_json,
                    remote_title = excluded.remote_title,
                    last_remote_message_at = excluded.last_remote_message_at,
                    synced_at = excluded.synced_at,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation.conversation_id,
                    conversation.key.platform,
                    conversation.key.account_registration_id,
                    conversation.key.remote_dialog_id,
                    _dump_json(conversation.context.to_payload()),
                    conversation.remote_title,
                    conversation.last_remote_message_at,
                    conversation.synced_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BuyerConversationPersistenceConflictError(
                "conversation identity is already bound to a different account scope"
            ) from exc

    def _ensure_conversation_sync(
        self,
        connection: sqlite3.Connection,
        key: BuyerConversationKey,
        context: BuyerConversationContext,
        now: str,
    ) -> None:
        existing = connection.execute(
            "SELECT conversation_id FROM buyer_conversations WHERE conversation_id = ?",
            (key.conversation_id,),
        ).fetchone()
        if existing is not None:
            return
        self._upsert_conversation_sync(
            connection,
            BuyerConversation(key=key, context=context),
            now,
        )

    def _upsert_message_sync(
        self,
        connection: sqlite3.Connection,
        message: BuyerConversationMessage,
        now: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO buyer_conversation_messages (
                    message_id, conversation_id, platform, account_registration_id, remote_message_id,
                    direction, body, sender_account_registration_id, remote_sender_id,
                    remote_created_at, observed_at, delivery_state, read_at, context_json,
                    message_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    direction = excluded.direction,
                    body = excluded.body,
                    sender_account_registration_id = excluded.sender_account_registration_id,
                    remote_sender_id = excluded.remote_sender_id,
                    remote_created_at = excluded.remote_created_at,
                    observed_at = excluded.observed_at,
                    delivery_state = excluded.delivery_state,
                    read_at = excluded.read_at,
                    context_json = excluded.context_json,
                    message_hash = excluded.message_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    message.message_id,
                    message.conversation_id,
                    message.key.platform,
                    message.key.account_registration_id,
                    message.remote_message_id,
                    message.direction.value,
                    message.body,
                    message.sender_account_registration_id,
                    message.remote_sender_id,
                    message.remote_created_at,
                    message.observed_at,
                    message.delivery_state.value,
                    message.read_at,
                    _dump_json(message.context.to_payload()),
                    message.message_hash,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BuyerConversationPersistenceConflictError(
                "message identity is already bound to a different account-bound conversation"
            ) from exc
        attachment_keys: set[str] = set()
        for index, attachment_key, _attachment, attachment_json in _attachment_rows(message.attachments):
            attachment_keys.add(attachment_key)
            connection.execute(
                """
                INSERT INTO buyer_conversation_attachments (
                    message_id, attachment_key, attachment_index, attachment_json,
                    attachment_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, attachment_key) DO UPDATE SET
                    attachment_index = excluded.attachment_index,
                    attachment_json = excluded.attachment_json,
                    attachment_hash = excluded.attachment_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    message.message_id,
                    attachment_key,
                    index,
                    attachment_json,
                    sha256(attachment_json.encode("utf-8")).hexdigest(),
                    now,
                    now,
                ),
            )
        existing_attachment_rows = connection.execute(
            "SELECT attachment_key FROM buyer_conversation_attachments WHERE message_id = ?",
            (message.message_id,),
        ).fetchall()
        for row in existing_attachment_rows:
            if row["attachment_key"] not in attachment_keys:
                connection.execute(
                    """
                    DELETE FROM buyer_conversation_attachments
                    WHERE message_id = ? AND attachment_key = ?
                    """,
                    (message.message_id, row["attachment_key"]),
                )

    def _attachments_for_message_sync(
        self,
        connection: sqlite3.Connection,
        message_id: str,
    ) -> tuple[BuyerConversationAttachment, ...]:
        rows = connection.execute(
            """
            SELECT attachment_json FROM buyer_conversation_attachments
            WHERE message_id = ?
            ORDER BY attachment_index ASC, attachment_key ASC
            """,
            (message_id,),
        ).fetchall()
        return tuple(_attachment_from_payload(_load_json_mapping(row["attachment_json"], "attachment")) for row in rows)


def _attachment_rows(
    attachments: Iterable[BuyerConversationAttachment],
) -> Iterable[tuple[int, str, BuyerConversationAttachment, str]]:
    occurrences: dict[str, int] = {}
    for index, attachment in enumerate(attachments):
        payload_json = _dump_json(attachment.to_payload())
        if attachment.remote_attachment_id is not None:
            base_key = f"remote:{attachment.remote_attachment_id}"
        else:
            digest = sha256(payload_json.encode("utf-8")).hexdigest()
            base_key = f"payload:{digest}"
        occurrence = occurrences.get(base_key, 0)
        occurrences[base_key] = occurrence + 1
        attachment_key = f"{base_key}:{occurrence}"
        yield index, attachment_key, attachment, payload_json


def _cursor_from_row(row: sqlite3.Row) -> BuyerConversationCursor:
    try:
        return BuyerConversationCursor(
            platform=row["platform"],
            account_registration_id=row["account_registration_id"],
            cursor=row["cursor"],
            watermark=row["watermark"],
            revision=row["revision"],
            updated_at=row["updated_at"],
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored account cursor is invalid") from exc


def _conversation_from_row(row: sqlite3.Row) -> BuyerConversation:
    try:
        key = BuyerConversationKey(
            platform=row["platform"],
            account_registration_id=row["account_registration_id"],
            remote_dialog_id=row["remote_dialog_id"],
        )
        conversation = BuyerConversation(
            key=key,
            context=_context_from_payload(_load_json_mapping(row["context_json"], "conversation context")),
            remote_title=row["remote_title"],
            last_remote_message_at=row["last_remote_message_at"],
            synced_at=row["synced_at"],
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored conversation is invalid") from exc
    if conversation.conversation_id != row["conversation_id"]:
        raise BuyerConversationPersistenceError("stored conversation identity does not match its account-bound key")
    return conversation


def _message_from_row(
    row: sqlite3.Row,
    attachments: tuple[BuyerConversationAttachment, ...],
) -> BuyerConversationMessage:
    try:
        key = BuyerConversationKey(
            platform=row["platform"],
            account_registration_id=row["account_registration_id"],
            remote_dialog_id=row["remote_dialog_id"],
        )
        message = BuyerConversationMessage(
            key=key,
            remote_message_id=row["remote_message_id"],
            direction=row["direction"],
            body=row["body"],
            sender_account_registration_id=row["sender_account_registration_id"],
            remote_sender_id=row["remote_sender_id"],
            remote_created_at=row["remote_created_at"],
            observed_at=row["observed_at"],
            delivery_state=row["delivery_state"],
            read_at=row["read_at"],
            attachments=attachments,
            context=_context_from_payload(_load_json_mapping(row["context_json"], "message context")),
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored conversation message is invalid") from exc
    if message.message_id != row["message_id"]:
        raise BuyerConversationPersistenceError("stored message identity does not match its account-bound key")
    if message.message_hash != row["message_hash"]:
        raise BuyerConversationPersistenceError("stored message hash does not match its normalized payload")
    return message


def _draft_from_row(row: sqlite3.Row) -> BuyerConversationDraft:
    try:
        key = BuyerConversationKey(
            platform=row["platform"],
            account_registration_id=row["account_registration_id"],
            remote_dialog_id=row["remote_dialog_id"],
        )
        draft = BuyerConversationDraft(
            draft_id=row["draft_id"],
            key=key,
            sender_account_registration_id=row["sender_account_registration_id"],
            body=row["body"],
            context=_context_from_payload(_load_json_mapping(row["context_json"], "draft context")),
            created_at=row["draft_created_at"],
            source=row["source"],
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored reply draft is invalid") from exc
    if row["state"] != "draft":
        raise BuyerConversationPersistenceError("stored reply draft has an unsupported state")
    if draft.draft_hash != row["draft_hash"]:
        raise BuyerConversationPersistenceError("stored reply draft hash does not match its normalized payload")
    return draft


def _send_intent_from_row(row: sqlite3.Row) -> BuyerConversationSendIntent:
    try:
        key = BuyerConversationKey(
            platform=row["platform"],
            account_registration_id=row["account_registration_id"],
            remote_dialog_id=row["remote_dialog_id"],
        )
        intent = BuyerConversationSendIntent(
            intent_id=row["intent_id"],
            key=key,
            draft_id=row["draft_id"],
            sender_account_registration_id=row["sender_account_registration_id"],
            body=row["body"],
            context=_context_from_payload(_load_json_mapping(row["context_json"], "send intent context")),
            draft_hash=row["draft_hash"],
            requested_by=row["requested_by"],
            command_id=row["command_id"],
            requested_at=row["requested_at"],
            state=BuyerConversationSendState(row["state"]),
            send_attempts=row["send_attempts"],
            remote_message_id=row["remote_message_id"],
            remote_receipt=row["remote_receipt"],
            failure_reason=row["failure_reason"],
        )
    except (BuyerConversationError, BuyerConversationSendError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored conversation send intent is invalid") from exc
    if intent.key.conversation_id != row["conversation_id"]:
        raise BuyerConversationPersistenceError("stored send intent identity does not match its account-bound key")
    if intent.idempotency_key != row["idempotency_key"]:
        raise BuyerConversationPersistenceError("stored send intent idempotency key does not match immutable draft identity")
    return intent


def _send_audit_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": int(row["event_id"]),
        "intent_id": str(row["intent_id"]),
        "conversation_id": str(row["conversation_id"]),
        "platform": str(row["platform"]),
        "account_registration_id": str(row["account_registration_id"]),
        "event_type": str(row["event_type"]),
        "payload": dict(_load_json_mapping(row["payload_json"], "conversation send audit payload")),
        "created_at": str(row["created_at"]),
    }


def _validate_send_intent_draft(intent: BuyerConversationSendIntent, draft: BuyerConversationDraft) -> None:
    if (
        intent.key != draft.key
        or intent.draft_id != draft.draft_id
        or intent.sender_account_registration_id != draft.sender_account_registration_id
        or intent.body != draft.body
        or intent.context != draft.context
        or intent.draft_hash != draft.draft_hash
    ):
        raise BuyerConversationPersistenceConflictError(
            "conversation send intent does not match its immutable account-bound reply draft"
        )


def _validate_send_intent_transition(
    stored: BuyerConversationSendIntent,
    candidate: BuyerConversationSendIntent,
) -> None:
    if not _same_send_intent_identity(stored, candidate):
        raise BuyerConversationPersistenceConflictError(
            "conversation send intent cannot change its account, draft, body, or operator command"
        )
    if stored.state is BuyerConversationSendState.PENDING:
        if candidate.state is not BuyerConversationSendState.SENDING or candidate.send_attempts != stored.send_attempts + 1:
            raise BuyerConversationPersistenceConflictError("pending conversation send intent can only begin one delivery attempt")
        return
    if stored.state is BuyerConversationSendState.SENDING:
        if candidate.state not in {
            BuyerConversationSendState.SENDING,
            BuyerConversationSendState.SENT,
            BuyerConversationSendState.UNKNOWN,
            BuyerConversationSendState.FAILED,
        }:
            raise BuyerConversationPersistenceConflictError("in-flight conversation send intent has an invalid outcome")
        if candidate.send_attempts != stored.send_attempts:
            raise BuyerConversationPersistenceConflictError("in-flight conversation send intent cannot change send_attempts")
        return
    if stored.state is BuyerConversationSendState.UNKNOWN:
        if candidate.state not in {
            BuyerConversationSendState.UNKNOWN,
            BuyerConversationSendState.SENT,
            BuyerConversationSendState.FAILED,
        }:
            raise BuyerConversationPersistenceConflictError("unknown conversation send intent has an invalid reconciliation")
        if candidate.send_attempts != stored.send_attempts:
            raise BuyerConversationPersistenceConflictError("unknown conversation send intent cannot re-dispatch automatically")
        return
    if candidate.to_payload() != stored.to_payload():
        raise BuyerConversationPersistenceConflictError("terminal conversation send intent cannot be changed")


def _same_send_intent_identity(left: BuyerConversationSendIntent, right: BuyerConversationSendIntent) -> bool:
    return (
        left.intent_id == right.intent_id
        and left.key == right.key
        and left.draft_id == right.draft_id
        and left.sender_account_registration_id == right.sender_account_registration_id
        and left.body == right.body
        and left.context == right.context
        and left.draft_hash == right.draft_hash
        and left.requested_by == right.requested_by
        and left.command_id == right.command_id
        and left.requested_at == right.requested_at
        and left.idempotency_key == right.idempotency_key
    )


def _context_from_payload(payload: Mapping[str, Any]) -> BuyerConversationContext:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise BuyerConversationPersistenceError("stored conversation context metadata must be a mapping")
    try:
        return BuyerConversationContext(
            project_id=payload.get("project_id"),
            proposal_draft_id=payload.get("proposal_draft_id"),
            proposal_intent_id=payload.get("proposal_intent_id"),
            buyer_remote_user_id=payload.get("buyer_remote_user_id"),
            metadata=metadata,
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored conversation context is invalid") from exc


def _attachment_from_payload(payload: Mapping[str, Any]) -> BuyerConversationAttachment:
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise BuyerConversationPersistenceError("stored attachment metadata must be a mapping")
    try:
        return BuyerConversationAttachment(
            remote_attachment_id=payload.get("remote_attachment_id"),
            filename=payload.get("filename"),
            content_type=payload.get("content_type"),
            remote_url=payload.get("remote_url"),
            metadata=metadata,
        )
    except (BuyerConversationError, TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("stored attachment is invalid") from exc


def _load_json_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise BuyerConversationPersistenceError(f"stored {name} JSON is missing")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BuyerConversationPersistenceError(f"stored {name} JSON is invalid") from exc
    if not isinstance(payload, Mapping):
        raise BuyerConversationPersistenceError(f"stored {name} JSON must be an object")
    return payload


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BuyerConversationPersistenceError("value cannot be serialized as durable JSON") from exc


def _scope_cursor(platform: str, account_registration_id: str) -> BuyerConversationCursor:
    try:
        return BuyerConversationCursor(platform=platform, account_registration_id=account_registration_id)
    except (BuyerConversationError, TypeError, ValueError):
        raise


def _require_key(key: BuyerConversationKey) -> None:
    if not isinstance(key, BuyerConversationKey):
        raise TypeError("key must be a BuyerConversationKey")


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BuyerConversationPersistenceError(f"{name} cannot be blank")
    return normalized


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    return max(1, min(value, 5_000))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


BuyerConversationSQLiteStore = SQLiteBuyerConversationStore


__all__ = [
    "BuyerConversationPersistenceConflictError",
    "BuyerConversationPersistenceError",
    "BuyerConversationPersistenceNotFoundError",
    "BuyerConversationSQLiteStore",
    "SQLiteBuyerConversationStore",
]
