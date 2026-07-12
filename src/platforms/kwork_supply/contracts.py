"""Transport-independent contracts and validation for market sources."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Protocol, runtime_checkable


class CursorKind(StrEnum):
    """How a source expresses a collection position."""

    PAGE = "page"
    EXCLUDE_IDS = "exclude_ids"
    OPAQUE = "opaque"


class ProtectionStatus(StrEnum):
    """Source response state relevant to collection safety."""

    OK = "ok"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    CHALLENGE = "challenge"
    HTTP_ERROR = "http_error"
    UNKNOWN = "unknown"

    @property
    def blocks_collection(self) -> bool:
        return self in {self.BLOCKED, self.RATE_LIMITED, self.CHALLENGE}


class ContractState(StrEnum):
    """Terminal outcome for one source batch attempt."""

    ACCEPTED = "accepted"
    CONTRACT_VIOLATION = "contract_violation"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


def _stable_unique_ids(values: Iterable[object]) -> tuple[str, ...]:
    """Return nonempty IDs once, preserving first observation order."""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SourceCursor:
    """A normalized request or response cursor."""

    kind: CursorKind
    page: int | None = None
    exclude_ids: tuple[str, ...] = ()
    token: str | None = None

    def __post_init__(self) -> None:
        if self.kind is CursorKind.PAGE:
            if self.page is None or self.page < 1:
                raise ValueError("page cursor requires a positive page")
            if self.exclude_ids or self.token is not None:
                raise ValueError("page cursor cannot contain IDs or a token")
            return
        if self.kind is CursorKind.EXCLUDE_IDS:
            if self.page is not None or self.token is not None:
                raise ValueError("exclude_ids cursor cannot contain a page or token")
            object.__setattr__(self, "exclude_ids", _stable_unique_ids(self.exclude_ids))
            return
        if self.kind is CursorKind.OPAQUE:
            if not self.token or not self.token.strip():
                raise ValueError("opaque cursor requires a token")
            if self.page is not None or self.exclude_ids:
                raise ValueError("opaque cursor cannot contain a page or IDs")
            object.__setattr__(self, "token", self.token.strip())
            return
        raise ValueError(f"unsupported cursor kind: {self.kind}")

    @classmethod
    def page_cursor(cls, page: int) -> SourceCursor:
        return cls(kind=CursorKind.PAGE, page=page)

    @classmethod
    def exclude_ids_cursor(cls, identifiers: Iterable[object]) -> SourceCursor:
        return cls(kind=CursorKind.EXCLUDE_IDS, exclude_ids=_stable_unique_ids(identifiers))

    @classmethod
    def opaque_cursor(cls, token: str) -> SourceCursor:
        return cls(kind=CursorKind.OPAQUE, token=token)


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """Declared guarantees of one market source."""

    supports_scope_mapping: bool = False
    supports_continuation: bool = False
    supports_page_pagination: bool = False
    reports_cursor: bool = False
    emits_raw_artifacts: bool = False


MOBILE_FIRST_PAGE_CAPABILITIES = SourceCapabilities(
    supports_scope_mapping=True,
    supports_continuation=False,
    supports_page_pagination=False,
    reports_cursor=True,
    emits_raw_artifacts=True,
)


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """One source request, independent from an HTTP implementation."""

    source: str
    shard_key: str
    cursor: SourceCursor | None = None
    requested_page_size: int | None = None
    scope: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source is required")
        if not self.shard_key.strip():
            raise ValueError("shard_key is required")
        if self.requested_page_size is not None and self.requested_page_size < 1:
            raise ValueError("requested_page_size must be positive")


def card_identity(card: Mapping[str, Any]) -> str | None:
    """Find a stable listing identity without parsing source-specific fields."""

    for key in ("id", "PID", "share_url", "url"):
        value = card.get(key)
        if value is None:
            continue
        identity = str(value).strip()
        if identity:
            return identity
    return None


def fingerprint_for_cards(cards: Iterable[Mapping[str, Any]]) -> str:
    """Hash sorted unique card IDs so response ordering cannot affect a verdict."""

    identifiers = sorted({identifier for card in cards if (identifier := card_identity(card)) is not None})
    payload = json.dumps(identifiers, ensure_ascii=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Normalized evidence returned by one source request."""

    source: str
    requested_cursor: SourceCursor | None
    reported_cursor: SourceCursor | None
    cards: tuple[Mapping[str, Any], ...] = ()
    actual_item_count: int | None = None
    fingerprint: str | None = None
    source_total: int | None = None
    source_total_found: int | None = None
    next_cursor: SourceCursor | None = None
    protection_status: ProtectionStatus = ProtectionStatus.UNKNOWN
    raw_response_ref: str | None = None
    timing_ms: int | None = None
    response_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source is required")
        cards = tuple(self.cards)
        if not all(isinstance(card, Mapping) for card in cards):
            raise TypeError("cards must contain mappings")
        object.__setattr__(self, "cards", cards)
        if self.actual_item_count is None:
            object.__setattr__(self, "actual_item_count", len(cards))
        elif self.actual_item_count < 0:
            raise ValueError("actual_item_count cannot be negative")
        for value_name in ("source_total", "source_total_found", "timing_ms", "response_bytes"):
            value = getattr(self, value_name)
            if value is not None and value < 0:
                raise ValueError(f"{value_name} cannot be negative")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.fingerprint is None:
            object.__setattr__(self, "fingerprint", fingerprint_for_cards(cards))


@dataclass(frozen=True, slots=True)
class NoveltyStats:
    """Duplicate and novelty accounting for one source response."""

    received_count: int
    identified_count: int
    unique_in_batch: int
    new_unique: int
    duplicate_within_batch: int
    duplicate_from_previous: int
    unknown_identity: int
    all_unique_ids: tuple[str, ...]
    new_unique_ids: tuple[str, ...]

    @property
    def duplicate_count(self) -> int:
        return self.duplicate_within_batch + self.duplicate_from_previous

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_count / self.received_count if self.received_count else 0.0


def account_novelty(
    cards: Iterable[Mapping[str, Any]],
    *,
    seen_ids: Iterable[object] = (),
) -> NoveltyStats:
    """Account for duplicate occurrences while preserving the first-seen IDs."""

    previous = set(_stable_unique_ids(seen_ids))
    batch_seen: set[str] = set()
    all_unique_ids: list[str] = []
    new_unique_ids: list[str] = []
    received_count = 0
    identified_count = 0
    duplicate_within_batch = 0
    duplicate_from_previous = 0
    unknown_identity = 0

    for card in cards:
        received_count += 1
        identifier = card_identity(card)
        if identifier is None:
            unknown_identity += 1
            continue
        identified_count += 1
        if identifier in batch_seen:
            duplicate_within_batch += 1
            continue
        batch_seen.add(identifier)
        all_unique_ids.append(identifier)
        if identifier in previous:
            duplicate_from_previous += 1
            continue
        new_unique_ids.append(identifier)

    return NoveltyStats(
        received_count=received_count,
        identified_count=identified_count,
        unique_in_batch=len(all_unique_ids),
        new_unique=len(new_unique_ids),
        duplicate_within_batch=duplicate_within_batch,
        duplicate_from_previous=duplicate_from_previous,
        unknown_identity=unknown_identity,
        all_unique_ids=tuple(all_unique_ids),
        new_unique_ids=tuple(new_unique_ids),
    )


@dataclass(frozen=True, slots=True)
class BatchState:
    """Only previously accepted observations for a single shard."""

    seen_card_ids: frozenset[str] = frozenset()
    last_fingerprint: str | None = None
    accepted_batches: int = 0

    def accept(self, result: BatchResult) -> BatchState:
        """Advance state after a validator has accepted the result."""

        identifiers = _stable_unique_ids(card_identity(card) for card in result.cards)
        return BatchState(
            seen_card_ids=self.seen_card_ids.union(identifiers),
            last_fingerprint=result.fingerprint,
            accepted_batches=self.accepted_batches + 1,
        )


@dataclass(frozen=True, slots=True)
class ContractVerdict:
    """A machine-readable batch acceptance decision."""

    state: ContractState
    reason_codes: tuple[str, ...] = ()
    novelty: NoveltyStats | None = None

    @property
    def accepted(self) -> bool:
        return self.state is ContractState.ACCEPTED


def _violation(*reasons: str, novelty: NoveltyStats | None = None) -> ContractVerdict:
    return ContractVerdict(
        state=ContractState.CONTRACT_VIOLATION,
        reason_codes=tuple(dict.fromkeys(reasons)),
        novelty=novelty,
    )


def validate_mobile_page(
    request: BatchRequest,
    result: BatchResult,
    previous: BatchState | None = None,
    *,
    capabilities: SourceCapabilities = MOBILE_FIRST_PAGE_CAPABILITIES,
) -> ContractVerdict:
    """Validate the mobile page contract before a scanner can schedule more work."""

    if result.protection_status.blocks_collection:
        return ContractVerdict(state=ContractState.BLOCKED, reason_codes=("protection_signal",))

    reasons: list[str] = []
    if request.source != result.source:
        reasons.append("source_mismatch")
    if result.requested_cursor != request.cursor:
        reasons.append("requested_cursor_mismatch")
    if request.cursor is None or request.cursor.kind is not CursorKind.PAGE:
        reasons.append("mobile_page_cursor_required")
    elif result.reported_cursor is None:
        reasons.append("reported_cursor_missing")
    elif result.reported_cursor.kind is not CursorKind.PAGE or result.reported_cursor.page != request.cursor.page:
        reasons.append("reported_cursor_mismatch")
    elif request.cursor.page and request.cursor.page > 1 and not capabilities.supports_page_pagination:
        reasons.append("page_continuation_not_supported")

    expected_fingerprint = fingerprint_for_cards(result.cards)
    if result.fingerprint != expected_fingerprint:
        reasons.append("fingerprint_mismatch")
    if result.actual_item_count != len(result.cards):
        reasons.append("actual_item_count_mismatch")

    novelty = account_novelty(result.cards, seen_ids=(previous.seen_card_ids if previous else ()))
    if result.cards and novelty.new_unique == 0:
        reasons.append("zero_novelty")
    if previous and result.cards and previous.last_fingerprint == result.fingerprint:
        reasons.append("repeated_fingerprint")
    if reasons:
        return _violation(*reasons, novelty=novelty)
    if not result.cards:
        return ContractVerdict(state=ContractState.EXHAUSTED, novelty=novelty)
    return ContractVerdict(state=ContractState.ACCEPTED, novelty=novelty)


@runtime_checkable
class MarketSource(Protocol):
    """The narrow interface adapters expose to collection orchestration."""

    name: str
    capabilities: SourceCapabilities

    async def fetch_batch(self, request: BatchRequest) -> BatchResult:
        """Fetch one normalized batch."""

    def validate_batch(
        self,
        request: BatchRequest,
        response: BatchResult,
        previous: BatchState | None,
    ) -> ContractVerdict:
        """Validate one batch against its previous accepted state."""

    def normalize(self, raw_card: Mapping[str, Any]) -> Mapping[str, Any]:
        """Normalize one source-specific card."""


SourceAdapter = MarketSource
