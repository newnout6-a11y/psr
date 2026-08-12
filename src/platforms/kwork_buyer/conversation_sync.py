"""Account-bound, read-only synchronization for Buyer Search conversations.

The controller turns one bounded inbox read into immutable Buyer conversation
models and commits that batch atomically.  It deliberately exposes no message
delivery capability: readers can list dialogs and their already-observed
messages only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
import inspect
from typing import Any, Protocol, runtime_checkable

from .conversation_persistence import SQLiteBuyerConversationStore
from .conversations import (
    BuyerConversation,
    BuyerConversationContext,
    BuyerConversationCursor,
    BuyerConversationKey,
    BuyerConversationMessage,
    BuyerConversationMessageDirection,
    BuyerConversationSyncBatch,
    advance_account_cursor,
    build_conversation_sync_batch,
    normalize_incoming_message,
    normalize_outgoing_message,
)
from .service import BuyerSearchSettings
from .sources.capabilities import BuyerReadProvenance


class BuyerConversationSyncError(ValueError):
    """Raised when an inbox read cannot be committed safely."""


class BuyerConversationSyncDisabledError(BuyerConversationSyncError):
    """Raised while the conversation-sync feature flag is disabled."""


class BuyerConversationSyncCapabilityError(BuyerConversationSyncError):
    """Raised when a reader is not pinned to the requested account."""


@runtime_checkable
class BuyerConversationInboxClient(Protocol):
    """The only remote methods permitted to a Buyer conversation sync."""

    def fetch_inbox_dialogs(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any] | Sequence[Mapping[str, Any]] | Awaitable[Mapping[str, Any] | Sequence[Mapping[str, Any]]]:
        """Read a bounded dialog page without mutating inbox state."""

    def fetch_inbox_messages(
        self,
        *,
        dialog: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any] | Sequence[Mapping[str, Any]] | Awaitable[Mapping[str, Any] | Sequence[Mapping[str, Any]]]:
        """Read already-observed messages for one dialog without sending."""


class BuyerConversationReadCapabilities:
    """Private-client wrapper exposing only account-bound inbox reads."""

    def __init__(
        self,
        client: BuyerConversationInboxClient | object,
        provenance: BuyerReadProvenance,
        *,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(provenance, BuyerReadProvenance):
            raise TypeError("provenance must be BuyerReadProvenance")
        self._client = client
        self.provenance = provenance
        self._release = release
        self._closed = False

    async def fetch_dialogs(self, *, cursor: str | None, limit: int) -> Mapping[str, Any] | Sequence[Mapping[str, Any]]:
        return await self._call("fetch_inbox_dialogs", cursor=cursor, limit=limit)

    async def fetch_messages(
        self,
        *,
        dialog: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any] | Sequence[Mapping[str, Any]]:
        return await self._call("fetch_inbox_messages", dialog=dialog, cursor=cursor, limit=limit)

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

    async def _call(self, method_name: str, **kwargs: Any) -> Mapping[str, Any] | Sequence[Mapping[str, Any]]:
        method = getattr(self._client, method_name, None)
        if not callable(method):
            raise BuyerConversationSyncCapabilityError(
                f"conversation reader does not expose read capability {method_name!r}"
            )
        value = method(**kwargs)
        value = await value if inspect.isawaitable(value) else value
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return _mapping_sequence(value, method_name)
        raise BuyerConversationSyncCapabilityError(f"conversation reader {method_name!r} returned an invalid payload")


BuyerConversationReadFactory = Callable[
    [str],
    BuyerConversationReadCapabilities | Awaitable[BuyerConversationReadCapabilities],
]


class BuyerConversationSyncController:
    """Read and atomically persist one account-scoped inbox snapshot."""

    def __init__(
        self,
        conversation_store: SQLiteBuyerConversationStore,
        *,
        reader_factory: BuyerConversationReadFactory,
        settings: BuyerSearchSettings,
    ) -> None:
        if not isinstance(conversation_store, SQLiteBuyerConversationStore):
            raise TypeError("conversation_store must be SQLiteBuyerConversationStore")
        if not callable(reader_factory):
            raise TypeError("reader_factory must be callable")
        if not isinstance(settings, BuyerSearchSettings):
            raise TypeError("settings must be BuyerSearchSettings")
        self._conversation_store = conversation_store
        self._reader_factory = reader_factory
        self._settings = settings

    async def sync_account(
        self,
        *,
        account_registration_id: str,
        dialog_limit: int = 100,
        message_limit: int = 500,
    ) -> dict[str, Any]:
        """Persist a bounded read-only inbox page under one sender account."""

        if not self._settings.conversation_sync:
            raise BuyerConversationSyncDisabledError("Buyer conversation sync is disabled by feature flag")
        account_id = _required_text(account_registration_id, "account_registration_id")
        dialog_limit = _bounded_limit(dialog_limit, "dialog_limit", maximum=200)
        message_limit = _bounded_limit(message_limit, "message_limit", maximum=1_000)
        reader: BuyerConversationReadCapabilities | None = None
        try:
            current = await self._conversation_store.get_cursor(
                platform="kwork",
                account_registration_id=account_id,
            )
            reader = await _await_reader(self._reader_factory, account_id)
            _validate_reader_scope(reader, account_id)
            dialogs_payload = await reader.fetch_dialogs(
                cursor=current.cursor if current is not None else None,
                limit=dialog_limit,
            )
            batch = await self._build_batch(
                account_registration_id=account_id,
                current_cursor=current,
                dialogs_payload=dialogs_payload,
                reader=reader,
                message_limit=message_limit,
            )
            persisted = await self._conversation_store.commit_sync_batch(batch)
            return {
                "batch": persisted.to_payload(),
                "cursor": persisted.cursor.to_payload(),
                "dialog_count": len(persisted.conversations),
                "message_count": len(persisted.messages),
                "provenance": persisted_provenance(reader.provenance),
                "auto_send": False,
            }
        except BuyerConversationSyncError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep transport and model failures behind the sync boundary.
            raise BuyerConversationSyncError(f"Buyer conversation sync failed: {exc}") from exc
        finally:
            if reader is not None:
                await reader.close()

    async def _build_batch(
        self,
        *,
        account_registration_id: str,
        current_cursor: BuyerConversationCursor | None,
        dialogs_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        reader: BuyerConversationReadCapabilities,
        message_limit: int,
    ) -> BuyerConversationSyncBatch:
        dialogs, next_cursor, watermark = _dialog_page(dialogs_payload)
        cursor = _next_cursor(
            account_registration_id=account_registration_id,
            current=current_cursor,
            cursor=next_cursor,
            watermark=watermark,
        )
        conversations: list[BuyerConversation] = []
        messages: list[BuyerConversationMessage] = []
        for raw_dialog in dialogs:
            key, conversation = _conversation_from_dialog(raw_dialog, account_registration_id=account_registration_id)
            conversations.append(conversation)
            raw_messages = await reader.fetch_messages(dialog=raw_dialog, cursor=None, limit=message_limit)
            for raw_message in _message_page(raw_messages):
                messages.append(
                    _message_from_payload(
                        raw_message,
                        key=key,
                        account_registration_id=account_registration_id,
                        context=conversation.context,
                    )
                )
        return build_conversation_sync_batch(
            cursor=cursor,
            conversations=conversations,
            messages=messages,
        )


async def _await_reader(factory: BuyerConversationReadFactory, account_registration_id: str) -> BuyerConversationReadCapabilities:
    value = factory(account_registration_id)
    value = await value if inspect.isawaitable(value) else value
    if not isinstance(value, BuyerConversationReadCapabilities):
        raise BuyerConversationSyncCapabilityError("reader_factory must return BuyerConversationReadCapabilities")
    return value


def _validate_reader_scope(reader: BuyerConversationReadCapabilities, account_registration_id: str) -> None:
    if reader.provenance.account_registration_id != account_registration_id:
        raise BuyerConversationSyncCapabilityError(
            "account-bound conversation reader does not match account_registration_id"
        )
    if reader.provenance.source != "conversation_sync":
        raise BuyerConversationSyncCapabilityError("conversation reader provenance must use source='conversation_sync'")


def _dialog_page(
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if isinstance(payload, Mapping):
        dialogs = _items(payload, "dialogs", "conversations", "items", "response")
        paging = payload.get("paging") if isinstance(payload.get("paging"), Mapping) else {}
        return (
            dialogs,
            _optional_text(payload.get("next_cursor") or payload.get("cursor") or paging.get("next_cursor")),
            _optional_text(payload.get("watermark") or paging.get("watermark")),
        )
    return _mapping_sequence(payload, "dialogs"), None, None


def _message_page(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        return _items(payload, "messages", "items", "response")
    return _mapping_sequence(payload, "messages")


def _items(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return _mapping_sequence(value, key)
    return []


def _mapping_sequence(value: Sequence[object], name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise BuyerConversationSyncCapabilityError(f"conversation reader {name!r} returned a non-object item")
        items.append(dict(item))
    return items


def _conversation_from_dialog(
    payload: Mapping[str, Any],
    *,
    account_registration_id: str,
) -> tuple[BuyerConversationKey, BuyerConversation]:
    remote_dialog_id = _first_text(payload, "remote_dialog_id", "dialog_id", "id", "user_id", "username")
    key = BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id=remote_dialog_id,
    )
    last_message = payload.get("last_message") or payload.get("lastMessage")
    last_message = last_message if isinstance(last_message, Mapping) else {}
    context = BuyerConversationContext(
        project_id=_optional_text(payload.get("buyer_search_project_id") or payload.get("project_id")),
        proposal_draft_id=_optional_text(payload.get("proposal_draft_id")),
        proposal_intent_id=_optional_text(payload.get("proposal_intent_id")),
        buyer_remote_user_id=_optional_text(payload.get("buyer_remote_user_id") or payload.get("user_id")),
        metadata=_dialog_context_metadata(payload),
    )
    return key, BuyerConversation(
        key=key,
        context=context,
        remote_title=_optional_text(payload.get("remote_title") or payload.get("title") or payload.get("project_name") or payload.get("username")),
        last_remote_message_at=_optional_text(
            payload.get("last_remote_message_at")
            or payload.get("last_message_at")
            or last_message.get("created_at")
            or last_message.get("time")
        ),
        synced_at=_utc_now(),
    )


def _message_from_payload(
    payload: Mapping[str, Any],
    *,
    key: BuyerConversationKey,
    account_registration_id: str,
    context: BuyerConversationContext,
) -> BuyerConversationMessage:
    direction = _message_direction(payload)
    if direction is BuyerConversationMessageDirection.OUTGOING:
        return normalize_outgoing_message(
            payload,
            key=key,
            sender_account_registration_id=account_registration_id,
            context=context,
        )
    return normalize_incoming_message(payload, key=key, context=context)


def _message_direction(payload: Mapping[str, Any]) -> BuyerConversationMessageDirection:
    value = str(payload.get("direction") or payload.get("sender") or "").strip().casefold()
    if value in {"outgoing", "out", "sent", "freelancer", "worker", "self"}:
        return BuyerConversationMessageDirection.OUTGOING
    if payload.get("is_outgoing") is True or payload.get("outgoing") is True:
        return BuyerConversationMessageDirection.OUTGOING
    return BuyerConversationMessageDirection.INCOMING


def _next_cursor(
    *,
    account_registration_id: str,
    current: BuyerConversationCursor | None,
    cursor: str | None,
    watermark: str | None,
) -> BuyerConversationCursor:
    if current is None:
        return BuyerConversationCursor(
            platform="kwork",
            account_registration_id=account_registration_id,
            cursor=cursor,
            watermark=watermark,
            updated_at=_utc_now(),
        )
    next_cursor = cursor if cursor is not None else current.cursor
    next_watermark = watermark if watermark is not None else current.watermark
    if next_cursor == current.cursor and next_watermark == current.watermark:
        return current
    return advance_account_cursor(
        current,
        cursor=next_cursor,
        watermark=next_watermark,
        updated_at=_utc_now(),
    )


def _dialog_context_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for source, destination in (
        ("username", "username"),
        ("buyer_search_run_id", "run_id"),
        ("unread_count", "unread_count"),
    ):
        value = payload.get(source)
        if value is not None:
            metadata[destination] = value
    return metadata


def persisted_provenance(provenance: BuyerReadProvenance) -> dict[str, str]:
    return provenance.as_dict()


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _optional_text(payload.get(key))
        if value is not None:
            return value
    raise BuyerConversationSyncError(f"conversation payload requires one of: {', '.join(keys)}")


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerConversationSyncError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bounded_limit(value: object, name: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise BuyerConversationSyncError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerConversationSyncError(f"{name} must be an integer") from exc
    if not 1 <= result <= maximum:
        raise BuyerConversationSyncError(f"{name} must be between 1 and {maximum}")
    return result


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BuyerConversationInboxClient",
    "BuyerConversationReadCapabilities",
    "BuyerConversationReadFactory",
    "BuyerConversationSyncCapabilityError",
    "BuyerConversationSyncController",
    "BuyerConversationSyncDisabledError",
    "BuyerConversationSyncError",
]
