"""Account-bound runtime composition for an explicit Buyer inbox send.

The conversation-send controller owns idempotency and durable state.  This
module only borrows the exact account/route already leased by Buyer discovery,
builds its account-bound Kwork client through the standard factory, and hides
that client behind the single mutation capability accepted by the controller.
"""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any

from src.platforms.kwork_supply.identity_pool import MarketIdentityPool

from .conversation_send import (
    BuyerConversationRemoteSendReceipt,
    BuyerConversationSendCapabilities,
    BuyerConversationSendCapabilityError,
    BuyerConversationSendProvenance,
)
from .conversations import BuyerConversationDeliveryState
from .runtime_composition import AccountBoundBuyerKworkClientFactory
from .worker import BuyerDiscoveryIdentity


class AccountBoundBuyerConversationSendCapabilitiesFactory:
    """Borrow one active Buyer identity for one operator-confirmed send.

    Conversation delivery deliberately does not take a run ID.  The supervisor
    resolves an active lease for the requested account, so the account context,
    worker identity, VPNTE transport and egress IP all remain one indivisible
    provenance record.  The resulting capability owns both the private client
    and the reservation until its ``close`` method runs.
    """

    def __init__(
        self,
        supervisor: object,
        identity_pool: MarketIdentityPool,
        account_client_factory: AccountBoundBuyerKworkClientFactory,
    ) -> None:
        for method_name in ("reserve_account_identity", "release_attachment_identity"):
            if not callable(getattr(supervisor, method_name, None)):
                raise TypeError(f"supervisor must expose {method_name}")
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        self.supervisor = supervisor
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory

    async def __call__(self, account_registration_id: str) -> BuyerConversationSendCapabilities:
        """Return a short-lived sender pinned to ``account_registration_id``."""

        account_id = _required_text(account_registration_id, "account_registration_id")
        reservation = await _reserve_account_identity(self.supervisor, account_id)
        identity = _reservation_identity(reservation)
        client: object | None = None
        try:
            if identity.account_registration_id != account_id:
                raise BuyerConversationSendCapabilityError(
                    "reserved Buyer worker does not match the requested conversation sender account"
                )
            context = await self.identity_pool.context_for_worker(identity.worker_id)
            if context is None or context.registration_id != identity.account_registration_id:
                raise BuyerConversationSendCapabilityError(
                    "active Buyer worker no longer owns the requested account context"
                )
            client = await _await_account_client(self.account_client_factory, context, identity)
            if not callable(getattr(client, "send_conversation_message", None)):
                raise BuyerConversationSendCapabilityError(
                    "account-bound Kwork client does not expose explicit conversation send"
                )

            async def release() -> None:
                await _release_account_identity(self.supervisor, reservation)

            return BuyerConversationSendCapabilities(
                _AccountBoundConversationSender(client),
                BuyerConversationSendProvenance(
                    worker_id=identity.worker_id,
                    account_registration_id=identity.account_registration_id,
                    transport_id=identity.transport_id,
                    egress_ip=identity.egress_ip,
                    source="conversation_send",
                ),
                release=release,
            )
        except BuyerConversationSendCapabilityError:
            await _close_and_release(client, self.supervisor, reservation)
            raise
        except Exception as exc:  # noqa: BLE001 - keep unsafe composition behind the capability boundary.
            await _close_and_release(client, self.supervisor, reservation)
            raise BuyerConversationSendCapabilityError(
                "requested account could not create an account-bound conversation sender"
            ) from exc
        except BaseException:
            # Cancellation must not strand an account/route reservation.
            await _close_and_release(client, self.supervisor, reservation)
            raise


class _AccountBoundConversationSender:
    """Normalize one ``inboxCreate`` result without exposing its client."""

    def __init__(self, client: object) -> None:
        self._client = client

    async def send_conversation_message(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> BuyerConversationRemoteSendReceipt:
        remote_dialog = _required_text(remote_dialog_id, "remote_dialog_id")
        message_body = _required_text(body, "body")
        key = _required_text(idempotency_key, "idempotency_key")
        method = getattr(self._client, "send_conversation_message", None)
        if not callable(method):
            raise BuyerConversationSendCapabilityError(
                "account-bound Kwork client does not expose explicit conversation send"
            )
        value = method(
            remote_dialog_id=remote_dialog,
            body=message_body,
            idempotency_key=key,
        )
        value = await value if inspect.isawaitable(value) else value
        if not isinstance(value, Mapping):
            raise BuyerConversationSendCapabilityError("account-bound Kwork sender returned an invalid inboxCreate result")
        return _receipt_from_inbox_create(value, remote_dialog_id=remote_dialog, idempotency_key=key)

    async def close(self) -> None:
        await _close_client(self._client)


async def _reserve_account_identity(supervisor: object, account_registration_id: str) -> object:
    reserve = getattr(supervisor, "reserve_account_identity", None)
    if not callable(reserve):
        raise BuyerConversationSendCapabilityError(
            "Buyer discovery supervisor cannot reserve a conversation sender identity"
        )
    try:
        value = reserve(account_registration_id)
        return await value if inspect.isawaitable(value) else value
    except BuyerConversationSendCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001 - avoid a global account or route fallback.
        raise BuyerConversationSendCapabilityError(
            "requested account has no active Buyer worker with a leased VPNTE route"
        ) from exc


def _reservation_identity(reservation: object) -> BuyerDiscoveryIdentity:
    identity = getattr(reservation, "identity", None)
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise BuyerConversationSendCapabilityError(
            "Buyer discovery supervisor returned an invalid conversation sender identity reservation"
        )
    return identity


async def _await_account_client(
    factory: AccountBoundBuyerKworkClientFactory,
    context: object,
    identity: BuyerDiscoveryIdentity,
) -> object:
    value = factory(context, identity)  # type: ignore[arg-type]
    return await value if inspect.isawaitable(value) else value


async def _close_and_release(client: object | None, supervisor: object, reservation: object) -> None:
    try:
        if client is not None:
            await _close_client(client)
    finally:
        await _release_account_identity(supervisor, reservation)


async def _close_client(client: object) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    value = close()
    if inspect.isawaitable(value):
        await value


async def _release_account_identity(supervisor: object, reservation: object) -> None:
    release = getattr(supervisor, "release_attachment_identity", None)
    if not callable(release):
        return
    value = release(reservation)
    if inspect.isawaitable(value):
        await value


def _receipt_from_inbox_create(
    payload: Mapping[str, Any],
    *,
    remote_dialog_id: str,
    idempotency_key: str,
) -> BuyerConversationRemoteSendReceipt:
    remote_response = payload.get("remote_response")
    response = remote_response if isinstance(remote_response, Mapping) else payload
    accepted = _accepted_value(response)
    remote_message_id = _find_text(response, "remote_message_id", "message_id", "inbox_message_id", "MID", "id")
    if accepted is None and remote_message_id is not None:
        accepted = True
    remote_receipt = _find_text(response, "remote_receipt", "receipt", "request_id", "requestId", "trace_id", "traceId")
    observed_at = _find_text(response, "observed_at", "created_at", "sent_at", "time", "date")
    metadata = {
        "endpoint": str(payload.get("endpoint") or "inboxCreate"),
        "remote_dialog_id": remote_dialog_id,
        "idempotency_key": idempotency_key,
        "remote_response": _safe_metadata(response),
    }
    request_metadata = payload.get("request")
    if isinstance(request_metadata, Mapping):
        metadata["request"] = _safe_metadata(request_metadata)
    return BuyerConversationRemoteSendReceipt(
        accepted=accepted,
        remote_message_id=remote_message_id,
        remote_receipt=remote_receipt,
        delivery_state=_delivery_state(response),
        observed_at=observed_at,
        metadata=metadata,
    )


def _accepted_value(payload: Mapping[str, Any]) -> bool | None:
    for current in _payload_layers(payload):
        for name in ("accepted", "ok", "success"):
            value = current.get(name)
            if isinstance(value, bool):
                return value
        for name in ("error", "errors", "error_message"):
            value = current.get(name)
            if value not in (None, "", [], {}, False):
                return False
    return None


def _delivery_state(payload: Mapping[str, Any]) -> BuyerConversationDeliveryState:
    for current in _payload_layers(payload):
        value = current.get("delivery_state")
        if not isinstance(value, str):
            continue
        try:
            return BuyerConversationDeliveryState(value.strip().lower())
        except ValueError:
            continue
    return BuyerConversationDeliveryState.SENT


def _find_text(payload: Mapping[str, Any], *names: str) -> str | None:
    for current in _payload_layers(payload):
        for name in names:
            value = current.get(name)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _payload_layers(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    layers: list[Mapping[str, Any]] = [payload]
    for name in ("response", "data", "result", "message"):
        value = payload.get(name)
        if isinstance(value, Mapping):
            layers.append(value)
    return tuple(layers)


def _safe_metadata(value: object, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if any(token in name.casefold() for token in ("password", "secret", "token", "cookie", "authorization", "session")):
                result[name] = "[redacted]"
            else:
                result[name] = _safe_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:2_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerConversationSendCapabilityError(f"{name} cannot be blank")
    return value.strip()


__all__ = ["AccountBoundBuyerConversationSendCapabilitiesFactory"]
