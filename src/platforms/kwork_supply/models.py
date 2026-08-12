"""Domain types for durable Kwork market collection jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


MAX_ENRICHMENT_PRICE = 15_000.0


def utc_now() -> str:
    """Return an RFC 3339 timestamp with second precision."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JobState(StrEnum):
    PREPARING = "preparing"
    MAPPING = "mapping"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETING = "completing"
    ENRICHING = "enriching"
    ANALYZING = "analyzing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    BLOCKED = "blocked"
    FAILED = "failed"


class JobKind(StrEnum):
    """Durable workflow namespace sharing the market control plane."""

    SUPPLY = "supply"
    BUYER_SEARCH = "buyer_search"


class JobPhase(StrEnum):
    PREPARE = "prepare"
    MAP = "map"
    PLAN = "plan"
    COLLECT = "collect"
    ENRICH = "enrich"
    ANALYZE = "analyze"
    EXPORT = "export"


class WorkerDesiredState(StrEnum):
    RUNNING = "running"
    DRAINING = "draining"
    DISABLED = "disabled"
    STOPPED = "stopped"


class WorkerState(StrEnum):
    STARTING = "starting"
    CONNECTING = "connecting"
    IDLE = "idle"
    LEASING = "leasing"
    BUSY = "busy"
    COOLDOWN = "cooldown"
    BACKOFF = "backoff"
    BLOCKED = "blocked"
    DRAINING = "draining"
    STOPPED = "stopped"
    CRASHED = "crashed"


class TransportKind(StrEnum):
    DIRECT = "direct"
    VPNTE = "vpnte"


class TransportHealth(StrEnum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


class OperationKind(StrEnum):
    MAP_SCOPE = "map_scope"
    RESOLVE_ALIAS = "resolve_alias"
    FETCH_BATCH = "fetch_batch"
    ENRICH_LISTING = "enrich_listing"
    ANALYZE_SNAPSHOT = "analyze_snapshot"
    EXPORT_SNAPSHOT = "export_snapshot"


class OperationState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CONTRACT_VIOLATION = "contract_violation"
    BLOCKED = "blocked"


class NetworkPolicy(StrEnum):
    DIRECT_ONLY = "direct_only"
    VPNTE_ONLY = "vpnte_only"
    PREFER_VPNTE = "prefer_vpnte"
    EXPLICIT_POOL = "explicit_pool"


class SourcePolicy(StrEnum):
    VALIDATED_ONLY = "validated_only"
    MOBILE_FIRST_PAGE_ONLY = "mobile_first_page_only"


class WorkerCommandKind(StrEnum):
    DRAIN = "drain"
    RESTART = "restart"
    DISABLE = "disable"
    ROTATE = "rotate"
    RECONNECT = "reconnect"
    RETRY_OPERATION = "retry_operation"


class CommandState(StrEnum):
    QUEUED = "queued"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MarketScope:
    category_id: int
    category_name: str = ""
    classifier_id: int | None = None
    classifier_name: str = ""
    canonical_alias: str | None = None
    filters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category_id <= 0:
            raise ValueError("category_id must be positive")


@dataclass(frozen=True, slots=True)
class MarketJobCreate:
    scope: MarketScope
    profile: str = "working"
    target_unique_cards: int = 60
    desired_workers: int = 2
    account_registration_ids: tuple[str, ...] = ()
    network_policy: NetworkPolicy = NetworkPolicy.PREFER_VPNTE
    source_policy: SourcePolicy = SourcePolicy.VALIDATED_ONLY
    include_ai: bool = True
    request_budget: int | None = None
    time_budget_seconds: int | None = None
    job_kind: JobKind = JobKind.SUPPLY
    workflow_config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.target_unique_cards <= 0 or self.target_unique_cards > 10_000:
            raise ValueError("target_unique_cards must be between 1 and 10000")
        if isinstance(self.desired_workers, bool) or not isinstance(self.desired_workers, int) or self.desired_workers <= 0:
            raise ValueError("desired_workers must be a positive integer")
        normalized_accounts: list[str] = []
        seen_accounts: set[str] = set()
        for value in self.account_registration_ids:
            if not isinstance(value, str):
                raise ValueError("account_registration_ids must contain strings")
            registration_id = value.strip()
            if not registration_id:
                raise ValueError("account_registration_ids cannot contain blanks")
            if registration_id not in seen_accounts:
                normalized_accounts.append(registration_id)
                seen_accounts.add(registration_id)
        if len(normalized_accounts) > 1_000:
            raise ValueError("account_registration_ids cannot contain more than 1000 accounts")
        object.__setattr__(self, "account_registration_ids", tuple(normalized_accounts))
        if self.request_budget is not None and self.request_budget <= 0:
            raise ValueError("request_budget must be positive")
        if self.time_budget_seconds is not None and self.time_budget_seconds <= 0:
            raise ValueError("time_budget_seconds must be positive")
        try:
            job_kind = JobKind(self.job_kind)
        except ValueError as exc:
            raise ValueError(f"unsupported job_kind: {self.job_kind!r}") from exc
        if not isinstance(self.workflow_config, Mapping):
            raise TypeError("workflow_config must be a mapping")
        object.__setattr__(self, "job_kind", job_kind)
        object.__setattr__(self, "workflow_config", dict(self.workflow_config))


@dataclass(frozen=True, slots=True)
class MarketJob:
    job_id: str
    scope: MarketScope
    profile: str
    target_unique_cards: int
    desired_workers: int
    network_policy: NetworkPolicy
    source_policy: SourcePolicy
    include_ai: bool
    account_registration_ids: tuple[str, ...] = ()
    state: JobState = JobState.PREPARING
    phase: JobPhase = JobPhase.PREPARE
    revision: int = 1
    request_budget: int | None = None
    time_budget_seconds: int | None = None
    counters: Mapping[str, int | float] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    latest_checkpoint_id: str | None = None
    last_error: str | None = None
    last_warning: str | None = None
    job_kind: JobKind = JobKind.SUPPLY
    workflow_config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShardSpec:
    shard_id: str
    job_id: str
    source: str
    alias: str
    filters: Mapping[str, Any] = field(default_factory=dict)
    expected_count: int | None = None
    state: str = "queued"
    priority: int = 0
    cursor: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    job_id: str
    shard_id: str | None
    kind: OperationKind
    state: OperationState = OperationState.QUEUED
    priority: int = 0
    idempotency_key: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    not_before: str | None = None
    lease_owner: str | None = None
    lease_deadline: str | None = None
    current_attempt: int = 0


@dataclass(frozen=True, slots=True)
class TransportSnapshot:
    transport_id: str
    kind: TransportKind
    health: TransportHealth = TransportHealth.UNKNOWN
    slot: int | None = None
    proxy_url: str | None = None
    profile_id: str | None = None
    profile_name: str | None = None
    country: str | None = None
    pid: int | None = None
    generation: int = 1
    lease_owner: str | None = None
    quarantine_until: str | None = None
    last_rotate_reason: str | None = None
    egress_ip: str | None = None
    egress_checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    worker_id: str
    generation: int
    desired_state: WorkerDesiredState
    actual_state: WorkerState
    runtime_kind: str = "in_process"
    transport_id: str | None = None
    current_operation_id: str | None = None
    heartbeat_at: str | None = None
    counters: Mapping[str, int | float] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class MarketEvent:
    job_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    revision: int | None = None
    worker_id: str | None = None
    operation_id: str | None = None
    emitted_at: str = field(default_factory=utc_now)
