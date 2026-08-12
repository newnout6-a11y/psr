"""Account-bound runtime capability for one explicit Kwork proposal delivery.

The durable outreach controller owns confirmation and state transitions.  This
module owns the intentionally narrow operational bridge that is allowed to
perform the one remote ``createoffer`` mutation after that confirmation.  It
borrows the exact discovery worker identity already leased for the Buyer run,
constructs its client through the verified VPNTE route, and releases that
fence only when the gateway is closed.

There is deliberately no background retry here.  A transport ambiguity is
raised to the delivery controller so it becomes ``unknown`` and is later
resolved by this gateway's read-only detail inspection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from hashlib import sha256
import inspect
import math
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.platforms.kwork_supply.identity_pool import MarketAccountContext, MarketIdentityPool

from .outreach import BuyerProposalSendIntent, validate_sender_account_registration_id
from .outreach_service import BuyerRemoteOfferEvidence
from .proposal_delivery import BuyerProposalDeliveryRejectedError, BuyerProposalRemoteDeliveryReceipt
from .runtime_composition import AccountBoundBuyerKworkClientFactory
from .worker import BuyerDiscoveryIdentity


KWORK_CREATE_OFFER_URL = "https://kwork.ru/api/offer/createoffer"
"""The one web mutation endpoint exposed by this dedicated capability."""


class BuyerProposalDeliveryRuntimeError(RuntimeError):
    """Raised when a leased Buyer proposal delivery capability is unsafe or indeterminate."""


BuyerProposalProjectResolver = Callable[[str, str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class AccountBoundBuyerProposalDeliveryGateway:
    """One account/run/project-scoped mutation and reconciliation capability.

    Instances are created only by
    :class:`AccountBoundBuyerProposalDeliveryGatewayFactory`.  They retain the
    supervisor reservation until :meth:`close`, ensuring the discovery worker
    cannot simultaneously use the account/route pair for a read page.
    """

    def __init__(
        self,
        *,
        run_id: str,
        project_id: str,
        remote_project_id: str,
        account_registration_id: str,
        client: object,
        release: Callable[[], Awaitable[None]],
        offer_type: str,
        kwork_name: str,
        request_timeout_seconds: float,
    ) -> None:
        self.run_id = _required_text(run_id, "run_id")
        self.project_id = _required_text(project_id, "project_id")
        self.remote_project_id = _required_remote_project_id(remote_project_id)
        self.account_registration_id = validate_sender_account_registration_id(account_registration_id)
        self._client = client
        self._release = release
        self.offer_type = _required_text(offer_type, "offer_type")
        self.kwork_name = _required_text(kwork_name, "kwork_name")
        self.request_timeout_seconds = _positive_float(request_timeout_seconds, "request_timeout_seconds")
        self._context, self._proxy_url = _validate_delivery_client(client, self.account_registration_id)
        self._closed = False

    async def deliver_proposal(
        self,
        *,
        remote_project_id: str,
        proposal_body: str,
        price: int | float | None,
        delivery_days: int | None,
        currency: str,
        idempotency_key: str,
    ) -> BuyerProposalRemoteDeliveryReceipt:
        """Post one explicit offer through the retained VPNTE route.

        A definitive Kwork validation response becomes
        :class:`BuyerProposalDeliveryRejectedError`.  Network faults, redirects,
        429/5xx responses, malformed success payloads, and receipt-less 2xx
        results remain indeterminate so callers do not blindly retry a mutation.
        """

        self._require_open()
        requested_remote_id = _required_remote_project_id(remote_project_id)
        if requested_remote_id != self.remote_project_id:
            raise BuyerProposalDeliveryRejectedError(
                "remote project identifier does not match the account-bound delivery reservation"
            )
        body = _required_text(proposal_body, "proposal_body")
        numeric_price = _positive_price(price)
        days = _positive_int(delivery_days, "delivery_days")
        normalized_currency = _required_text(currency, "currency").upper()
        if normalized_currency != "RUB":
            raise BuyerProposalDeliveryRejectedError("Kwork createoffer currently supports only RUB proposal amounts")
        key = _required_text(idempotency_key, "idempotency_key")
        csrf_token = _csrf_token(self._context.cookies)
        if csrf_token is None:
            raise BuyerProposalDeliveryRejectedError(
                "account-bound Kwork session has no csrf_user_token for createoffer"
            )

        payload = {
            "wantId": self.remote_project_id,
            "offerType": self.offer_type,
            "description": body,
            "kwork_duration": str(days),
            "kwork_price": _price_text(numeric_price),
            "kwork_name": self.kwork_name,
            "csrftoken": csrf_token,
            # Kwork's web form uses a short opaque draft key.  This is stable
            # for the durable proposal identity and never substitutes for the
            # separate explicit confirmation/idempotency key.
            "draftKey": _draft_key(key),
        }
        headers = self._request_headers(idempotency_key=key, csrf_token=csrf_token)
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self._context.cookies),
                timeout=self.request_timeout_seconds,
                follow_redirects=False,
                proxy=self._proxy_url,
                trust_env=False,
            ) as http_client:
                response = await http_client.post(
                    KWORK_CREATE_OFFER_URL,
                    files={name: (None, value) for name, value in payload.items()},
                )
        except httpx.HTTPError as exc:
            raise BuyerProposalDeliveryRuntimeError("Kwork createoffer request did not receive a reliable response") from exc

        status_code = _response_status(response)
        if 300 <= status_code < 400:
            raise BuyerProposalDeliveryRuntimeError("Kwork createoffer returned an unexpected redirect")
        response_payload = _json_response(response)
        if _is_definitive_rejection(status_code, response_payload):
            raise BuyerProposalDeliveryRejectedError(
                _remote_error_message(response_payload, status_code),
                detail={"http_status": status_code, "remote_project_id": self.remote_project_id},
            )
        if status_code >= 400:
            raise BuyerProposalDeliveryRuntimeError(f"Kwork createoffer returned HTTP {status_code}")
        if not isinstance(response_payload, Mapping):
            raise BuyerProposalDeliveryRuntimeError("Kwork createoffer returned invalid JSON")
        if _response_indicates_rejection(response_payload):
            raise BuyerProposalDeliveryRejectedError(
                _remote_error_message(response_payload, status_code),
                detail={"http_status": status_code, "remote_project_id": self.remote_project_id},
            )
        receipt = _offer_receipt(response_payload)
        if receipt is None:
            raise BuyerProposalDeliveryRuntimeError("Kwork createoffer accepted no stable remote offer receipt")
        return BuyerProposalRemoteDeliveryReceipt(
            remote_receipt=receipt,
            source="kwork_offer_api",
            detail={
                "http_status": status_code,
                "remote_project_id": self.remote_project_id,
                "remote_offer_id": receipt,
                "offer_type": self.offer_type,
            },
        )

    async def inspect_send_intent(self, intent: BuyerProposalSendIntent) -> BuyerRemoteOfferEvidence:
        """Inspect the same remote project through the pinned client's read API.

        The reader never replays ``createoffer``.  It first uses the project
        detail endpoint and falls back to the want detail endpoint only when
        the first response does not expose an offer state.
        """

        self._require_open()
        if not isinstance(intent, BuyerProposalSendIntent):
            raise TypeError("intent must be BuyerProposalSendIntent")
        if intent.sender_account_registration_id != self.account_registration_id:
            raise BuyerProposalDeliveryRuntimeError("send intent is pinned to a different account")

        project_detail = await _call_client_read(self._client, "fetch_project_detail", self.remote_project_id)
        if not isinstance(project_detail, Mapping):
            raise BuyerProposalDeliveryRuntimeError("Kwork project detail returned an invalid payload")
        detail_payload = dict(project_detail)
        has_offer = _remote_has_offer(detail_payload)
        if has_offer is None:
            want_detail = await _call_client_read(self._client, "fetch_want_detail", self.remote_project_id)
            if not isinstance(want_detail, Mapping):
                raise BuyerProposalDeliveryRuntimeError("Kwork want detail returned an invalid payload")
            detail_payload = {**dict(want_detail), **detail_payload}
            has_offer = _remote_has_offer(detail_payload)
        receipt = _offer_receipt(detail_payload)
        if has_offer is None and receipt is not None:
            has_offer = True
        return BuyerRemoteOfferEvidence(
            source="account_bound_offer_detail",
            has_offer=has_offer,
            remote_receipt=receipt if has_offer is True else None,
            detail={
                "run_id": self.run_id,
                "project_id": self.project_id,
                "remote_project_id": self.remote_project_id,
                "remote_offer_id": receipt,
                "has_offer": has_offer,
            },
        )

    async def close(self) -> None:
        """Close the dedicated client and release the exact supervisor fence once."""

        if self._closed:
            return
        self._closed = True
        try:
            await _close_client(self._client)
        finally:
            await self._release()

    def _request_headers(self, *, idempotency_key: str, csrf_token: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "User-Agent": "PSR-BuyerProposalDelivery/1.0",
            "Origin": "https://kwork.ru",
            "Referer": f"https://kwork.ru/new_offer?project={self.remote_project_id}",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": idempotency_key,
        }
        headers.update({str(name): str(value) for name, value in self._context.headers.items() if str(name).strip()})
        # The durable idempotency header is not allowed to be overridden by
        # persona metadata supplied during account registration.
        headers["Idempotency-Key"] = idempotency_key
        headers["X-CSRF-Token"] = csrf_token
        return headers

    def _require_open(self) -> None:
        if self._closed:
            raise BuyerProposalDeliveryRuntimeError("account-bound proposal delivery gateway is already closed")


class AccountBoundBuyerProposalDeliveryGatewayFactory:
    """Create one delivery gateway from the exact run/account VPNTE lease.

    ``project_resolver`` must resolve the durable Buyer Search project using the
    supplied run/project IDs.  Passing ``buyer_search_service.get_project`` is
    the intended server composition: it gives reconciliation the canonical
    remote project ID without letting the delivery runtime query arbitrary
    projects or discover another account.
    """

    def __init__(
        self,
        supervisor: object,
        identity_pool: MarketIdentityPool,
        account_client_factory: AccountBoundBuyerKworkClientFactory,
        *,
        project_resolver: BuyerProposalProjectResolver,
        offer_type: str = "custom",
        kwork_name: str = "Custom offer",
        request_timeout_seconds: float = 15.0,
    ) -> None:
        for method_name in ("reserve_attachment_identity", "release_attachment_identity"):
            if not callable(getattr(supervisor, method_name, None)):
                raise TypeError(f"supervisor must expose {method_name}")
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        if not callable(project_resolver):
            raise TypeError("project_resolver must be callable")
        self.supervisor = supervisor
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory
        self.project_resolver = project_resolver
        self.offer_type = _required_text(offer_type, "offer_type")
        self.kwork_name = _required_text(kwork_name, "kwork_name")
        self.request_timeout_seconds = _positive_float(request_timeout_seconds, "request_timeout_seconds")

    async def __call__(
        self,
        run_id: str,
        project_id: str,
        account_registration_id: str,
    ) -> AccountBoundBuyerProposalDeliveryGateway:
        normalized_run_id = _required_text(run_id, "run_id")
        normalized_project_id = _required_text(project_id, "project_id")
        account_id = validate_sender_account_registration_id(account_registration_id)
        project = await _resolve_project(self.project_resolver, normalized_run_id, normalized_project_id)
        remote_project_id = _remote_project_id_from_project(project)
        reservation = await _reserve_identity(self.supervisor, normalized_run_id, account_id)
        client: object | None = None
        try:
            identity = _reservation_identity(reservation)
            if _reservation_run_id(reservation) != normalized_run_id:
                raise BuyerProposalDeliveryRuntimeError("supervisor reserved a worker from a different Buyer run")
            if identity.account_registration_id != account_id:
                raise BuyerProposalDeliveryRuntimeError("reserved Buyer worker does not match the pinned sender account")
            context = await self.identity_pool.context_for_worker(identity.worker_id)
            if not isinstance(context, MarketAccountContext) or context.registration_id != account_id:
                raise BuyerProposalDeliveryRuntimeError(
                    "reserved Buyer worker no longer owns the pinned account context"
                )
            client = await _await_value(self.account_client_factory(context, identity))

            async def release() -> None:
                await _release_identity(self.supervisor, reservation)

            return AccountBoundBuyerProposalDeliveryGateway(
                run_id=normalized_run_id,
                project_id=normalized_project_id,
                remote_project_id=remote_project_id,
                account_registration_id=account_id,
                client=client,
                release=release,
                offer_type=self.offer_type,
                kwork_name=self.kwork_name,
                request_timeout_seconds=self.request_timeout_seconds,
            )
        except Exception:
            try:
                if client is not None:
                    await _close_client(client)
            finally:
                await _release_identity(self.supervisor, reservation)
            raise


async def _resolve_project(
    resolver: BuyerProposalProjectResolver,
    run_id: str,
    project_id: str,
) -> Mapping[str, Any]:
    value = await _await_value(resolver(run_id, project_id))
    if not isinstance(value, Mapping):
        raise BuyerProposalDeliveryRuntimeError("project_resolver must return a Buyer project mapping")
    return value


async def _reserve_identity(supervisor: object, run_id: str, account_registration_id: str) -> object:
    reserve = getattr(supervisor, "reserve_attachment_identity", None)
    if not callable(reserve):
        raise BuyerProposalDeliveryRuntimeError("Buyer discovery supervisor cannot reserve a delivery identity")
    try:
        return await _await_value(reserve(run_id, account_registration_id))
    except BuyerProposalDeliveryRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the account-bound runtime boundary.
        raise BuyerProposalDeliveryRuntimeError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        ) from exc


async def _release_identity(supervisor: object, reservation: object) -> None:
    release = getattr(supervisor, "release_attachment_identity", None)
    if not callable(release):
        raise BuyerProposalDeliveryRuntimeError("Buyer discovery supervisor cannot release a delivery identity")
    await _await_value(release(reservation))


def _reservation_identity(reservation: object) -> BuyerDiscoveryIdentity:
    identity = getattr(reservation, "identity", None)
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise BuyerProposalDeliveryRuntimeError("supervisor returned an invalid Buyer delivery identity reservation")
    return identity


def _reservation_run_id(reservation: object) -> str:
    return _required_text(getattr(reservation, "run_id", None), "reservation.run_id")


def _validate_delivery_client(client: object, account_registration_id: str) -> tuple[MarketAccountContext, str]:
    context = getattr(client, "context", None)
    if not isinstance(context, MarketAccountContext):
        raise BuyerProposalDeliveryRuntimeError("account client must expose its MarketAccountContext")
    if context.registration_id != account_registration_id:
        raise BuyerProposalDeliveryRuntimeError("account-bound Kwork client does not match the pinned sender account")
    proxy_url = _required_text(getattr(client, "proxy_url", None), "account client proxy_url")
    parsed = urlsplit(proxy_url)
    if parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"} or not parsed.netloc:
        raise BuyerProposalDeliveryRuntimeError("account client proxy_url is not a valid VPNTE proxy URL")
    return context, proxy_url


async def _call_client_read(client: object, method_name: str, remote_project_id: str) -> Any:
    method = getattr(client, method_name, None)
    if not callable(method):
        raise BuyerProposalDeliveryRuntimeError(f"account-bound Kwork client lacks {method_name}")
    try:
        return await _await_value(method(remote_project_id))
    except BuyerProposalDeliveryRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - reconciliation must preserve an unknown result on read failure.
        raise BuyerProposalDeliveryRuntimeError(f"Kwork {method_name} failed during delivery reconciliation") from exc


async def _close_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        await _await_value(close())


def _response_status(response: object) -> int:
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise BuyerProposalDeliveryRuntimeError("Kwork createoffer response has an invalid HTTP status")
    return status


def _json_response(response: object) -> Mapping[str, Any] | None:
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        return None
    try:
        value = json_method()
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _is_definitive_rejection(status_code: int, payload: Mapping[str, Any] | None) -> bool:
    if status_code in {408, 409, 425, 429} or status_code >= 500:
        return False
    if 400 <= status_code < 500:
        return True
    return payload is not None and _response_indicates_rejection(payload)


def _response_indicates_rejection(payload: Mapping[str, Any]) -> bool:
    if payload.get("success") is False:
        return True
    status = str(payload.get("status") or "").strip().casefold()
    if status in {"error", "failed", "failure", "rejected"}:
        return True
    return bool(payload.get("error") or payload.get("errors"))


def _remote_error_message(payload: Mapping[str, Any] | None, status_code: int) -> str:
    if isinstance(payload, Mapping):
        for name in ("message", "error", "response", "errors", "detail"):
            value = payload.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
            if isinstance(value, Mapping):
                for nested in ("message", "error", "detail"):
                    text = value.get(nested)
                    if isinstance(text, str) and text.strip():
                        return text.strip()[:500]
    return f"Kwork createoffer rejected the proposal with HTTP {status_code}"


def _offer_receipt(payload: Mapping[str, Any]) -> str | None:
    """Extract an offer receipt without treating a project ID as an offer ID."""

    queue: list[tuple[Mapping[str, Any], bool]] = [(payload, False)]
    visited: set[int] = set()
    while queue:
        current, may_use_generic_id = queue.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        for name in ("remote_receipt", "remote_offer_id", "offer_id", "offerId"):
            value = _optional_text(current.get(name))
            if value is not None:
                return value
        if may_use_generic_id:
            value = _optional_text(current.get("id"))
            if value is not None:
                return value
        for name in ("offer", "data", "response", "result"):
            nested = current.get(name)
            if isinstance(nested, Mapping):
                queue.append((nested, name == "offer" or may_use_generic_id))
    return None


def _remote_has_offer(payload: Mapping[str, Any]) -> bool | None:
    queue: list[Mapping[str, Any]] = [payload]
    visited: set[int] = set()
    while queue:
        current = queue.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        for name in ("has_offer", "hasOffer", "offer_exists", "offerExists"):
            if name in current:
                value = _as_bool(current.get(name))
                if value is not None:
                    return value
        for name in ("offer", "data", "response", "project", "want"):
            nested = current.get(name)
            if isinstance(nested, Mapping):
                queue.append(nested)
    return None


def _remote_project_id_from_project(project: Mapping[str, Any]) -> str:
    for name in ("remote_project_id", "remote_id", "want_id", "wantId", "id"):
        value = _optional_text(project.get(name))
        if value is not None:
            return _required_remote_project_id(value)
    raise BuyerProposalDeliveryRuntimeError("Buyer project has no remote project identifier")


def _required_remote_project_id(value: object) -> str:
    normalized = _required_text(value, "remote_project_id")
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise BuyerProposalDeliveryRuntimeError("remote_project_id must be a positive decimal identifier")
    return str(int(normalized))


def _csrf_token(cookies: Mapping[str, str]) -> str | None:
    for name in ("csrf_user_token", "csrf_token", "XSRF-TOKEN"):
        value = _optional_text(cookies.get(name))
        if value is not None:
            return value
    return None


def _draft_key(idempotency_key: str) -> str:
    return sha256(idempotency_key.encode("utf-8")).hexdigest()[:8]


def _positive_price(value: int | float | None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        raise BuyerProposalDeliveryRejectedError("price must be a positive finite number for Kwork createoffer")
    return value


def _price_text(value: int | float) -> str:
    return str(int(value)) if float(value).is_integer() else format(float(value), ".2f").rstrip("0").rstrip(".")


def _positive_int(value: int | None, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BuyerProposalDeliveryRejectedError(f"{name} must be a positive integer for Kwork createoffer")
    return value


def _positive_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "active", "open"}:
            return True
        if normalized in {"false", "no", "0", "inactive", "closed"}:
            return False
    return None


async def _await_value(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerProposalDeliveryRuntimeError(f"{name} cannot be blank")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "AccountBoundBuyerProposalDeliveryGateway",
    "AccountBoundBuyerProposalDeliveryGatewayFactory",
    "BuyerProposalDeliveryRuntimeError",
    "BuyerProposalProjectResolver",
    "KWORK_CREATE_OFFER_URL",
]
