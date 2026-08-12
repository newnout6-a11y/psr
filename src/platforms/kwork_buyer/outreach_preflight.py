"""Account-bound, read-only proposal preflight evidence collection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import inspect
from typing import Any

from .sources.capabilities import BuyerReadCapabilities


class BuyerAccountPreflightError(RuntimeError):
    """Raised when a server-side proposal preflight cannot retain account scope."""


BuyerAccountCapabilitiesFactory = Callable[
    [str, str, str],
    BuyerReadCapabilities | Awaitable[BuyerReadCapabilities],
]


class BuyerAccountBoundPreflightVerifier:
    """Read current project/account facts through the sender's leased identity.

    This collector intentionally has no create-offer or message capability.  It
    translates available remote fields into the narrow evidence consumed by
    the proposal preflight and leaves fields unknown when an endpoint does not
    expose them.  Unknown is safer than an optimistic browser assertion.
    """

    def __init__(self, capabilities_factory: BuyerAccountCapabilitiesFactory) -> None:
        if not callable(capabilities_factory):
            raise TypeError("capabilities_factory must be callable")
        self.capabilities_factory = capabilities_factory

    async def __call__(
        self,
        account_registration_id: str,
        run_id: str,
        project_id: str,
        project: Mapping[str, Any],
    ) -> dict[str, Any]:
        account_id = _required_text(account_registration_id, "account_registration_id")
        capabilities = await _capabilities(self.capabilities_factory, run_id, project_id, account_id)
        if capabilities.provenance.account_registration_id != account_id:
            await _close_capabilities(capabilities)
            raise BuyerAccountPreflightError("account-bound preflight reader does not match the pinned sender account")
        try:
            remote_id = _required_remote_project_id(project)
            project_detail = await capabilities.fetch_project_detail(remote_id)
            want_detail = await capabilities.fetch_want_detail(remote_id)
            if not isinstance(project_detail, Mapping) or not isinstance(want_detail, Mapping):
                raise BuyerAccountPreflightError("remote proposal preflight returned an invalid detail payload")
            merged = {**dict(want_detail), **dict(project_detail)}
            return {
                "project_is_active": _remote_active(merged),
                "has_offer": _remote_bool(merged, "has_offer", "hasOffer", "offer_exists", "offerExists"),
                "already_work": _remote_bool(merged, "already_work", "alreadyWork", "in_work", "inWork"),
                "sender_account_eligible": True,
                "account_session_valid": True,
                "connects_sufficient": _connects_sufficient(merged),
                "attachment_upload_capability_verified": False,
            }
        finally:
            await _close_capabilities(capabilities)


async def _capabilities(
    factory: BuyerAccountCapabilitiesFactory,
    run_id: str,
    project_id: str,
    account_registration_id: str,
) -> BuyerReadCapabilities:
    value = factory(
        _required_text(run_id, "run_id"),
        _required_text(project_id, "project_id"),
        _required_text(account_registration_id, "account_registration_id"),
    )
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, BuyerReadCapabilities):
        raise BuyerAccountPreflightError("capabilities_factory must return BuyerReadCapabilities")
    return value


async def _close_capabilities(capabilities: BuyerReadCapabilities) -> None:
    close = getattr(capabilities, "close", None)
    if not callable(close):
        return
    value = close()
    if inspect.isawaitable(value):
        await value


def _required_remote_project_id(project: Mapping[str, Any]) -> str:
    for name in ("remote_project_id", "remote_id", "want_id", "wantId", "id"):
        value = project.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise BuyerAccountPreflightError("Buyer project has no remote project identifier")


def _remote_active(payload: Mapping[str, Any]) -> bool | None:
    value = next((payload.get(name) for name in ("is_active", "active", "isActive") if name in payload), None)
    explicit = _as_bool(value)
    if explicit is not None:
        return explicit
    status = next((payload.get(name) for name in ("status", "want_status", "wantStatus") if name in payload), None)
    normalized = str(status or "").strip().casefold()
    if normalized in {"active", "open", "published", "available", "1"}:
        return True
    if normalized in {"closed", "archived", "cancelled", "completed", "inactive", "0"}:
        return False
    return None


def _remote_bool(payload: Mapping[str, Any], *names: str) -> bool | None:
    for name in names:
        if name in payload:
            return _as_bool(payload[name])
    return None


def _connects_sufficient(payload: Mapping[str, Any]) -> bool | None:
    direct = _remote_bool(payload, "connects_sufficient", "connectsSufficient")
    if direct is not None:
        return direct
    values: list[object] = [payload.get(name) for name in ("connects", "connects_required", "connectsRequired")]
    connects = payload.get("connects")
    if isinstance(connects, Mapping):
        values.extend(connects.get(name) for name in ("free_amount", "active_connects", "available", "balance"))
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            continue
    return None


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


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerAccountPreflightError(f"{name} cannot be blank")
    return value.strip()


__all__ = ["BuyerAccountBoundPreflightVerifier", "BuyerAccountPreflightError"]
