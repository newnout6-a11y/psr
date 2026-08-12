"""Pure account-bound Buyer Search conversation records and sync helpers.

The module intentionally contains no transport or send operation.  A remote
outgoing message can be normalized after it is observed, while any locally
created reply remains an explicit draft for an account-scoped editor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class BuyerConversationError(ValueError):
    """Raised when a conversation record is incomplete or crosses account scope."""


class BuyerConversationCursorConflict(BuyerConversationError):
    """Raised when equally-versioned account cursors disagree."""


class BuyerConversationMessageDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class BuyerConversationDeliveryState(StrEnum):
    UNKNOWN = "unknown"
    RECEIVED = "received"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BuyerConversationKey:
    """The durable uniqueness key for one account's remote dialog."""

    platform: str
    account_registration_id: str
    remote_dialog_id: str
    conversation_id: str = field(init=False)

    def __post_init__(self) -> None:
        platform = _required_text(self.platform, "platform")
        account_registration_id = _required_text(self.account_registration_id, "account_registration_id")
        remote_dialog_id = _required_text(self.remote_dialog_id, "remote_dialog_id")
        conversation_id = _conversation_id(platform, account_registration_id, remote_dialog_id)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "account_registration_id", account_registration_id)
        object.__setattr__(self, "remote_dialog_id", remote_dialog_id)
        object.__setattr__(self, "conversation_id", conversation_id)

    @property
    def account_scope(self) -> tuple[str, str]:
        return (self.platform, self.account_registration_id)

    def to_payload(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "platform": self.platform,
            "account_registration_id": self.account_registration_id,
            "remote_dialog_id": self.remote_dialog_id,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationContext:
    """Optional Buyer Search links that travel with a dialog and its messages."""

    project_id: str | None = None
    proposal_draft_id: str | None = None
    proposal_intent_id: str | None = None
    buyer_remote_user_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "proposal_draft_id", "proposal_intent_id", "buyer_remote_user_id"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze_json_value(_normalize_json_value(self.metadata, "metadata")))

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "proposal_draft_id": self.proposal_draft_id,
            "proposal_intent_id": self.proposal_intent_id,
            "buyer_remote_user_id": self.buyer_remote_user_id,
            "metadata": _thaw_json_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BuyerConversation:
    """Persistence-ready account-bound dialog with Buyer Search context."""

    key: BuyerConversationKey
    context: BuyerConversationContext = field(default_factory=BuyerConversationContext)
    remote_title: str | None = None
    last_remote_message_at: str | None = None
    synced_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, BuyerConversationKey):
            raise TypeError("key must be a BuyerConversationKey")
        if not isinstance(self.context, BuyerConversationContext):
            raise TypeError("context must be a BuyerConversationContext")
        object.__setattr__(self, "remote_title", _optional_text(self.remote_title, "remote_title"))
        object.__setattr__(self, "last_remote_message_at", _optional_text(self.last_remote_message_at, "last_remote_message_at"))
        object.__setattr__(self, "synced_at", _optional_text(self.synced_at, "synced_at"))

    @property
    def conversation_id(self) -> str:
        return self.key.conversation_id

    def with_context(self, context: BuyerConversationContext) -> BuyerConversation:
        if not isinstance(context, BuyerConversationContext):
            raise TypeError("context must be a BuyerConversationContext")
        return replace(self, context=context)

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.key.to_payload(),
            "context": self.context.to_payload(),
            "remote_title": self.remote_title,
            "last_remote_message_at": self.last_remote_message_at,
            "synced_at": self.synced_at,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationCursor:
    """An opaque inbox position scoped to exactly one platform/account pair."""

    platform: str
    account_registration_id: str
    cursor: str | None = None
    watermark: str | None = None
    revision: int = 0
    updated_at: str | None = None

    def __post_init__(self) -> None:
        platform = _required_text(self.platform, "platform")
        account_registration_id = _required_text(self.account_registration_id, "account_registration_id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise BuyerConversationError("revision must be a non-negative integer")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "account_registration_id", account_registration_id)
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "watermark", _optional_text(self.watermark, "watermark"))
        object.__setattr__(self, "updated_at", _optional_text(self.updated_at, "updated_at"))

    @property
    def account_scope(self) -> tuple[str, str]:
        return (self.platform, self.account_registration_id)

    @classmethod
    def for_conversation(
        cls,
        key: BuyerConversationKey,
        *,
        cursor: str | None = None,
        watermark: str | None = None,
        revision: int = 0,
        updated_at: str | None = None,
    ) -> BuyerConversationCursor:
        if not isinstance(key, BuyerConversationKey):
            raise TypeError("key must be a BuyerConversationKey")
        return cls(
            platform=key.platform,
            account_registration_id=key.account_registration_id,
            cursor=cursor,
            watermark=watermark,
            revision=revision,
            updated_at=updated_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account_registration_id": self.account_registration_id,
            "cursor": self.cursor,
            "watermark": self.watermark,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationAttachment:
    """Immutable attachment manifest item retained with a message."""

    remote_attachment_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    remote_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        remote_attachment_id = _optional_text(self.remote_attachment_id, "remote_attachment_id")
        filename = _optional_text(self.filename, "filename")
        content_type = _optional_text(self.content_type, "content_type")
        remote_url = _optional_text(self.remote_url, "remote_url")
        if remote_attachment_id is None and filename is None and remote_url is None:
            raise BuyerConversationError("an attachment requires a remote_attachment_id, filename, or remote_url")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "remote_attachment_id", remote_attachment_id)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "content_type", content_type.casefold() if content_type else None)
        object.__setattr__(self, "remote_url", remote_url)
        object.__setattr__(self, "metadata", _freeze_json_value(_normalize_json_value(self.metadata, "metadata")))

    def to_payload(self) -> dict[str, Any]:
        return {
            "remote_attachment_id": self.remote_attachment_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "remote_url": self.remote_url,
            "metadata": _thaw_json_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationMessage:
    """A remote message normalized under an account-bound conversation key."""

    key: BuyerConversationKey
    remote_message_id: str
    direction: BuyerConversationMessageDirection
    body: str | None = None
    sender_account_registration_id: str | None = None
    remote_sender_id: str | None = None
    remote_created_at: str | None = None
    observed_at: str | None = None
    delivery_state: BuyerConversationDeliveryState = BuyerConversationDeliveryState.UNKNOWN
    read_at: str | None = None
    attachments: tuple[BuyerConversationAttachment, ...] = ()
    context: BuyerConversationContext = field(default_factory=BuyerConversationContext)
    message_id: str = field(init=False)
    message_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, BuyerConversationKey):
            raise TypeError("key must be a BuyerConversationKey")
        remote_message_id = _required_text(self.remote_message_id, "remote_message_id")
        try:
            direction = BuyerConversationMessageDirection(self.direction)
        except (TypeError, ValueError) as exc:
            raise BuyerConversationError("direction must be an incoming or outgoing value") from exc
        try:
            delivery_state = BuyerConversationDeliveryState(self.delivery_state)
        except (TypeError, ValueError) as exc:
            raise BuyerConversationError("delivery_state must be a known delivery state") from exc
        if not isinstance(self.context, BuyerConversationContext):
            raise TypeError("context must be a BuyerConversationContext")
        attachments = _normalize_attachments(self.attachments)
        body = normalize_conversation_text(self.body)
        if not body and not attachments:
            raise BuyerConversationError("a message requires body text or an attachment")
        sender_account_registration_id = _optional_text(
            self.sender_account_registration_id,
            "sender_account_registration_id",
        )
        remote_sender_id = _optional_text(self.remote_sender_id, "remote_sender_id")
        if direction is BuyerConversationMessageDirection.OUTGOING:
            if sender_account_registration_id is None:
                raise BuyerConversationError("outgoing messages require sender_account_registration_id")
            if sender_account_registration_id != self.key.account_registration_id:
                raise BuyerConversationError("outgoing sender_account_registration_id must match the conversation account")
        elif sender_account_registration_id is not None:
            raise BuyerConversationError("incoming messages cannot declare sender_account_registration_id")
        message_id = _message_id(self.key, remote_message_id)
        payload = {
            "key": self.key.to_payload(),
            "remote_message_id": remote_message_id,
            "direction": direction.value,
            "body": body,
            "sender_account_registration_id": sender_account_registration_id,
            "remote_sender_id": remote_sender_id,
            "remote_created_at": _optional_text(self.remote_created_at, "remote_created_at"),
            "observed_at": _optional_text(self.observed_at, "observed_at"),
            "delivery_state": delivery_state.value,
            "read_at": _optional_text(self.read_at, "read_at"),
            "attachments": [attachment.to_payload() for attachment in attachments],
            "context": self.context.to_payload(),
        }
        object.__setattr__(self, "remote_message_id", remote_message_id)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "sender_account_registration_id", sender_account_registration_id)
        object.__setattr__(self, "remote_sender_id", remote_sender_id)
        object.__setattr__(self, "remote_created_at", payload["remote_created_at"])
        object.__setattr__(self, "observed_at", payload["observed_at"])
        object.__setattr__(self, "delivery_state", delivery_state)
        object.__setattr__(self, "read_at", payload["read_at"])
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "message_hash", _hash_json(payload))

    @property
    def conversation_id(self) -> str:
        return self.key.conversation_id

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.key.to_payload(),
            "message_id": self.message_id,
            "remote_message_id": self.remote_message_id,
            "direction": self.direction.value,
            "body": self.body,
            "sender_account_registration_id": self.sender_account_registration_id,
            "remote_sender_id": self.remote_sender_id,
            "remote_created_at": self.remote_created_at,
            "observed_at": self.observed_at,
            "delivery_state": self.delivery_state.value,
            "read_at": self.read_at,
            "attachments": [attachment.to_payload() for attachment in self.attachments],
            "context": self.context.to_payload(),
            "message_hash": self.message_hash,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationDraft:
    """Account-pinned editor content; this type cannot represent a sent message."""

    draft_id: str
    key: BuyerConversationKey
    sender_account_registration_id: str
    body: str | None = None
    context: BuyerConversationContext = field(default_factory=BuyerConversationContext)
    created_at: str | None = None
    source: str = "ai"
    draft_hash: str = field(init=False)
    state: str = field(init=False, default="draft")

    def __post_init__(self) -> None:
        draft_id = _required_text(self.draft_id, "draft_id")
        if not isinstance(self.key, BuyerConversationKey):
            raise TypeError("key must be a BuyerConversationKey")
        sender_account_registration_id = _required_text(
            self.sender_account_registration_id,
            "sender_account_registration_id",
        )
        if sender_account_registration_id != self.key.account_registration_id:
            raise BuyerConversationError("draft sender_account_registration_id must match the conversation account")
        if not isinstance(self.context, BuyerConversationContext):
            raise TypeError("context must be a BuyerConversationContext")
        body = normalize_conversation_text(self.body)
        created_at = _optional_text(self.created_at, "created_at")
        source = _required_text(self.source, "source")
        draft_hash = _hash_json(
            {
                "draft_id": draft_id,
                "key": self.key.to_payload(),
                "sender_account_registration_id": sender_account_registration_id,
                "body": body,
                "context": self.context.to_payload(),
                "created_at": created_at,
                "source": source,
                "state": "draft",
            }
        )
        object.__setattr__(self, "draft_id", draft_id)
        object.__setattr__(self, "sender_account_registration_id", sender_account_registration_id)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "draft_hash", draft_hash)

    @property
    def conversation_id(self) -> str:
        return self.key.conversation_id

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.key.to_payload(),
            "draft_id": self.draft_id,
            "sender_account_registration_id": self.sender_account_registration_id,
            "body": self.body,
            "context": self.context.to_payload(),
            "created_at": self.created_at,
            "source": self.source,
            "state": self.state,
            "draft_hash": self.draft_hash,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationSyncBatch:
    """A persistence-ready page of account-scoped dialog and message updates."""

    cursor: BuyerConversationCursor
    conversations: tuple[BuyerConversation, ...] = ()
    messages: tuple[BuyerConversationMessage, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, BuyerConversationCursor):
            raise TypeError("cursor must be a BuyerConversationCursor")
        conversations = tuple(self.conversations)
        messages = tuple(self.messages)
        conversation_ids: set[str] = set()
        for conversation in conversations:
            if not isinstance(conversation, BuyerConversation):
                raise TypeError("conversations must contain BuyerConversation values")
            _require_account_scope(conversation.key, self.cursor)
            if conversation.conversation_id in conversation_ids:
                raise BuyerConversationError("sync batch contains duplicate conversations")
            conversation_ids.add(conversation.conversation_id)
        message_ids: set[str] = set()
        for message in messages:
            if not isinstance(message, BuyerConversationMessage):
                raise TypeError("messages must contain BuyerConversationMessage values")
            _require_account_scope(message.key, self.cursor)
            if message.message_id in message_ids:
                raise BuyerConversationError("sync batch contains duplicate messages")
            message_ids.add(message.message_id)
        object.__setattr__(self, "conversations", conversations)
        object.__setattr__(self, "messages", messages)

    def to_payload(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor.to_payload(),
            "conversations": [conversation.to_payload() for conversation in self.conversations],
            "messages": [message.to_payload() for message in self.messages],
        }


def normalize_conversation_text(value: str | None) -> str:
    """Normalize line endings and boundary whitespace without flattening paragraphs."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("message body must be a string or None")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    normalized: list[str] = []
    blank = False
    for line in lines:
        if line:
            normalized.append(line)
            blank = False
        elif not blank:
            normalized.append("")
            blank = True
    return "\n".join(normalized)


def normalize_incoming_message(
    payload: Mapping[str, Any] | None = None,
    *,
    conversation_key: BuyerConversationKey | None = None,
    key: BuyerConversationKey | None = None,
    remote_message_id: str | None = None,
    body: str | None = None,
    remote_sender_id: str | None = None,
    remote_created_at: str | None = None,
    observed_at: str | None = None,
    delivery_state: BuyerConversationDeliveryState | str | None = None,
    read_at: str | None = None,
    attachments: Iterable[BuyerConversationAttachment | Mapping[str, Any]] | None = None,
    context: BuyerConversationContext | None = None,
) -> BuyerConversationMessage:
    """Normalize a remotely observed incoming message without mutating the remote dialog."""

    raw = _optional_payload(payload)
    resolved_key = _resolve_conversation_key(conversation_key=conversation_key, key=key)
    return BuyerConversationMessage(
        key=resolved_key,
        remote_message_id=(
            remote_message_id
            if remote_message_id is not None
            else _payload_required_text(raw, "remote_message_id", "message_id", "id")
        ),
        direction=BuyerConversationMessageDirection.INCOMING,
        body=body if body is not None else _payload_optional_text(raw, "body", "text", "message", "content"),
        remote_sender_id=remote_sender_id if remote_sender_id is not None else _payload_optional_text(raw, "remote_sender_id", "sender_id", "from_id"),
        remote_created_at=remote_created_at if remote_created_at is not None else _payload_optional_text(raw, "remote_created_at", "created_at", "sent_at", "date"),
        observed_at=observed_at if observed_at is not None else _payload_optional_text(raw, "observed_at", "received_at"),
        delivery_state=delivery_state or _payload_delivery_state(raw, BuyerConversationDeliveryState.RECEIVED),
        read_at=read_at if read_at is not None else _payload_optional_text(raw, "read_at"),
        attachments=attachments if attachments is not None else _payload_attachments(raw),
        context=context or BuyerConversationContext(),
    )


def normalize_outgoing_message(
    payload: Mapping[str, Any] | None = None,
    *,
    conversation_key: BuyerConversationKey | None = None,
    key: BuyerConversationKey | None = None,
    sender_account_registration_id: str,
    remote_message_id: str | None = None,
    body: str | None = None,
    remote_created_at: str | None = None,
    observed_at: str | None = None,
    delivery_state: BuyerConversationDeliveryState | str | None = None,
    read_at: str | None = None,
    attachments: Iterable[BuyerConversationAttachment | Mapping[str, Any]] | None = None,
    context: BuyerConversationContext | None = None,
) -> BuyerConversationMessage:
    """Normalize an already-observed outgoing message; it never sends one remotely."""

    raw = _optional_payload(payload)
    resolved_key = _resolve_conversation_key(conversation_key=conversation_key, key=key)
    return BuyerConversationMessage(
        key=resolved_key,
        remote_message_id=(
            remote_message_id
            if remote_message_id is not None
            else _payload_required_text(raw, "remote_message_id", "message_id", "id")
        ),
        direction=BuyerConversationMessageDirection.OUTGOING,
        body=body if body is not None else _payload_optional_text(raw, "body", "text", "message", "content"),
        sender_account_registration_id=sender_account_registration_id,
        remote_created_at=remote_created_at if remote_created_at is not None else _payload_optional_text(raw, "remote_created_at", "created_at", "sent_at", "date"),
        observed_at=observed_at if observed_at is not None else _payload_optional_text(raw, "observed_at"),
        delivery_state=delivery_state or _payload_delivery_state(raw, BuyerConversationDeliveryState.SENT),
        read_at=read_at if read_at is not None else _payload_optional_text(raw, "read_at"),
        attachments=attachments if attachments is not None else _payload_attachments(raw),
        context=context or BuyerConversationContext(),
    )


def create_outgoing_draft(
    *,
    draft_id: str,
    conversation_key: BuyerConversationKey | None = None,
    key: BuyerConversationKey | None = None,
    sender_account_registration_id: str,
    body: str | None = None,
    context: BuyerConversationContext | None = None,
    created_at: str | None = None,
    source: str = "ai",
) -> BuyerConversationDraft:
    """Create editor-only reply content.  Transport code must handle sending elsewhere."""

    return BuyerConversationDraft(
        draft_id=draft_id,
        key=_resolve_conversation_key(conversation_key=conversation_key, key=key),
        sender_account_registration_id=sender_account_registration_id,
        body=body,
        context=context or BuyerConversationContext(),
        created_at=created_at,
        source=source,
    )


def advance_account_cursor(
    current: BuyerConversationCursor,
    *,
    cursor: str | None,
    watermark: str | None = None,
    updated_at: str | None = None,
) -> BuyerConversationCursor:
    """Create the next persisted position for the same account-only inbox scope."""

    if not isinstance(current, BuyerConversationCursor):
        raise TypeError("current must be a BuyerConversationCursor")
    return BuyerConversationCursor(
        platform=current.platform,
        account_registration_id=current.account_registration_id,
        cursor=cursor,
        watermark=watermark if watermark is not None else current.watermark,
        revision=current.revision + 1,
        updated_at=updated_at,
    )


def merge_account_cursors(
    current: BuyerConversationCursor,
    candidate: BuyerConversationCursor,
) -> BuyerConversationCursor:
    """Select a newer account cursor while rejecting cross-account or tie conflicts."""

    if not isinstance(current, BuyerConversationCursor) or not isinstance(candidate, BuyerConversationCursor):
        raise TypeError("current and candidate must be BuyerConversationCursor values")
    if current.account_scope != candidate.account_scope:
        raise BuyerConversationError("cannot merge cursors from different account scopes")
    if candidate.revision > current.revision:
        return candidate
    if candidate.revision < current.revision:
        return current
    if candidate != current:
        raise BuyerConversationCursorConflict("equally-versioned account cursors disagree")
    return current


def build_conversation_sync_batch(
    *,
    cursor: BuyerConversationCursor,
    conversations: Iterable[BuyerConversation] = (),
    messages: Iterable[BuyerConversationMessage] = (),
) -> BuyerConversationSyncBatch:
    """Collect one account-scoped sync result for an atomic repository commit."""

    return BuyerConversationSyncBatch(
        cursor=cursor,
        conversations=tuple(conversations),
        messages=tuple(messages),
    )


def _conversation_id(platform: str, account_registration_id: str, remote_dialog_id: str) -> str:
    identity = "\x00".join(("buyer-conversation-v1", platform, account_registration_id, remote_dialog_id))
    return f"buyer-conversation-{uuid5(NAMESPACE_URL, identity).hex}"


def _message_id(key: BuyerConversationKey, remote_message_id: str) -> str:
    identity = "\x00".join(("buyer-message-v1", key.platform, key.account_registration_id, key.remote_dialog_id, remote_message_id))
    return f"buyer-message-{uuid5(NAMESPACE_URL, identity).hex}"


def _resolve_conversation_key(
    *,
    conversation_key: BuyerConversationKey | None,
    key: BuyerConversationKey | None,
) -> BuyerConversationKey:
    if conversation_key is not None and key is not None and conversation_key != key:
        raise BuyerConversationError("conversation_key and key disagree")
    resolved = conversation_key or key
    if not isinstance(resolved, BuyerConversationKey):
        raise TypeError("conversation_key must be a BuyerConversationKey")
    return resolved


def _optional_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if payload is None:
        return MappingProxyType({})
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping or None")
    return payload


def _payload_required_text(payload: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return _required_text(value, name)
    raise BuyerConversationError(f"payload requires one of: {', '.join(names)}")


def _payload_optional_text(payload: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name in payload and payload[name] is not None:
            return _optional_text(payload[name], name)
    return None


def _payload_delivery_state(
    payload: Mapping[str, Any],
    default: BuyerConversationDeliveryState,
) -> BuyerConversationDeliveryState | str:
    for name in ("delivery_state", "status", "state"):
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _payload_attachments(payload: Mapping[str, Any]) -> Iterable[BuyerConversationAttachment | Mapping[str, Any]]:
    for name in ("attachments", "files"):
        if name in payload and payload[name] is not None:
            attachments = payload[name]
            if isinstance(attachments, (str, bytes)) or not isinstance(attachments, Iterable):
                raise TypeError(f"payload {name} must be an iterable of attachments")
            return attachments
    return ()


def _normalize_attachments(
    attachments: Iterable[BuyerConversationAttachment | Mapping[str, Any]],
) -> tuple[BuyerConversationAttachment, ...]:
    if isinstance(attachments, (str, bytes)) or not isinstance(attachments, Iterable):
        raise TypeError("attachments must be an iterable")
    normalized: list[BuyerConversationAttachment] = []
    for item in attachments:
        if isinstance(item, BuyerConversationAttachment):
            normalized.append(item)
        elif isinstance(item, Mapping):
            normalized.append(
                BuyerConversationAttachment(
                    remote_attachment_id=_payload_optional_text(item, "remote_attachment_id", "attachment_id", "id"),
                    filename=_payload_optional_text(item, "filename", "name"),
                    content_type=_payload_optional_text(item, "content_type", "mime_type", "type"),
                    remote_url=_payload_optional_text(item, "remote_url", "url", "download_url"),
                    metadata=_attachment_metadata(item),
                )
            )
        else:
            raise TypeError("attachments must contain BuyerConversationAttachment values or mappings")
    return tuple(normalized)


def _attachment_metadata(item: Mapping[str, Any]) -> Mapping[str, Any]:
    if "metadata" in item:
        metadata = item["metadata"]
        if not isinstance(metadata, Mapping):
            raise TypeError("attachment metadata must be a mapping")
        return metadata
    ignored = {
        "remote_attachment_id",
        "attachment_id",
        "id",
        "filename",
        "name",
        "content_type",
        "mime_type",
        "type",
        "remote_url",
        "url",
        "download_url",
    }
    return {str(key): value for key, value in item.items() if key not in ignored}


def _require_account_scope(key: BuyerConversationKey, cursor: BuyerConversationCursor) -> None:
    if key.account_scope != cursor.account_scope:
        raise BuyerConversationError("conversation data does not belong to the sync cursor account")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise BuyerConversationError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _normalize_json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError(f"{name} keys must be strings")
            normalized[key] = _normalize_json_value(value[key], name)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item, name) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BuyerConversationError(f"{name} numbers must be finite")
        return value
    raise TypeError(f"unsupported {name} value type: {type(value).__name__}")


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _hash_json(value: Mapping[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "BuyerConversation",
    "BuyerConversationAttachment",
    "BuyerConversationContext",
    "BuyerConversationCursor",
    "BuyerConversationCursorConflict",
    "BuyerConversationDeliveryState",
    "BuyerConversationDraft",
    "BuyerConversationError",
    "BuyerConversationKey",
    "BuyerConversationMessage",
    "BuyerConversationMessageDirection",
    "BuyerConversationSyncBatch",
    "advance_account_cursor",
    "build_conversation_sync_batch",
    "create_outgoing_draft",
    "merge_account_cursors",
    "normalize_conversation_text",
    "normalize_incoming_message",
    "normalize_outgoing_message",
]
