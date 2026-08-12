"""Explicit, account-bound delivery of one durable Buyer proposal outbox item.

The outreach service owns the durable draft, preflight, confirmation, and
outbox state.  This module owns the narrow transition from that already
confirmed outbox item to one remote Kwork delivery request.  It deliberately
does not create a Kwork session, queue background work, or discover an
account: callers must inject an account-bound gateway factory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import inspect
from types import MappingProxyType
from typing import Any, Protocol

from .outreach import BuyerProposalSendIntent, BuyerProposalSendState, validate_sender_account_registration_id
from .outreach_service import (
    BuyerOutreachReconciliationGateway,
    BuyerOutreachService,
    BuyerRemoteOfferEvidence,
)


class BuyerProposalDeliveryError(ValueError):
    """Raised when an explicit Buyer proposal delivery cannot proceed."""


class BuyerProposalDeliveryFeatureDisabledError(BuyerProposalDeliveryError):
    """Raised before a remote gateway is acquired while delivery is disabled."""


class BuyerProposalDeliveryRejectedError(BuyerProposalDeliveryError):
    """A gateway may use this for a definitive, receipt-less remote rejection."""

    def __init__(self, reason: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(_required_text(reason, "reason"))
        if detail is not None and not isinstance(detail, Mapping):
            raise TypeError("detail must be a mapping or None")
        self.detail = dict(detail or {})


@dataclass(frozen=True, slots=True)
class BuyerProposalRemoteDeliveryReceipt:
    """Stable evidence returned only after the remote platform accepted an offer."""

    remote_receipt: str
    source: str = "kwork_offer_api"
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.detail, Mapping):
            raise TypeError("detail must be a mapping")
        detail = _json_mapping(self.detail, "detail")
        object.__setattr__(self, "remote_receipt", _required_text(self.remote_receipt, "remote_receipt"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "detail", MappingProxyType(detail))

    def to_payload(self) -> dict[str, Any]:
        return {
            "remote_receipt": self.remote_receipt,
            "source": self.source,
            "detail": dict(self.detail),
        }


class BuyerProposalDeliveryGateway(BuyerOutreachReconciliationGateway, Protocol):
    """One leased account's mutation and read-only reconciliation capability."""

    account_registration_id: str

    async def deliver_proposal(
        self,
        *,
        remote_project_id: str,
        proposal_body: str,
        price: int | float | None,
        delivery_days: int | None,
        currency: str,
        idempotency_key: str,
    ) -> BuyerProposalRemoteDeliveryReceipt | Mapping[str, Any]:
        """Perform exactly one remote offer creation and return its stable ID."""

    async def inspect_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerRemoteOfferEvidence | Mapping[str, Any]:
        """Read remote evidence for an already-unknown delivery outcome."""


BuyerAccountProposalDeliveryGatewayFactory = Callable[
    [str, str, str],
    BuyerProposalDeliveryGateway | Awaitable[BuyerProposalDeliveryGateway],
]


class BuyerAccountBoundProposalDeliveryController:
    """Run one operator-confirmed Buyer proposal delivery through a pinned account.

    There is no polling loop and no background execution path.  Calling
    :meth:`deliver` is the only operation that can invoke a gateway mutation;
    it requires the feature flag, a new delivery confirmation ID, and the
    sender account already pinned to the durable outbox item.
    """

    def __init__(
        self,
        outreach_service: BuyerOutreachService,
        gateway_factory: BuyerAccountProposalDeliveryGatewayFactory,
        *,
        delivery_enabled: bool = False,
    ) -> None:
        if not isinstance(outreach_service, BuyerOutreachService):
            raise TypeError("outreach_service must be BuyerOutreachService")
        if not callable(gateway_factory):
            raise TypeError("gateway_factory must be callable")
        if not isinstance(delivery_enabled, bool):
            raise TypeError("delivery_enabled must be a boolean")
        self.outreach_service = outreach_service
        self.gateway_factory = gateway_factory
        self.delivery_enabled = delivery_enabled

    async def deliver(
        self,
        *,
        intent_id: str,
        sender_account_registration_id: str,
        delivery_confirmation_id: str,
        confirmed_by: str,
        explicit_delivery_confirmation: bool,
    ) -> dict[str, Any]:
        """Deliver one pending proposal, or return the durable replay result.

        A remote timeout or malformed response becomes ``unknown`` instead of
        retrying automatically.  The caller must explicitly invoke
        :meth:`reconcile_unknown` before a new delivery confirmation can be
        used for a resend.
        """

        self._require_enabled()
        normalized_intent_id = _required_text(intent_id, "intent_id")
        account_id = validate_sender_account_registration_id(sender_account_registration_id)
        confirmation_id = _required_text(delivery_confirmation_id, "delivery_confirmation_id")
        operator = _required_text(confirmed_by, "confirmed_by")
        if explicit_delivery_confirmation is not True:
            raise BuyerProposalDeliveryError("explicit_delivery_confirmation must be true")

        intent = await self.outreach_service.get_send_intent_record(normalized_intent_id)
        self._assert_pinned_account(intent, account_id)
        previous_confirmation = await self.outreach_service.get_confirmation(confirmation_id)
        if previous_confirmation is not None:
            if previous_confirmation.intent_id != intent.intent_id:
                raise BuyerProposalDeliveryError("delivery confirmation ID is already bound to a different send intent")
            return self._idempotent_replay(intent, previous_confirmation.confirmation_id)

        draft = await self.outreach_service.get_draft_record(intent.draft_id)
        promotion = await self.outreach_service.get_promotion(
            platform=intent.platform,
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
        )
        remote_project_id = _remote_project_id_from_project(promotion.get("project"))
        gateway = await self._open_gateway(
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
            account_registration_id=account_id,
        )
        try:
            attempt = await self.outreach_service.begin_explicit_delivery(
                intent_id=intent.intent_id,
                sender_account_registration_id=account_id,
                explicit_confirmation=True,
                confirmation_id=confirmation_id,
                confirmed_by=operator,
            )
            try:
                raw_receipt = await gateway.deliver_proposal(
                    remote_project_id=remote_project_id,
                    proposal_body=attempt.draft.draft.body,
                    price=attempt.draft.draft.price,
                    delivery_days=attempt.draft.draft.delivery_days,
                    currency=attempt.draft.currency,
                    idempotency_key=attempt.intent.idempotency_key,
                )
                receipt = _coerce_receipt(raw_receipt)
            except BuyerProposalDeliveryRejectedError as exc:
                result = await self.outreach_service.mark_delivery_failed(
                    intent_id=attempt.intent.intent_id,
                    reason=str(exc),
                    evidence=BuyerRemoteOfferEvidence(
                        source="account_bound_delivery_gateway",
                        has_offer=False,
                        detail={
                            "delivery_confirmation_id": confirmation_id,
                            "rejection": dict(exc.detail),
                        },
                    ),
                )
                return _delivery_payload(
                    result,
                    confirmation_id=confirmation_id,
                    idempotent_replay=False,
                    remote_request_performed=True,
                    requires_reconciliation=False,
                )
            except Exception:  # noqa: BLE001 - an uncertain mutation must never be retried automatically.
                result = await self.outreach_service.mark_delivery_unknown(
                    intent_id=attempt.intent.intent_id,
                    reason="remote delivery outcome is indeterminate; reconcile before another delivery attempt",
                    evidence=BuyerRemoteOfferEvidence(
                        source="account_bound_delivery_gateway",
                        has_offer=None,
                        detail={"delivery_confirmation_id": confirmation_id},
                    ),
                )
                return _delivery_payload(
                    result,
                    confirmation_id=confirmation_id,
                    idempotent_replay=False,
                    remote_request_performed=True,
                    requires_reconciliation=True,
                )

            result = await self.outreach_service.mark_delivery_accepted(
                intent_id=attempt.intent.intent_id,
                remote_receipt=receipt.remote_receipt,
                evidence=BuyerRemoteOfferEvidence(
                    source=receipt.source,
                    has_offer=True,
                    remote_receipt=receipt.remote_receipt,
                    detail=receipt.detail,
                ),
            )
            return _delivery_payload(
                result,
                confirmation_id=confirmation_id,
                idempotent_replay=False,
                remote_request_performed=True,
                requires_reconciliation=False,
            )
        finally:
            await _close_gateway(gateway)

    async def reconcile_unknown(
        self,
        *,
        intent_id: str,
        sender_account_registration_id: str,
    ) -> dict[str, Any]:
        """Read remote evidence for one unknown delivery; never send again."""

        self._require_enabled()
        normalized_intent_id = _required_text(intent_id, "intent_id")
        account_id = validate_sender_account_registration_id(sender_account_registration_id)
        intent = await self.outreach_service.get_send_intent_record(normalized_intent_id)
        self._assert_pinned_account(intent, account_id)
        if intent.state is not BuyerProposalSendState.UNKNOWN:
            raise BuyerProposalDeliveryError("only an unknown send intent can be reconciled remotely")
        draft = await self.outreach_service.get_draft_record(intent.draft_id)
        gateway = await self._open_gateway(
            run_id=draft.draft.run_id,
            project_id=intent.project_id,
            account_registration_id=account_id,
        )
        try:
            result = await self.outreach_service.reconcile_from_gateway(intent_id=intent.intent_id, gateway=gateway)
        except Exception as exc:  # noqa: BLE001 - preserve the unknown state for a later explicit retry.
            raise BuyerProposalDeliveryError("remote delivery reconciliation failed") from exc
        finally:
            await _close_gateway(gateway)
        result["delivery"] = {
            "mode": "explicit_remote_reconciliation",
            "remote_request_performed": False,
            "requires_reconciliation": False,
        }
        result["auto_send"] = False
        return result

    def _require_enabled(self) -> None:
        if not self.delivery_enabled:
            raise BuyerProposalDeliveryFeatureDisabledError("Buyer proposal delivery feature is disabled")

    @staticmethod
    def _assert_pinned_account(intent: BuyerProposalSendIntent, account_registration_id: str) -> None:
        if intent.sender_account_registration_id != account_registration_id:
            raise BuyerProposalDeliveryError("sender account must match the account pinned to the send intent")

    async def _open_gateway(
        self,
        *,
        run_id: str,
        project_id: str,
        account_registration_id: str,
    ) -> BuyerProposalDeliveryGateway:
        value = self.gateway_factory(
            _required_text(run_id, "run_id"),
            _required_text(project_id, "project_id"),
            validate_sender_account_registration_id(account_registration_id),
        )
        if inspect.isawaitable(value):
            value = await value
        if not callable(getattr(value, "deliver_proposal", None)) or not callable(getattr(value, "inspect_send_intent", None)):
            raise BuyerProposalDeliveryError("gateway_factory must return a delivery and reconciliation gateway")
        if getattr(value, "account_registration_id", None) != account_registration_id:
            await _close_gateway(value)
            raise BuyerProposalDeliveryError("account-bound delivery gateway does not match the pinned sender account")
        return value

    @staticmethod
    def _idempotent_replay(intent: BuyerProposalSendIntent, confirmation_id: str) -> dict[str, Any]:
        if intent.state is BuyerProposalSendState.PENDING_SEND:
            raise BuyerProposalDeliveryError(
                "delivery confirmation is already bound to a pending outbox item; use a new confirmation ID"
            )
        return {
            "send_intent": intent.to_payload(),
            "evidence": None,
            "auto_send": False,
            "delivery": {
                "mode": "explicit_operator_request",
                "delivery_confirmation_id": confirmation_id,
                "idempotent_replay": True,
                "remote_request_performed": False,
                "requires_reconciliation": intent.state is BuyerProposalSendState.UNKNOWN,
            },
        }


def _delivery_payload(
    result: Mapping[str, Any],
    *,
    confirmation_id: str,
    idempotent_replay: bool,
    remote_request_performed: bool,
    requires_reconciliation: bool,
) -> dict[str, Any]:
    payload = dict(result)
    payload["auto_send"] = False
    payload["delivery"] = {
        "mode": "explicit_operator_request",
        "delivery_confirmation_id": confirmation_id,
        "idempotent_replay": idempotent_replay,
        "remote_request_performed": remote_request_performed,
        "requires_reconciliation": requires_reconciliation,
    }
    return payload


def _coerce_receipt(value: BuyerProposalRemoteDeliveryReceipt | Mapping[str, Any]) -> BuyerProposalRemoteDeliveryReceipt:
    if isinstance(value, BuyerProposalRemoteDeliveryReceipt):
        return value
    if not isinstance(value, Mapping):
        raise BuyerProposalDeliveryError("delivery gateway returned an invalid remote receipt")
    receipt = value.get("remote_receipt") or value.get("offer_id") or value.get("remote_offer_id")
    return BuyerProposalRemoteDeliveryReceipt(
        remote_receipt=_required_text(str(receipt) if receipt is not None else None, "remote_receipt"),
        source=str(value.get("source") or "kwork_offer_api"),
        detail=value.get("detail") if isinstance(value.get("detail"), Mapping) else {},
    )


def _remote_project_id_from_project(project: Any) -> str:
    if not isinstance(project, Mapping):
        raise BuyerProposalDeliveryError("promoted Buyer project has no remote project identifier")
    for name in ("remote_project_id", "remote_id", "want_id", "wantId", "id"):
        value = project.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise BuyerProposalDeliveryError("promoted Buyer project has no remote project identifier")


async def _close_gateway(gateway: object) -> None:
    close = getattr(gateway, "close", None)
    if not callable(close):
        return
    try:
        value = close()
        if inspect.isawaitable(value):
            await value
    except Exception:
        # Delivery state has already been persisted.  A teardown error must not
        # trigger another remote request or hide an accepted/unknown outcome.
        return


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerProposalDeliveryError(f"{name} cannot be blank")
    return value.strip()


def _json_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise BuyerProposalDeliveryError(f"{name} keys must be strings")
        normalized[key] = _json_value(item, name)
    return normalized


def _json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return _json_mapping(value, name)
    if isinstance(value, (list, tuple)):
        return [_json_value(item, name) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise BuyerProposalDeliveryError(f"unsupported {name} value type: {type(value).__name__}")


__all__ = [
    "BuyerAccountBoundProposalDeliveryController",
    "BuyerAccountProposalDeliveryGatewayFactory",
    "BuyerProposalDeliveryError",
    "BuyerProposalDeliveryFeatureDisabledError",
    "BuyerProposalDeliveryGateway",
    "BuyerProposalDeliveryRejectedError",
    "BuyerProposalRemoteDeliveryReceipt",
]
