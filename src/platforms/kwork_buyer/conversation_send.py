"""Explicit, account-bound delivery for an already-persisted Buyer reply draft.

The normal conversation flow remains draft-only.  This module is deliberately
separate so that a remote mutation is possible only through an injected,
per-account capability and a single operator-confirmed command.  It never
keeps a global client, creates background delivery work, or retries a request
whose remote outcome is ambiguous.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
import inspect
import json
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, uuid5

from .conversations import (
    BuyerConversationContext,
    BuyerConversationDeliveryState,
    BuyerConversationDraft,
    BuyerConversationKey,
    BuyerConversationMessage,
    BuyerConversationMessageDirection,
    normalize_conversation_text,
)


class BuyerConversationSendError(ValueError):
    """Raised when an explicit Buyer conversation delivery is unsafe to perform."""


class BuyerConversationSendNotFoundError(BuyerConversationSendError):
    """Raised when an account-scoped draft or send intent is absent."""


class BuyerConversationSendConflictError(BuyerConversationSendError):
    """Raised when a retry would change or duplicate an immutable send intent."""


class BuyerConversationSendDisabledError(BuyerConversationSendError):
    """Raised unless the dedicated delivery slice is explicitly enabled."""


class BuyerConversationSendCapabilityError(BuyerConversationSendError):
    """Raised when an injected sender is not pinned to the requested account."""


class BuyerConversationSendState(StrEnum):
    """Durable terminal and reconciliation states for one remote delivery."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BuyerConversationSendProvenance:
    """Identity evidence required from a one-account sender capability."""

    worker_id: str
    account_registration_id: str
    transport_id: str
    egress_ip: str
    source: str = "conversation_send"

    def __post_init__(self) -> None:
        for name in ("worker_id", "account_registration_id", "transport_id", "egress_ip", "source"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))

    def to_payload(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "account_registration_id": self.account_registration_id,
            "transport_id": self.transport_id,
            "egress_ip": self.egress_ip,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationRemoteSendReceipt:
    """Normalized evidence returned by a narrow remote send capability.

    A successful transport response without a remote message identity is kept
    ``unknown``.  The controller intentionally does not retry it, because the
    original message may already have reached the buyer.
    """

    accepted: bool | None
    remote_message_id: str | None = None
    remote_receipt: str | None = None
    delivery_state: BuyerConversationDeliveryState = BuyerConversationDeliveryState.SENT
    observed_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.accepted is not None and not isinstance(self.accepted, bool):
            raise TypeError("accepted must be a boolean or None")
        remote_message_id = _optional_text(self.remote_message_id, "remote_message_id")
        remote_receipt = _optional_text(self.remote_receipt, "remote_receipt")
        observed_at = _optional_text(self.observed_at, "observed_at")
        try:
            delivery_state = BuyerConversationDeliveryState(self.delivery_state)
        except (TypeError, ValueError) as exc:
            raise BuyerConversationSendError("delivery_state must be a known message delivery state") from exc
        if delivery_state is BuyerConversationDeliveryState.RECEIVED:
            raise BuyerConversationSendError("outgoing delivery_state cannot be received")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "remote_message_id", remote_message_id)
        object.__setattr__(self, "remote_receipt", remote_receipt)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "delivery_state", delivery_state)
        object.__setattr__(self, "metadata", _freeze_json(_normalize_mapping(self.metadata, "metadata")))

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "remote_message_id": self.remote_message_id,
            "remote_receipt": self.remote_receipt,
            "delivery_state": self.delivery_state.value,
            "observed_at": self.observed_at,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BuyerConversationSendIntent:
    """A durable, immutable request to deliver one already-saved reply draft."""

    intent_id: str
    key: BuyerConversationKey
    draft_id: str
    sender_account_registration_id: str
    body: str
    context: BuyerConversationContext
    draft_hash: str
    requested_by: str
    command_id: str
    requested_at: str
    state: BuyerConversationSendState = BuyerConversationSendState.PENDING
    send_attempts: int = 0
    remote_message_id: str | None = None
    remote_receipt: str | None = None
    failure_reason: str | None = None
    idempotency_key: str = field(init=False)

    def __post_init__(self) -> None:
        intent_id = _required_text(self.intent_id, "intent_id")
        if not isinstance(self.key, BuyerConversationKey):
            raise TypeError("key must be a BuyerConversationKey")
        draft_id = _required_text(self.draft_id, "draft_id")
        sender = _required_text(self.sender_account_registration_id, "sender_account_registration_id")
        if sender != self.key.account_registration_id:
            raise BuyerConversationSendError("sender_account_registration_id must match the conversation account")
        body = normalize_conversation_text(self.body)
        if not body:
            raise BuyerConversationSendError("send intent body cannot be blank")
        if not isinstance(self.context, BuyerConversationContext):
            raise TypeError("context must be a BuyerConversationContext")
        draft_hash = _required_hash(self.draft_hash, "draft_hash")
        requested_by = _required_text(self.requested_by, "requested_by")
        command_id = _required_text(self.command_id, "command_id")
        requested_at = _required_text(self.requested_at, "requested_at")
        try:
            state = BuyerConversationSendState(self.state)
        except (TypeError, ValueError) as exc:
            raise BuyerConversationSendError("state must be a known send state") from exc
        if isinstance(self.send_attempts, bool) or not isinstance(self.send_attempts, int) or self.send_attempts < 0:
            raise BuyerConversationSendError("send_attempts must be a non-negative integer")
        remote_message_id = _optional_text(self.remote_message_id, "remote_message_id")
        remote_receipt = _optional_text(self.remote_receipt, "remote_receipt")
        failure_reason = _optional_text(self.failure_reason, "failure_reason")
        _validate_state_fields(
            state=state,
            send_attempts=self.send_attempts,
            remote_message_id=remote_message_id,
            failure_reason=failure_reason,
        )
        idempotency_key = _intent_idempotency_key(self.key, draft_id, draft_hash)
        object.__setattr__(self, "intent_id", intent_id)
        object.__setattr__(self, "draft_id", draft_id)
        object.__setattr__(self, "sender_account_registration_id", sender)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "draft_hash", draft_hash)
        object.__setattr__(self, "requested_by", requested_by)
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "remote_message_id", remote_message_id)
        object.__setattr__(self, "remote_receipt", remote_receipt)
        object.__setattr__(self, "failure_reason", failure_reason)
        object.__setattr__(self, "idempotency_key", idempotency_key)

    @classmethod
    def from_draft(
        cls,
        *,
        draft: BuyerConversationDraft,
        requested_by: str,
        command_id: str,
        requested_at: str | None = None,
    ) -> BuyerConversationSendIntent:
        if not isinstance(draft, BuyerConversationDraft):
            raise TypeError("draft must be a BuyerConversationDraft")
        identity = "\x00".join(("buyer-conversation-send-v1", draft.conversation_id, draft.draft_id, draft.draft_hash))
        return cls(
            intent_id=f"buyer-conversation-send-{uuid5(NAMESPACE_URL, identity).hex}",
            key=draft.key,
            draft_id=draft.draft_id,
            sender_account_registration_id=draft.sender_account_registration_id,
            body=draft.body or "",
            context=draft.context,
            draft_hash=draft.draft_hash,
            requested_by=requested_by,
            command_id=command_id,
            requested_at=requested_at or _utc_now(),
        )

    def begin_send(self) -> BuyerConversationSendIntent:
        if self.state is not BuyerConversationSendState.PENDING:
            raise BuyerConversationSendConflictError("only a pending conversation send intent can start delivery")
        return replace(self, state=BuyerConversationSendState.SENDING, send_attempts=self.send_attempts + 1)

    def mark_sent(self, receipt: BuyerConversationRemoteSendReceipt) -> BuyerConversationSendIntent:
        if self.state not in {BuyerConversationSendState.SENDING, BuyerConversationSendState.UNKNOWN}:
            raise BuyerConversationSendConflictError("only an in-flight or unknown send intent can become sent")
        if receipt.accepted is not True or receipt.remote_message_id is None:
            raise BuyerConversationSendError("a sent conversation message requires accepted receipt and remote_message_id")
        return replace(
            self,
            state=BuyerConversationSendState.SENT,
            remote_message_id=receipt.remote_message_id,
            remote_receipt=receipt.remote_receipt,
            failure_reason=None,
        )

    def mark_unknown(
        self,
        *,
        remote_receipt: str | None = None,
        reason: str | None = None,
    ) -> BuyerConversationSendIntent:
        if self.state not in {BuyerConversationSendState.SENDING, BuyerConversationSendState.UNKNOWN}:
            raise BuyerConversationSendConflictError("only an in-flight or unknown send intent can remain unknown")
        return replace(
            self,
            state=BuyerConversationSendState.UNKNOWN,
            remote_message_id=None,
            remote_receipt=remote_receipt or self.remote_receipt,
            failure_reason=reason or self.failure_reason,
        )

    def mark_failed(self, reason: str, *, remote_receipt: str | None = None) -> BuyerConversationSendIntent:
        if self.state not in {BuyerConversationSendState.SENDING, BuyerConversationSendState.UNKNOWN}:
            raise BuyerConversationSendConflictError("only an in-flight or unknown send intent can become failed")
        return replace(
            self,
            state=BuyerConversationSendState.FAILED,
            remote_message_id=None,
            remote_receipt=remote_receipt or self.remote_receipt,
            failure_reason=reason,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.key.to_payload(),
            "intent_id": self.intent_id,
            "draft_id": self.draft_id,
            "sender_account_registration_id": self.sender_account_registration_id,
            "body": self.body,
            "context": self.context.to_payload(),
            "draft_hash": self.draft_hash,
            "requested_by": self.requested_by,
            "command_id": self.command_id,
            "requested_at": self.requested_at,
            "state": self.state.value,
            "send_attempts": self.send_attempts,
            "remote_message_id": self.remote_message_id,
            "remote_receipt": self.remote_receipt,
            "failure_reason": self.failure_reason,
            "idempotency_key": self.idempotency_key,
        }


@runtime_checkable
class BuyerConversationMessageSender(Protocol):
    """The only mutation method accepted by the conversation-send controller."""

    def send_conversation_message(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> Awaitable[BuyerConversationRemoteSendReceipt | Mapping[str, Any]] | BuyerConversationRemoteSendReceipt | Mapping[str, Any]:
        """Send exactly one message through an account-pinned remote session."""


class BuyerConversationSendCapabilities:
    """A short-lived private-client wrapper that exposes one explicit mutation."""

    def __init__(
        self,
        client: BuyerConversationMessageSender | object,
        provenance: BuyerConversationSendProvenance,
        *,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(provenance, BuyerConversationSendProvenance):
            raise TypeError("provenance must be BuyerConversationSendProvenance")
        self._client = client
        self.provenance = provenance
        self._release = release
        self._closed = False

    async def send(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> BuyerConversationRemoteSendReceipt:
        method = getattr(self._client, "send_conversation_message", None)
        if not callable(method):
            raise BuyerConversationSendCapabilityError("conversation sender does not expose send_conversation_message")
        value = method(
            remote_dialog_id=_required_text(remote_dialog_id, "remote_dialog_id"),
            body=_required_text(body, "body"),
            idempotency_key=_required_text(idempotency_key, "idempotency_key"),
        )
        value = await value if inspect.isawaitable(value) else value
        return _coerce_remote_receipt(value)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._client, "close", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value
        finally:
            if self._release is not None:
                await self._release()


BuyerConversationSendFactory = Callable[
    [str],
    BuyerConversationSendCapabilities | Awaitable[BuyerConversationSendCapabilities],
]


@runtime_checkable
class BuyerConversationSendStore(Protocol):
    """Durable operations required by the explicit conversation delivery slice."""

    async def get_draft(self, *, key: BuyerConversationKey, draft_id: str) -> BuyerConversationDraft | None:
        """Return a scoped immutable local draft."""

    async def get_send_intent(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
    ) -> BuyerConversationSendIntent | None:
        """Return one delivery request under its complete account-bound dialog key."""

    async def get_send_intent_by_draft(
        self,
        *,
        key: BuyerConversationKey,
        draft_id: str,
    ) -> BuyerConversationSendIntent | None:
        """Return the one allowed delivery intent for a local draft."""

    async def create_send_intent(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        """Persist a pending intent before a remote request begins."""

    async def update_send_intent(self, intent: BuyerConversationSendIntent) -> BuyerConversationSendIntent:
        """Persist a valid monotonic delivery transition."""

    async def append_send_audit_event(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Append immutable operator and remote-outcome evidence."""

    async def record_send_outcome(
        self,
        *,
        intent: BuyerConversationSendIntent,
        event_type: str,
        payload: Mapping[str, Any],
        outgoing_message: BuyerConversationMessage | None = None,
    ) -> BuyerConversationSendIntent:
        """Atomically record an outcome and any remotely identified outgoing message."""

    async def list_send_audit_events(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        """List immutable evidence for a scoped conversation send intent."""


class BuyerConversationSendService:
    """Own durable conversation-send intent transitions without owning a remote client.

    The controller supplies the short-lived account-pinned capability.  Keeping
    durable work here prevents a route or a capability factory from silently
    becoming an alternate send path.
    """

    def __init__(self, conversation_store: BuyerConversationSendStore) -> None:
        if not isinstance(conversation_store, BuyerConversationSendStore):
            raise TypeError("conversation_store must implement BuyerConversationSendStore")
        self._conversation_store = conversation_store

    async def prepare_draft_intent(
        self,
        *,
        key: BuyerConversationKey,
        draft_id: str,
        requested_by: str,
        command_id: str,
    ) -> tuple[BuyerConversationSendIntent, bool]:
        """Create the sole delivery intent for a saved draft before dispatch."""

        _require_key(key)
        draft = await self._conversation_store.get_draft(key=key, draft_id=_required_text(draft_id, "draft_id"))
        if draft is None:
            raise BuyerConversationSendNotFoundError("account-bound Buyer conversation draft was not found")
        if draft.key != key or draft.sender_account_registration_id != key.account_registration_id:
            raise BuyerConversationSendConflictError("draft is outside the requested account-bound conversation")
        existing = await self._conversation_store.get_send_intent_by_draft(key=key, draft_id=draft.draft_id)
        if existing is not None:
            _ensure_intent_matches_draft(existing, draft)
            return existing, True

        intent = BuyerConversationSendIntent.from_draft(
            draft=draft,
            requested_by=_required_text(requested_by, "requested_by"),
            command_id=_required_text(command_id, "command_id"),
        )
        persisted = await self._conversation_store.create_send_intent(intent)
        if persisted.intent_id != intent.intent_id:
            _ensure_intent_matches_draft(persisted, draft)
            return persisted, True
        await self._conversation_store.append_send_audit_event(
            key=key,
            intent_id=persisted.intent_id,
            event_type="conversation.send.requested",
            payload={
                "requested_by": persisted.requested_by,
                "command_id": persisted.command_id,
                "draft_id": persisted.draft_id,
                "draft_hash": persisted.draft_hash,
                "idempotency_key": persisted.idempotency_key,
            },
        )
        return persisted, False

    async def get_intent(self, *, key: BuyerConversationKey, intent_id: str) -> BuyerConversationSendIntent:
        """Load a delivery intent without beginning another remote attempt."""

        _require_key(key)
        intent = await self._conversation_store.get_send_intent(
            key=key,
            intent_id=_required_text(intent_id, "intent_id"),
        )
        if intent is None:
            raise BuyerConversationSendNotFoundError("account-bound Buyer conversation send intent was not found")
        return intent

    async def list_audit(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        limit: int = 200,
    ) -> tuple[dict[str, Any], ...]:
        """Return audit evidence without changing an intent or remote state."""

        await self.get_intent(key=key, intent_id=intent_id)
        return await self._conversation_store.list_send_audit_events(
            key=key,
            intent_id=_required_text(intent_id, "intent_id"),
            limit=limit,
        )

    async def begin_delivery(
        self,
        intent: BuyerConversationSendIntent,
        *,
        provenance: BuyerConversationSendProvenance,
    ) -> BuyerConversationSendIntent:
        """Record the one outbound attempt before its remote call starts."""

        if not isinstance(intent, BuyerConversationSendIntent):
            raise TypeError("intent must be a BuyerConversationSendIntent")
        if not isinstance(provenance, BuyerConversationSendProvenance):
            raise TypeError("provenance must be a BuyerConversationSendProvenance")
        sending = intent.begin_send()
        persisted = await self._conversation_store.update_send_intent(sending)
        await self._conversation_store.append_send_audit_event(
            key=persisted.key,
            intent_id=persisted.intent_id,
            event_type="conversation.send.dispatch_started",
            payload={
                "send_attempt": persisted.send_attempts,
                "provenance": provenance.to_payload(),
            },
        )
        return persisted

    async def record_remote_receipt(
        self,
        sending: BuyerConversationSendIntent,
        receipt: BuyerConversationRemoteSendReceipt,
    ) -> BuyerConversationSendIntent:
        """Persist a receipt, leaving incomplete remote evidence explicitly unknown."""

        if not isinstance(sending, BuyerConversationSendIntent):
            raise TypeError("sending must be a BuyerConversationSendIntent")
        if not isinstance(receipt, BuyerConversationRemoteSendReceipt):
            raise TypeError("receipt must be a BuyerConversationRemoteSendReceipt")
        if receipt.accepted is True and receipt.remote_message_id is not None:
            outcome = sending.mark_sent(receipt)
            message = _outgoing_message_from_intent(outcome, receipt)
            event_type = "conversation.send.sent"
        elif receipt.accepted is False or receipt.delivery_state is BuyerConversationDeliveryState.FAILED:
            outcome = sending.mark_failed("remote_rejected", remote_receipt=receipt.remote_receipt)
            message = None
            event_type = "conversation.send.rejected"
        else:
            outcome = sending.mark_unknown(
                remote_receipt=receipt.remote_receipt,
                reason="remote_receipt_unverified",
            )
            message = None
            event_type = "conversation.send.unknown"
        return await self._conversation_store.record_send_outcome(
            intent=outcome,
            event_type=event_type,
            payload={"receipt": receipt.to_payload()},
            outgoing_message=message,
        )

    async def record_unknown_after_remote(
        self,
        sending: BuyerConversationSendIntent,
        *,
        reason: str,
    ) -> BuyerConversationSendIntent:
        """Persist the no-retry result after a remote request may have escaped."""

        unknown = sending.mark_unknown(reason=_required_text(reason, "reason"))
        return await self._conversation_store.record_send_outcome(
            intent=unknown,
            event_type="conversation.send.unknown",
            payload={"reason": reason},
        )

    async def reconcile(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        requested_by: str,
        command_id: str,
        receipt: BuyerConversationRemoteSendReceipt,
        failure_reason: str | None = None,
    ) -> BuyerConversationSendIntent:
        """Apply operator-supplied receipt evidence without triggering a send."""

        if not isinstance(receipt, BuyerConversationRemoteSendReceipt):
            raise TypeError("receipt must be a BuyerConversationRemoteSendReceipt")
        intent = await self.get_intent(key=key, intent_id=intent_id)
        if intent.state not in {BuyerConversationSendState.UNKNOWN, BuyerConversationSendState.SENDING}:
            raise BuyerConversationSendConflictError("only an unknown or in-flight conversation send intent can be reconciled")
        operator = _required_text(requested_by, "requested_by")
        command = _required_text(command_id, "command_id")
        if receipt.accepted is True and receipt.remote_message_id is not None:
            outcome = intent.mark_sent(receipt)
            message = _outgoing_message_from_intent(outcome, receipt)
            event_type = "conversation.send.reconciled_sent"
        elif receipt.accepted is False or receipt.delivery_state is BuyerConversationDeliveryState.FAILED:
            outcome = intent.mark_failed(
                _optional_text(failure_reason, "failure_reason") or "remote_rejected",
                remote_receipt=receipt.remote_receipt,
            )
            message = None
            event_type = "conversation.send.reconciled_failed"
        else:
            outcome = intent.mark_unknown(remote_receipt=receipt.remote_receipt, reason="remote_receipt_unverified")
            message = None
            event_type = "conversation.send.reconciled_unknown"
        return await self._conversation_store.record_send_outcome(
            intent=outcome,
            event_type=event_type,
            payload={
                "requested_by": operator,
                "command_id": command,
                "receipt": receipt.to_payload(),
                "failure_reason": outcome.failure_reason,
            },
            outgoing_message=message,
        )


class BuyerConversationSendController:
    """Perform one operator-commanded delivery with no background or global session."""

    def __init__(
        self,
        conversation_store: BuyerConversationSendStore,
        *,
        sender_factory: BuyerConversationSendFactory,
        enabled: bool = False,
    ) -> None:
        if not isinstance(conversation_store, BuyerConversationSendStore):
            raise TypeError("conversation_store must implement BuyerConversationSendStore")
        if not callable(sender_factory):
            raise TypeError("sender_factory must be callable")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        self._service = BuyerConversationSendService(conversation_store)
        self._sender_factory = sender_factory
        self._enabled = enabled

    async def send_draft(
        self,
        *,
        key: BuyerConversationKey,
        draft_id: str,
        requested_by: str,
        command_id: str,
        explicit_operator_command: bool,
    ) -> dict[str, Any]:
        """Deliver one immutable draft only after an explicit operator command."""

        self._require_enabled_command(explicit_operator_command)
        intent, idempotent = await self._service.prepare_draft_intent(
            key=key,
            draft_id=draft_id,
            requested_by=requested_by,
            command_id=command_id,
        )
        if idempotent:
            return _send_result(intent, idempotent=True)
        return await self._dispatch(intent)

    async def get_send_intent(self, *, key: BuyerConversationKey, intent_id: str) -> dict[str, Any]:
        """Return a persisted send audit record without re-dispatching it."""

        intent = await self._service.get_intent(key=key, intent_id=intent_id)
        return _send_result(intent, idempotent=False)

    async def list_send_audit(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        limit: int = 200,
    ) -> dict[str, list[dict[str, Any]]]:
        """List durable send evidence without dispatching or reconciling a message."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5_000:
            raise BuyerConversationSendError("limit must be between 1 and 5000")
        events = await self._service.list_audit(key=key, intent_id=intent_id, limit=limit)
        return {"items": list(events)}

    async def reconcile_send_intent(
        self,
        *,
        key: BuyerConversationKey,
        intent_id: str,
        requested_by: str,
        command_id: str,
        explicit_operator_command: bool,
        receipt: BuyerConversationRemoteSendReceipt,
        failure_reason: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an unknown remote outcome only from explicit operator evidence."""

        self._require_enabled_command(explicit_operator_command)
        persisted = await self._service.reconcile(
            key=key,
            intent_id=intent_id,
            requested_by=requested_by,
            command_id=command_id,
            receipt=receipt,
            failure_reason=failure_reason,
        )
        return _send_result(persisted, idempotent=False)

    async def _dispatch(self, intent: BuyerConversationSendIntent) -> dict[str, Any]:
        capabilities: BuyerConversationSendCapabilities | None = None
        sending: BuyerConversationSendIntent | None = None
        remote_call_started = False
        try:
            capabilities = await _await_sender(self._sender_factory, intent.key.account_registration_id)
            _validate_sender_scope(capabilities, intent.key.account_registration_id)
            sending = await self._service.begin_delivery(intent, provenance=capabilities.provenance)
            remote_call_started = True
            receipt = await capabilities.send(
                remote_dialog_id=sending.key.remote_dialog_id,
                body=sending.body,
                idempotency_key=sending.idempotency_key,
            )
            persisted = await self._service.record_remote_receipt(sending, receipt)
            return _send_result(persisted, idempotent=False)
        except BuyerConversationSendError:
            if remote_call_started and sending is not None:
                return await self._record_unknown_after_remote(
                    sending,
                    reason="remote_response_unverified",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures must become durable unknown outcomes.
            if not remote_call_started or sending is None:
                raise BuyerConversationSendCapabilityError("account-bound conversation sender could not start delivery") from exc
            return await self._record_unknown_after_remote(sending, reason="remote_transport_outcome_unknown")
        finally:
            if capabilities is not None:
                await capabilities.close()

    async def _record_unknown_after_remote(
        self,
        sending: BuyerConversationSendIntent,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Persist the no-retry outcome after a remote request may have escaped."""

        try:
            persisted = await self._service.record_unknown_after_remote(sending, reason=reason)
        except Exception as exc:  # noqa: BLE001 - do not report a false local terminal result.
            raise BuyerConversationSendError("remote delivery outcome is unknown and could not be persisted") from exc
        return _send_result(persisted, idempotent=False)

    def _require_enabled_command(self, explicit_operator_command: bool) -> None:
        if not self._enabled:
            raise BuyerConversationSendDisabledError("Buyer conversation send is disabled by feature flag")
        if explicit_operator_command is not True:
            raise BuyerConversationSendError("explicit operator confirmation is required before sending a conversation draft")


def _outgoing_message_from_intent(
    intent: BuyerConversationSendIntent,
    receipt: BuyerConversationRemoteSendReceipt,
) -> BuyerConversationMessage:
    if receipt.remote_message_id is None:
        raise BuyerConversationSendError("outgoing message persistence requires remote_message_id")
    delivery_state = receipt.delivery_state
    if delivery_state is BuyerConversationDeliveryState.UNKNOWN:
        delivery_state = BuyerConversationDeliveryState.SENT
    return BuyerConversationMessage(
        key=intent.key,
        remote_message_id=receipt.remote_message_id,
        direction=BuyerConversationMessageDirection.OUTGOING,
        body=intent.body,
        sender_account_registration_id=intent.sender_account_registration_id,
        remote_created_at=receipt.observed_at or _utc_now(),
        observed_at=receipt.observed_at or _utc_now(),
        delivery_state=delivery_state,
        context=intent.context,
    )


async def _await_sender(
    factory: BuyerConversationSendFactory,
    account_registration_id: str,
) -> BuyerConversationSendCapabilities:
    value = factory(account_registration_id)
    value = await value if inspect.isawaitable(value) else value
    if not isinstance(value, BuyerConversationSendCapabilities):
        raise BuyerConversationSendCapabilityError("sender_factory must return BuyerConversationSendCapabilities")
    return value


def _validate_sender_scope(capabilities: BuyerConversationSendCapabilities, account_registration_id: str) -> None:
    if capabilities.provenance.account_registration_id != account_registration_id:
        raise BuyerConversationSendCapabilityError(
            "account-bound conversation sender does not match account_registration_id"
        )
    if capabilities.provenance.source != "conversation_send":
        raise BuyerConversationSendCapabilityError("conversation sender provenance must use source='conversation_send'")


def _coerce_remote_receipt(value: Any) -> BuyerConversationRemoteSendReceipt:
    if isinstance(value, BuyerConversationRemoteSendReceipt):
        return value
    if not isinstance(value, Mapping):
        raise BuyerConversationSendCapabilityError("conversation sender returned an invalid receipt")
    accepted = value.get("accepted")
    if accepted is None:
        for key in ("ok", "success"):
            if key in value:
                accepted = value[key]
                break
    remote_message_id = _mapping_optional_text(value, "remote_message_id", "message_id", "id")
    if accepted is None and remote_message_id is not None:
        accepted = True
    if accepted is not None and not isinstance(accepted, bool):
        raise BuyerConversationSendCapabilityError("conversation sender receipt accepted must be a boolean")
    delivery_state = value.get("delivery_state") or value.get("state") or BuyerConversationDeliveryState.SENT
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise BuyerConversationSendCapabilityError("conversation sender receipt metadata must be an object")
    return BuyerConversationRemoteSendReceipt(
        accepted=accepted,
        remote_message_id=remote_message_id,
        remote_receipt=_mapping_optional_text(value, "remote_receipt", "receipt", "request_id"),
        delivery_state=delivery_state,
        observed_at=_mapping_optional_text(value, "observed_at", "created_at", "sent_at"),
        metadata=metadata,
    )


def _ensure_intent_matches_draft(intent: BuyerConversationSendIntent, draft: BuyerConversationDraft) -> None:
    if (
        intent.key != draft.key
        or intent.draft_id != draft.draft_id
        or intent.sender_account_registration_id != draft.sender_account_registration_id
        or intent.draft_hash != draft.draft_hash
        or intent.body != draft.body
        or intent.context != draft.context
    ):
        raise BuyerConversationSendConflictError("stored send intent does not match its immutable account-bound draft")


def _send_result(intent: BuyerConversationSendIntent, *, idempotent: bool) -> dict[str, Any]:
    return {
        "send_intent": intent.to_payload(),
        "idempotent": idempotent,
        "auto_send": False,
        "requires_reconciliation": intent.state in {BuyerConversationSendState.UNKNOWN, BuyerConversationSendState.SENDING},
    }


def _validate_state_fields(
    *,
    state: BuyerConversationSendState,
    send_attempts: int,
    remote_message_id: str | None,
    failure_reason: str | None,
) -> None:
    if state is BuyerConversationSendState.PENDING and send_attempts != 0:
        raise BuyerConversationSendError("pending send intent cannot have send attempts")
    if state is BuyerConversationSendState.SENDING and send_attempts < 1:
        raise BuyerConversationSendError("sending send intent requires an attempt")
    if state is BuyerConversationSendState.SENT:
        if send_attempts < 1 or remote_message_id is None:
            raise BuyerConversationSendError("sent send intent requires an attempt and remote_message_id")
    elif remote_message_id is not None:
        raise BuyerConversationSendError("only a sent send intent can store remote_message_id")
    if state is BuyerConversationSendState.FAILED:
        if send_attempts < 1 or failure_reason is None:
            raise BuyerConversationSendError("failed send intent requires an attempt and failure_reason")
    elif failure_reason is not None and state is not BuyerConversationSendState.UNKNOWN:
        raise BuyerConversationSendError("only unknown or failed send intents can store failure_reason")


def _intent_idempotency_key(key: BuyerConversationKey, draft_id: str, draft_hash: str) -> str:
    payload = "\x00".join(("buyer-conversation-send-v1", key.platform, key.account_registration_id, key.remote_dialog_id, draft_id, draft_hash))
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalize_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    try:
        serialized = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        normalized = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise BuyerConversationSendError(f"{name} must contain durable JSON values") from exc
    if not isinstance(normalized, dict):
        raise BuyerConversationSendError(f"{name} must be an object")
    return normalized


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _mapping_optional_text(mapping: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return _optional_text(mapping[name], name)
    return None


def _require_key(key: BuyerConversationKey) -> None:
    if not isinstance(key, BuyerConversationKey):
        raise TypeError("key must be a BuyerConversationKey")


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerConversationSendError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _required_hash(value: Any, name: str) -> str:
    normalized = _required_text(value, name)
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise BuyerConversationSendError(f"{name} must be a lowercase sha256 hex digest")
    return normalized


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "BuyerConversationMessageSender",
    "BuyerConversationRemoteSendReceipt",
    "BuyerConversationSendCapabilities",
    "BuyerConversationSendCapabilityError",
    "BuyerConversationSendConflictError",
    "BuyerConversationSendController",
    "BuyerConversationSendDisabledError",
    "BuyerConversationSendError",
    "BuyerConversationSendFactory",
    "BuyerConversationSendIntent",
    "BuyerConversationSendNotFoundError",
    "BuyerConversationSendProvenance",
    "BuyerConversationSendService",
    "BuyerConversationSendState",
    "BuyerConversationSendStore",
]
