"""Pure, explicit Buyer Search proposal and send-intent state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class BuyerProposalSendState(StrEnum):
    DRAFT = "draft"
    PREFLIGHT = "preflight"
    PENDING_SEND = "pending_send"
    SENDING = "sending"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"
    FAILED = "failed"


class BuyerProposalReconciliationOutcome(StrEnum):
    ACCEPTED = "accepted"
    NOT_SENT = "not_sent"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class BuyerProposalTransitionError(ValueError):
    """Raised when an outreach state transition violates the send contract."""


class BuyerProposalConfirmationRequired(BuyerProposalTransitionError):
    """Raised when a caller tries to enter ``sending`` without confirmation."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BuyerProposalDraft:
    """An immutable version of a proposal and the context used to produce it."""

    draft_id: str
    run_id: str
    project_id: str
    body: str
    price: int | float | None
    delivery_days: int | None
    context: Mapping[str, Any]
    model_alias: str
    platform: str = "kwork"
    version: int = 1
    context_json: str = field(init=False, repr=False)
    context_hash: str = field(init=False)
    draft_hash: str = field(init=False)

    def __post_init__(self) -> None:
        draft_id = _required_text(self.draft_id, "draft_id")
        run_id = _required_text(self.run_id, "run_id")
        project_id = _required_text(self.project_id, "project_id")
        body = _required_text(self.body, "body")
        model_alias = _required_text(self.model_alias, "model_alias")
        platform = _required_text(self.platform, "platform")
        if not isinstance(self.context, Mapping):
            raise TypeError("context must be a mapping")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("version must be a positive integer")
        _validate_price(self.price)
        _validate_delivery_days(self.delivery_days)

        normalized_context = _normalize_json_value(self.context)
        context_json = _canonical_json(normalized_context)
        context_hash = _hash_text(context_json)
        draft_json = _canonical_json(
            {
                "context_hash": context_hash,
                "body": body,
                "price": self.price,
                "delivery_days": self.delivery_days,
                "model_alias": model_alias,
                "platform": platform,
                "version": self.version,
            }
        )
        object.__setattr__(self, "draft_id", draft_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "model_alias", model_alias)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "context", _freeze_json_value(normalized_context))
        object.__setattr__(self, "context_json", context_json)
        object.__setattr__(self, "context_hash", context_hash)
        object.__setattr__(self, "draft_hash", _hash_text(draft_json))

    def to_payload(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "body": self.body,
            "price": self.price,
            "delivery_days": self.delivery_days,
            "context": json.loads(self.context_json),
            "context_hash": self.context_hash,
            "draft_hash": self.draft_hash,
            "model_alias": self.model_alias,
            "platform": self.platform,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class BuyerProposalPreflight:
    """The explicit result of validation before an operator can request send."""

    passed: bool
    checks: Mapping[str, bool] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a boolean")
        if not isinstance(self.checks, Mapping):
            raise TypeError("checks must be a mapping")
        checks: dict[str, bool] = {}
        for key, value in self.checks.items():
            normalized_key = _required_text(str(key), "check key")
            if not isinstance(value, bool):
                raise TypeError("check values must be booleans")
            checks[normalized_key] = value
        failures = tuple(_required_text(value, "failure") for value in self.failures)
        if self.passed and failures:
            raise ValueError("a passed preflight cannot have failures")
        if not self.passed and not failures:
            failures = ("preflight_failed",)
        object.__setattr__(self, "checks", MappingProxyType(checks))
        object.__setattr__(self, "failures", failures)

    def to_payload(self) -> dict[str, Any]:
        return {"passed": self.passed, "checks": dict(self.checks), "failures": list(self.failures)}


@dataclass(frozen=True, slots=True)
class BuyerProposalSendIntent:
    """A durable send intent, never an instruction to auto-send remotely."""

    intent_id: str
    draft_id: str
    project_id: str
    platform: str
    sender_account_registration_id: str
    idempotency_key: str
    context_hash: str
    draft_hash: str
    state: BuyerProposalSendState = BuyerProposalSendState.DRAFT
    preflight: BuyerProposalPreflight | None = None
    send_attempts: int = 0
    remote_receipt: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "draft_id", "project_id", "platform", "idempotency_key"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "sender_account_registration_id",
            validate_sender_account_registration_id(self.sender_account_registration_id),
        )
        for name in ("context_hash", "draft_hash"):
            value = _required_text(getattr(self, name), name)
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
            object.__setattr__(self, name, value)
        try:
            state = BuyerProposalSendState(self.state)
        except (TypeError, ValueError) as exc:
            raise ValueError("state must be a BuyerProposalSendState") from exc
        if isinstance(self.send_attempts, bool) or not isinstance(self.send_attempts, int) or self.send_attempts < 0:
            raise ValueError("send_attempts must be a non-negative integer")
        if self.preflight is not None and not isinstance(self.preflight, BuyerProposalPreflight):
            raise TypeError("preflight must be a BuyerProposalPreflight or None")
        if state in {
            BuyerProposalSendState.PENDING_SEND,
            BuyerProposalSendState.SENDING,
            BuyerProposalSendState.ACCEPTED,
            BuyerProposalSendState.UNKNOWN,
        }:
            if self.preflight is None or not self.preflight.passed:
                raise BuyerProposalTransitionError(f"{state.value} requires a passing preflight")
        if state in {BuyerProposalSendState.SENDING, BuyerProposalSendState.UNKNOWN, BuyerProposalSendState.ACCEPTED}:
            if self.send_attempts <= 0:
                raise BuyerProposalTransitionError(f"{state.value} requires at least one send attempt")
        if state is BuyerProposalSendState.ACCEPTED and not _optional_text(self.remote_receipt):
            raise BuyerProposalTransitionError("accepted requires a remote receipt")
        if state is BuyerProposalSendState.FAILED and not _optional_text(self.failure_reason):
            raise BuyerProposalTransitionError("failed requires a failure reason")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "remote_receipt", _optional_text(self.remote_receipt))
        object.__setattr__(self, "failure_reason", _optional_text(self.failure_reason))

    def to_payload(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "draft_id": self.draft_id,
            "project_id": self.project_id,
            "platform": self.platform,
            "sender_account_registration_id": self.sender_account_registration_id,
            "idempotency_key": self.idempotency_key,
            "context_hash": self.context_hash,
            "draft_hash": self.draft_hash,
            "state": self.state.value,
            "preflight": self.preflight.to_payload() if self.preflight is not None else None,
            "send_attempts": self.send_attempts,
            "remote_receipt": self.remote_receipt,
            "failure_reason": self.failure_reason,
        }


class BuyerProposalOutreach:
    """Pure transition functions for proposal send intents; it performs no I/O."""

    @staticmethod
    def create_intent(
        draft: BuyerProposalDraft,
        *,
        sender_account_registration_id: str,
    ) -> BuyerProposalSendIntent:
        if not isinstance(draft, BuyerProposalDraft):
            raise TypeError("draft must be a BuyerProposalDraft")
        sender = validate_sender_account_registration_id(sender_account_registration_id)
        idempotency_key = proposal_idempotency_key(
            platform=draft.platform,
            project_id=draft.project_id,
            sender_account_registration_id=sender,
            draft_hash=draft.draft_hash,
        )
        intent_id = f"buyer-send-{uuid5(NAMESPACE_URL, f'{draft.draft_id}:{sender}:{idempotency_key}').hex}"
        return BuyerProposalSendIntent(
            intent_id=intent_id,
            draft_id=draft.draft_id,
            project_id=draft.project_id,
            platform=draft.platform,
            sender_account_registration_id=sender,
            idempotency_key=idempotency_key,
            context_hash=draft.context_hash,
            draft_hash=draft.draft_hash,
        )

    @staticmethod
    def start_preflight(intent: BuyerProposalSendIntent) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.DRAFT)
        return replace(intent, state=BuyerProposalSendState.PREFLIGHT)

    @staticmethod
    def finish_preflight(
        intent: BuyerProposalSendIntent,
        result: BuyerProposalPreflight,
    ) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.PREFLIGHT)
        if not isinstance(result, BuyerProposalPreflight):
            raise TypeError("result must be a BuyerProposalPreflight")
        if result.passed:
            return replace(intent, state=BuyerProposalSendState.PENDING_SEND, preflight=result)
        return replace(
            intent,
            state=BuyerProposalSendState.FAILED,
            preflight=result,
            failure_reason="; ".join(result.failures),
        )

    @staticmethod
    def begin_send(
        intent: BuyerProposalSendIntent,
        *,
        explicit_confirmation: bool = False,
    ) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.PENDING_SEND)
        if explicit_confirmation is not True:
            raise BuyerProposalConfirmationRequired("explicit confirmation is required before sending a proposal")
        return replace(
            intent,
            state=BuyerProposalSendState.SENDING,
            send_attempts=intent.send_attempts + 1,
            failure_reason=None,
        )

    @staticmethod
    def mark_accepted(intent: BuyerProposalSendIntent, *, remote_receipt: str) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.SENDING)
        return replace(
            intent,
            state=BuyerProposalSendState.ACCEPTED,
            remote_receipt=_required_text(remote_receipt, "remote_receipt"),
            failure_reason=None,
        )

    @staticmethod
    def mark_unknown(intent: BuyerProposalSendIntent, *, reason: str) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.SENDING)
        return replace(
            intent,
            state=BuyerProposalSendState.UNKNOWN,
            failure_reason=_required_text(reason, "reason"),
        )

    @staticmethod
    def mark_failed(intent: BuyerProposalSendIntent, *, reason: str) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.SENDING)
        return replace(
            intent,
            state=BuyerProposalSendState.FAILED,
            failure_reason=_required_text(reason, "reason"),
        )

    @staticmethod
    def reconcile(
        intent: BuyerProposalSendIntent,
        *,
        outcome: BuyerProposalReconciliationOutcome,
        remote_receipt: str | None = None,
        reason: str | None = None,
    ) -> BuyerProposalSendIntent:
        _require_state(intent, BuyerProposalSendState.UNKNOWN)
        try:
            resolved_outcome = BuyerProposalReconciliationOutcome(outcome)
        except (TypeError, ValueError) as exc:
            raise ValueError("outcome must be a BuyerProposalReconciliationOutcome") from exc
        if resolved_outcome is BuyerProposalReconciliationOutcome.ACCEPTED:
            return replace(
                intent,
                state=BuyerProposalSendState.ACCEPTED,
                remote_receipt=_required_text(remote_receipt or "", "remote_receipt"),
                failure_reason=None,
            )
        if resolved_outcome is BuyerProposalReconciliationOutcome.NOT_SENT:
            return replace(
                intent,
                state=BuyerProposalSendState.PENDING_SEND,
                remote_receipt=None,
                failure_reason=None,
            )
        if resolved_outcome is BuyerProposalReconciliationOutcome.FAILED:
            return replace(
                intent,
                state=BuyerProposalSendState.FAILED,
                failure_reason=_required_text(reason or "", "reason"),
            )
        return replace(intent, failure_reason=_required_text(reason or "manual review required", "reason"))


def validate_sender_account_registration_id(value: str) -> str:
    """Require an explicit account identity instead of a global Kwork session."""

    normalized = _required_text(value, "sender_account_registration_id")
    if any(character.isspace() for character in normalized):
        raise ValueError("sender_account_registration_id cannot contain whitespace")
    return normalized


def proposal_idempotency_key(
    *,
    platform: str,
    project_id: str,
    sender_account_registration_id: str,
    draft_hash: str,
) -> str:
    """Derive platform/project/sender/draft idempotency required for outreach."""

    normalized_platform = _required_text(platform, "platform")
    normalized_project_id = _required_text(project_id, "project_id")
    normalized_sender = validate_sender_account_registration_id(sender_account_registration_id)
    normalized_draft_hash = _required_text(draft_hash, "draft_hash")
    if not _SHA256_RE.fullmatch(normalized_draft_hash):
        raise ValueError("draft_hash must be a lowercase SHA-256 hex digest")
    return _hash_text(
        "\x00".join(("buyer-proposal-send-v1", normalized_platform, normalized_project_id, normalized_sender, normalized_draft_hash))
    )


def _require_state(intent: BuyerProposalSendIntent, expected: BuyerProposalSendState) -> None:
    if not isinstance(intent, BuyerProposalSendIntent):
        raise TypeError("intent must be a BuyerProposalSendIntent")
    if intent.state is not expected:
        raise BuyerProposalTransitionError(f"expected {expected.value}, got {intent.state.value}")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_text(value, "optional text")


def _validate_price(value: int | float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError("price must be a positive finite number or None")


def _validate_delivery_days(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("delivery_days must be a positive integer or None")


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("context keys must be strings")
            normalized[key] = _normalize_json_value(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("context numbers must be finite")
        return value
    raise TypeError(f"unsupported context value type: {type(value).__name__}")


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BuyerProposalConfirmationRequired",
    "BuyerProposalDraft",
    "BuyerProposalOutreach",
    "BuyerProposalPreflight",
    "BuyerProposalReconciliationOutcome",
    "BuyerProposalSendIntent",
    "BuyerProposalSendState",
    "BuyerProposalTransitionError",
    "proposal_idempotency_key",
    "validate_sender_account_registration_id",
]
