"""SQLite durable store for Kwork market collection jobs.

The repository intentionally owns all SQLite access.  It opens short-lived
connections configured for WAL, runs blocking calls in ``asyncio.to_thread``,
and exposes JSON-serializable records for the API and worker layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from .models import (
    CommandState,
    JobPhase,
    JobState,
    MAX_ENRICHMENT_PRICE,
    MarketJobCreate,
    Operation,
    OperationKind,
    OperationState,
    ShardSpec,
    TransportSnapshot,
    WorkerCommandKind,
    WorkerRecord,
    utc_now,
)


JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict = dict[str, Any]


class MarketJobRepositoryError(RuntimeError):
    """Base error raised by the durable market-job store."""


class MarketJobNotFoundError(MarketJobRepositoryError):
    """Raised when a requested market job does not exist."""


class MarketJobRevisionConflictError(MarketJobRepositoryError):
    """Raised when an optimistic job revision check fails."""


class MarketOperationNotFoundError(MarketJobRepositoryError):
    """Raised when an operation does not exist for a requested job."""


class MarketOperationLeaseError(MarketJobRepositoryError):
    """Raised when a worker tries to finish an operation it does not lease."""


class MarketOperationLeaseLostError(MarketOperationLeaseError):
    """Raised when a stale attempt tries to use a superseded lease fence."""


class MarketCommitConflictError(MarketJobRepositoryError):
    """Raised when a batch commit key belongs to another operation."""


_TERMINAL_JOB_STATES = {JobState.COMPLETED.value, JobState.STOPPED.value, JobState.FAILED.value}
_NON_LEASABLE_JOB_STATES = (
    JobState.PAUSING.value,
    JobState.PAUSED.value,
    JobState.COMPLETING.value,
    JobState.STOPPING.value,
    JobState.STOPPED.value,
    JobState.BLOCKED.value,
    JobState.FAILED.value,
    JobState.COMPLETED.value,
)
_UNSET = object()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_jobs (
    job_id TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL,
    category_name TEXT NOT NULL DEFAULT '',
    classifier_id INTEGER,
    classifier_name TEXT NOT NULL DEFAULT '',
    canonical_alias TEXT,
    scope_filters_json TEXT NOT NULL DEFAULT '{}',
    profile TEXT NOT NULL,
    target_unique_cards INTEGER NOT NULL,
    desired_workers INTEGER NOT NULL,
    account_registration_ids_json TEXT NOT NULL DEFAULT '[]',
    job_kind TEXT NOT NULL DEFAULT 'supply',
    config_json TEXT NOT NULL DEFAULT '{}',
    network_policy TEXT NOT NULL,
    source_policy TEXT NOT NULL,
    include_ai INTEGER NOT NULL,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    revision INTEGER NOT NULL,
    request_budget INTEGER,
    time_budget_seconds INTEGER,
    counters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    latest_checkpoint_id TEXT,
    last_error TEXT,
    last_warning TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_category_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    canonical_alias TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    active_category_id INTEGER,
    source_url TEXT,
    last_validated_at TEXT,
    protection_state TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category_id, canonical_alias)
);
CREATE INDEX IF NOT EXISTS idx_market_category_aliases_category
    ON market_category_aliases(category_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS market_shards (
    shard_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    alias TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    expected_count INTEGER,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    cursor_json TEXT,
    last_fingerprint TEXT,
    counters_json TEXT NOT NULL DEFAULT '{}',
    received_count INTEGER NOT NULL DEFAULT 0,
    new_unique_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    consecutive_zero_novelty INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    shard_id TEXT REFERENCES market_shards(shard_id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    not_before TEXT,
    lease_owner TEXT,
    lease_deadline TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    leased_attempt_id TEXT,
    current_attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_operations_idempotency
    ON market_operations(job_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_market_operations_queue
    ON market_operations(job_id, state, not_before, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_market_operations_lease
    ON market_operations(state, lease_deadline);

CREATE TABLE IF NOT EXISTS market_operation_attempts (
    attempt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES market_operations(operation_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    worker_id TEXT,
    transport_id TEXT,
    requested_cursor_json TEXT,
    reported_cursor_json TEXT,
    request_json TEXT,
    response_status INTEGER,
    response_bytes INTEGER,
    duration_ms INTEGER,
    received_count INTEGER NOT NULL DEFAULT 0,
    new_unique_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    page_fingerprint TEXT,
    raw_response_ref TEXT,
    failure_kind TEXT,
    retry_after TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    UNIQUE(operation_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_market_attempts_operation ON market_operation_attempts(operation_id, attempt_number);

CREATE TABLE IF NOT EXISTS market_workers (
    worker_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES market_jobs(job_id) ON DELETE SET NULL,
    generation INTEGER NOT NULL,
    desired_state TEXT NOT NULL,
    actual_state TEXT NOT NULL,
    runtime_kind TEXT NOT NULL,
    transport_id TEXT,
    current_operation_id TEXT REFERENCES market_operations(operation_id) ON DELETE SET NULL,
    heartbeat_at TEXT,
    counters_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_transports (
    transport_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    health TEXT NOT NULL,
    slot INTEGER,
    proxy_url TEXT,
    profile_id TEXT,
    profile_name TEXT,
    country TEXT,
    pid INTEGER,
    generation INTEGER NOT NULL,
    lease_owner TEXT,
    quarantine_until TEXT,
    last_rotate_reason TEXT,
    egress_ip TEXT,
    egress_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_transports_slot
    ON market_transports(slot) WHERE slot IS NOT NULL;

CREATE TABLE IF NOT EXISTS market_account_inventory (
    registration_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    market_enabled INTEGER NOT NULL DEFAULT 1,
    session_cookie_count INTEGER NOT NULL DEFAULT 0,
    signup_ip TEXT,
    preferred_slot INTEGER,
    preferred_transport_id TEXT,
    persona_id TEXT,
    last_used_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_account_inventory_available
    ON market_account_inventory(market_enabled, status, last_used_at, registration_id);

CREATE TABLE IF NOT EXISTS market_worker_identity_bindings (
    worker_id TEXT PRIMARY KEY REFERENCES market_workers(worker_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    registration_id TEXT NOT NULL REFERENCES market_account_inventory(registration_id) ON DELETE RESTRICT,
    transport_id TEXT REFERENCES market_transports(transport_id) ON DELETE SET NULL,
    current_egress_ip TEXT,
    binding_mode TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_deadline TEXT,
    assigned_at TEXT NOT NULL,
    released_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_identity_active_account
    ON market_worker_identity_bindings(registration_id)
    WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_identity_active_transport
    ON market_worker_identity_bindings(transport_id)
    WHERE state = 'active' AND transport_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_identity_active_egress
    ON market_worker_identity_bindings(current_egress_ip)
    WHERE state = 'active' AND current_egress_ip IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_market_identity_job
    ON market_worker_identity_bindings(job_id, state, worker_id);

CREATE TABLE IF NOT EXISTS market_listings (
    listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    listing_key TEXT NOT NULL,
    title TEXT,
    seller_key TEXT,
    price REAL,
    canonical_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, listing_key)
);
CREATE INDEX IF NOT EXISTS idx_market_listings_job_seen ON market_listings(job_id, first_seen_at, listing_id);

CREATE TABLE IF NOT EXISTS market_listing_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    listing_id INTEGER NOT NULL REFERENCES market_listings(listing_id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL REFERENCES market_operations(operation_id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES market_operation_attempts(attempt_id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    shard_id TEXT REFERENCES market_shards(shard_id) ON DELETE SET NULL,
    response_position INTEGER NOT NULL,
    requested_cursor_json TEXT,
    reported_cursor_json TEXT,
    raw_response_ref TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(operation_id, response_position)
);
CREATE INDEX IF NOT EXISTS idx_market_observations_job ON market_listing_observations(job_id, observation_id);
CREATE INDEX IF NOT EXISTS idx_market_observations_listing ON market_listing_observations(listing_id, observation_id);

CREATE TABLE IF NOT EXISTS market_listing_features (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    listing_id INTEGER NOT NULL REFERENCES market_listings(listing_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    resolved_seller_key TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    extra_json TEXT NOT NULL DEFAULT '{}',
    description TEXT,
    instructions TEXT,
    service_size TEXT,
    queue_count INTEGER,
    work_time_seconds INTEGER,
    listing_reviews_count INTEGER,
    good_reviews INTEGER,
    bad_reviews INTEGER,
    last_review_at TEXT,
    fetched_at TEXT,
    expires_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_market_listing_features_status
    ON market_listing_features(job_id, status, expires_at, listing_id);

CREATE TABLE IF NOT EXISTS market_seller_features (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    seller_key TEXT NOT NULL,
    seller_id TEXT,
    status TEXT NOT NULL,
    profile_json TEXT NOT NULL DEFAULT '{}',
    seller_rating REAL,
    seller_rating_count INTEGER,
    seller_reviews_count INTEGER,
    seller_addtime TEXT,
    completed_orders_count INTEGER,
    active_kworks_count INTEGER,
    fetched_at TEXT,
    expires_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, seller_key)
);
CREATE INDEX IF NOT EXISTS idx_market_seller_features_status
    ON market_seller_features(job_id, status, expires_at, seller_key);

CREATE TABLE IF NOT EXISTS market_listing_reviews (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    listing_id INTEGER NOT NULL REFERENCES market_listings(listing_id) ON DELETE CASCADE,
    review_key TEXT NOT NULL,
    time_added TEXT,
    is_good INTEGER,
    is_bad INTEGER,
    review_text TEXT,
    writer TEXT,
    answer TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    PRIMARY KEY(job_id, listing_id, review_key)
);
CREATE INDEX IF NOT EXISTS idx_market_listing_reviews_recent
    ON market_listing_reviews(job_id, listing_id, time_added DESC);

CREATE TABLE IF NOT EXISTS market_listing_embeddings (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    listing_id INTEGER NOT NULL REFERENCES market_listings(listing_id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_market_listing_embeddings_model
    ON market_listing_embeddings(job_id, model_name, text_hash);

CREATE TABLE IF NOT EXISTS market_semantic_clusters (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    cluster_id TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    dossier_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, cluster_id)
);
CREATE INDEX IF NOT EXISTS idx_market_semantic_clusters_state
    ON market_semantic_clusters(job_id, state, confidence DESC);

CREATE TABLE IF NOT EXISTS market_semantic_dossiers (
    job_id TEXT PRIMARY KEY REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    dossier_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_semantic_cluster_members (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    cluster_id TEXT NOT NULL,
    listing_id INTEGER NOT NULL REFERENCES market_listings(listing_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY(job_id, cluster_id, listing_id),
    FOREIGN KEY(job_id, cluster_id)
        REFERENCES market_semantic_clusters(job_id, cluster_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_market_semantic_cluster_members_listing
    ON market_semantic_cluster_members(job_id, listing_id);

CREATE TABLE IF NOT EXISTS market_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    source_cluster_id TEXT,
    state TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    classifier_id INTEGER,
    service_summary TEXT NOT NULL,
    price INTEGER NOT NULL,
    work_time INTEGER NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    terra_result_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_recommendations_job
    ON market_recommendations(job_id, updated_at DESC, recommendation_id);

CREATE TABLE IF NOT EXISTS market_draft_handoffs (
    handoff_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    recommendation_id TEXT NOT NULL UNIQUE REFERENCES market_recommendations(recommendation_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    classifier_id INTEGER,
    service_summary TEXT NOT NULL,
    price INTEGER NOT NULL,
    work_time INTEGER NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    manifest_hash TEXT,
    selection_json TEXT NOT NULL DEFAULT '{}',
    selection_hash TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    generator_request_json TEXT NOT NULL DEFAULT '{}',
    draft_json TEXT NOT NULL DEFAULT '{}',
    draft_hash TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_draft_handoffs_job
    ON market_draft_handoffs(job_id, updated_at DESC, handoff_id);

CREATE TABLE IF NOT EXISTS market_published_listings (
    published_listing_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    recommendation_id TEXT NOT NULL REFERENCES market_recommendations(recommendation_id) ON DELETE CASCADE,
    handoff_id TEXT NOT NULL REFERENCES market_draft_handoffs(handoff_id) ON DELETE CASCADE,
    source_cluster_id TEXT,
    kwork_id TEXT,
    draft_hash TEXT NOT NULL,
    publish_result_json TEXT NOT NULL DEFAULT '{}',
    feedback_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(handoff_id, draft_hash)
);
CREATE INDEX IF NOT EXISTS idx_market_published_listings_cluster
    ON market_published_listings(job_id, source_cluster_id, created_at DESC);

CREATE TABLE IF NOT EXISTS market_events (
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER,
    worker_id TEXT,
    operation_id TEXT REFERENCES market_operations(operation_id) ON DELETE SET NULL,
    emitted_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_market_events_replay ON market_events(job_id, sequence);

CREATE TABLE IF NOT EXISTS market_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    phase TEXT NOT NULL,
    frontier_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    last_event_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_checkpoints_job ON market_checkpoints(job_id, revision DESC);

CREATE TABLE IF NOT EXISTS market_worker_commands (
    command_id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    worker_id TEXT REFERENCES market_workers(worker_id) ON DELETE SET NULL,
    command_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    completed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_worker_commands_queue
    ON market_worker_commands(worker_id, state, created_at);

CREATE TABLE IF NOT EXISTS market_batch_commits (
    commit_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES market_jobs(job_id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL REFERENCES market_operations(operation_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    UNIQUE(job_id, idempotency_key),
    UNIQUE(job_id, operation_id)
);
"""


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _jsonable(value: Any) -> JsonValue:
    """Convert supported domain values into JSON-compatible primitives."""

    value = _enum_value(value)
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _dump_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _timestamp(value: str | datetime | None = None) -> str:
    if value is None:
        return utc_now()
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lease_deadline(now: str, lease_seconds: int) -> str:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return _timestamp(parsed + timedelta(seconds=lease_seconds))


def _new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _listing_key(listing: Mapping[str, Any]) -> str:
    for key in ("listing_key", "listing_id", "id", "PID", "share_url", "url"):
        value = listing.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    raise ValueError("listing requires one of listing_key, listing_id, id, PID, share_url, or url")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    return int(number) if number is not None else None


def _optional_flag(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "none", "null"}:
            return None
        if normalized in {"0", "false", "no"}:
            return 0
        if normalized in {"1", "true", "yes"}:
            return 1
    return int(bool(value))


def _listing_title(listing: Mapping[str, Any]) -> str | None:
    for key in ("title", "gtitle", "name", "service_title"):
        value = _optional_text(listing.get(key))
        if value is not None:
            return value
    return None


class MarketJobRepository:
    """Durable SQLite repository for the Kwork market job control plane."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialized = False
        self._initialization_lock = threading.Lock()

    async def initialize(self) -> None:
        """Create the schema before the repository is used by workers."""

        await asyncio.to_thread(self._ensure_initialized)

    async def close(self) -> None:
        """Keep a symmetric lifecycle hook; connections are request-scoped."""

    async def create_job(self, create: MarketJobCreate, *, job_id: str | None = None) -> JsonDict:
        """Persist a new job in its initial ``preparing`` state."""

        if not isinstance(create, MarketJobCreate):
            raise TypeError("create must be a MarketJobCreate")
        return await asyncio.to_thread(self._create_job_sync, create, job_id)

    async def get_job(self, job_id: str) -> JsonDict | None:
        """Return one job summary, or ``None`` when it is absent."""

        return await asyncio.to_thread(self._get_job_sync, job_id)

    async def list_jobs(
        self,
        *,
        states: Iterable[JobState | str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JsonDict]:
        """List job summaries in newest-first order."""

        return await asyncio.to_thread(self._list_jobs_sync, states, limit, offset)

    async def update_job_state(
        self,
        job_id: str,
        state: JobState | str,
        *,
        phase: JobPhase | str | None = None,
        expected_revision: int | None = None,
        counters: Mapping[str, int | float] | None = None,
        last_error: str | None | object = _UNSET,
        last_warning: str | None | object = _UNSET,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Transition a job and increment its revision atomically."""

        return await asyncio.to_thread(
            self._update_job_state_sync,
            job_id,
            _enum_value(state),
            _enum_value(phase),
            expected_revision,
            counters,
            last_error,
            last_warning,
            _timestamp(now),
        )

    async def update_job_configuration(
        self,
        job_id: str,
        *,
        desired_workers: int | None = None,
        target_unique_cards: int | None = None,
        profile: str | None = None,
        request_budget: int | None | object = _UNSET,
        time_budget_seconds: int | None | object = _UNSET,
        expected_revision: int | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Update mutable execution controls and advance the job revision.

        Passing ``None`` for a budget explicitly clears it. Omitted values are
        left unchanged so UI controls can issue narrow optimistic updates.
        """

        return await asyncio.to_thread(
            self._update_job_configuration_sync,
            job_id,
            desired_workers,
            target_unique_cards,
            profile,
            request_budget,
            time_budget_seconds,
            expected_revision,
            _timestamp(now),
        )

    async def update_job_settings(
        self,
        job_id: str,
        **settings: Any,
    ) -> JsonDict:
        """Compatibility spelling for :meth:`update_job_configuration`."""

        return await self.update_job_configuration(job_id, **settings)

    async def create_shard(self, shard: ShardSpec | Mapping[str, Any]) -> JsonDict:
        """Create a durable collection shard for an existing job."""

        return await asyncio.to_thread(self._create_shard_sync, shard)

    async def upsert_category_alias(
        self,
        category_id: int,
        canonical_alias: str,
        *,
        validation_status: str,
        active_category_id: int | None = None,
        source_url: str | None = None,
        last_validated_at: str | datetime | None = None,
        protection_state: str | None = None,
        schema_version: int = 1,
    ) -> JsonDict:
        """Persist the latest validation result for a canonical category alias."""

        return await asyncio.to_thread(
            self._upsert_category_alias_sync,
            category_id,
            canonical_alias,
            validation_status,
            active_category_id,
            source_url,
            _timestamp(last_validated_at) if last_validated_at is not None else None,
            protection_state,
            schema_version,
        )

    async def get_category_alias(self, category_id: int, canonical_alias: str) -> JsonDict | None:
        """Return one persisted alias validation record."""

        return await asyncio.to_thread(self._get_category_alias_sync, category_id, canonical_alias)

    async def get_shard(self, shard_id: str) -> JsonDict | None:
        """Return one shard record."""

        return await asyncio.to_thread(self._get_shard_sync, shard_id)

    async def list_shards(self, job_id: str, *, limit: int = 100, offset: int = 0) -> list[JsonDict]:
        """List a job's shards by priority and creation order."""

        return await asyncio.to_thread(self._list_shards_sync, job_id, limit, offset)

    async def enqueue_operation(self, operation: Operation | Mapping[str, Any]) -> JsonDict:
        """Enqueue an operation, deduplicating an explicit idempotency key."""

        return await asyncio.to_thread(self._enqueue_operation_sync, operation)

    async def get_operation(self, operation_id: str) -> JsonDict | None:
        """Return an operation record."""

        return await asyncio.to_thread(self._get_operation_sync, operation_id)

    async def list_operation_attempts(self, operation_id: str, *, limit: int = 100) -> list[JsonDict]:
        """List one operation's durable request/response evidence by attempt."""

        return await asyncio.to_thread(self._list_operation_attempts_sync, operation_id, limit)

    async def get_job_concurrency_signals(self, job_id: str, *, window: int = 50) -> JsonDict:
        """Aggregate a bounded recent attempt window for concurrency advice.

        The result is advisory only. It never mutates the job's configured
        worker count, so an operator's explicit desired pool remains durable.
        """

        return await asyncio.to_thread(self._get_job_concurrency_signals_sync, job_id, window)

    async def list_operations(
        self,
        job_id: str,
        *,
        state: OperationState | str | Iterable[OperationState | str] | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List job operations after an opaque operation-ID cursor."""

        return await asyncio.to_thread(self._list_operations_sync, job_id, state, cursor, limit)

    async def find_active_operation(
        self,
        job_id: str,
        *,
        kinds: Iterable[OperationKind | str],
    ) -> JsonDict | None:
        """Return one queued/running operation for the requested kinds."""

        return await asyncio.to_thread(self._find_active_operation_sync, job_id, tuple(kinds))

    async def list_unresolved_terminal_enrichment_operations(
        self,
        job_id: str,
        *,
        limit: int = 100,
    ) -> list[JsonDict]:
        """Return terminal enrichment operations that have no explicit retry descendant."""

        return await asyncio.to_thread(self._list_unresolved_terminal_enrichment_operations_sync, job_id, limit)

    async def lease_operation(
        self,
        worker_id: str,
        *,
        job_id: str | None = None,
        transport_id: str | None = None,
        lease_seconds: int = 60,
        now: str | datetime | None = None,
    ) -> JsonDict | None:
        """Lease one ready operation after reclaiming expired leases."""

        return await asyncio.to_thread(
            self._lease_operation_sync,
            worker_id,
            job_id,
            _optional_text(transport_id),
            lease_seconds,
            _timestamp(now),
        )

    async def lease_next_operation(
        self,
        worker_id: str,
        *,
        job_id: str | None = None,
        transport_id: str | None = None,
        lease_seconds: int = 60,
        now: str | datetime | None = None,
    ) -> JsonDict | None:
        """Compatibility spelling for :meth:`lease_operation`."""

        return await self.lease_operation(
            worker_id,
            job_id=job_id,
            transport_id=transport_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    async def renew_operation_lease(
        self,
        operation_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        attempt_id: str | None = None,
        lease_fence: int | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Extend a worker-owned lease and return the refreshed operation."""

        return await asyncio.to_thread(
            self._renew_operation_lease_sync,
            operation_id,
            worker_id,
            lease_seconds,
            _optional_text(attempt_id),
            lease_fence,
            _timestamp(now),
        )

    async def complete_operation(
        self,
        operation_id: str,
        worker_id: str,
        *,
        attempt_id: str | None = None,
        lease_fence: int | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Complete a generic worker operation owned by ``worker_id``."""

        return await asyncio.to_thread(
            self._complete_operation_sync,
            operation_id,
            worker_id,
            _optional_text(attempt_id),
            lease_fence,
            _timestamp(now),
        )

    async def fail_operation(
        self,
        operation_id: str,
        worker_id: str,
        error: str,
        *,
        retry_at: str | datetime | None = None,
        failure_kind: str | None = None,
        attempt_id: str | None = None,
        lease_fence: int | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Fail a lease-owned operation, optionally putting it into retry wait."""

        if not _optional_text(error):
            raise ValueError("error is required")
        return await asyncio.to_thread(
            self._fail_operation_sync,
            operation_id,
            worker_id,
            error,
            _timestamp(retry_at) if retry_at is not None else None,
            failure_kind,
            _optional_text(attempt_id),
            lease_fence,
            _timestamp(now),
        )

    async def recover_expired_operations(self, *, now: str | datetime | None = None) -> int:
        """Return the number of operations reclaimed from an expired lease."""

        return await asyncio.to_thread(self._recover_expired_operations_sync, _timestamp(now))

    async def cancel_collection_operations(self, job_id: str, *, reason: str) -> int:
        """Cancel pending collection work while retaining already committed evidence."""

        if not _optional_text(reason):
            raise ValueError("reason is required")
        return await asyncio.to_thread(self._cancel_collection_operations_sync, job_id, reason, utc_now())

    async def append_event(
        self,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        revision: int | None = None,
        worker_id: str | None = None,
        operation_id: str | None = None,
        emitted_at: str | datetime | None = None,
    ) -> JsonDict:
        """Append one durable monotonic event for a job."""

        return await asyncio.to_thread(
            self._append_event_sync,
            job_id,
            event_type,
            payload or {},
            revision,
            worker_id,
            operation_id,
            _timestamp(emitted_at),
        )

    async def replay_events(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[JsonDict]:
        """Replay events strictly after a client sequence cursor."""

        if after_seq is not None:
            after_sequence = after_seq
        return await asyncio.to_thread(self._replay_events_sync, job_id, after_sequence, limit)

    async def list_recent_events(self, job_id: str, *, limit: int = 500) -> list[JsonDict]:
        """Return the latest durable events in chronological order."""

        return await asyncio.to_thread(self._list_recent_events_sync, job_id, limit)

    async def get_request_concurrency_summary(self, job_id: str) -> JsonDict:
        """Summarize actual overlapping catalog attempts and identity routes."""

        return await asyncio.to_thread(self._get_request_concurrency_summary_sync, job_id)

    async def get_event_sequence_bounds(self, job_id: str) -> JsonDict:
        """Return the first and last durable event sequence for one job."""

        return await asyncio.to_thread(self._get_event_sequence_bounds_sync, job_id)

    async def commit_accepted_batch(
        self,
        *,
        job_id: str,
        shard_id: str,
        operation_id: str,
        listings: Sequence[Mapping[str, Any]],
        source: str | None = None,
        idempotency_key: str | None = None,
        attempt_id: str | None = None,
        worker_id: str | None = None,
        lease_fence: int | None = None,
        requested_cursor: Mapping[str, Any] | None = None,
        reported_cursor: Mapping[str, Any] | None = None,
        next_cursor: Mapping[str, Any] | None = None,
        raw_response_ref: str | None = None,
        fingerprint: str | None = None,
        counter_deltas: Mapping[str, int | float] | None = None,
        counters: Mapping[str, int | float] | None = None,
        event_payloads: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        next_operation: Mapping[str, Any] | None = None,
        shard_state: str = "active",
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Atomically persist an accepted source batch.

        ``counter_deltas`` and the legacy ``counters`` alias are increments;
        canonical listing and observation totals are always derived from the
        database.  A replay of the same ``idempotency_key`` returns the first
        commit result without changing rows, counters, events, or checkpoints.
        """

        if counter_deltas is not None and counters is not None:
            raise ValueError("pass either counter_deltas or counters, not both")
        if not isinstance(listings, Sequence) or isinstance(listings, (str, bytes)):
            raise TypeError("listings must be a sequence of mappings")
        if not all(isinstance(listing, Mapping) for listing in listings):
            raise TypeError("listings must contain mappings")
        return await asyncio.to_thread(
            self._commit_accepted_batch_sync,
            job_id,
            shard_id,
            operation_id,
            tuple(listings),
            source,
            idempotency_key or f"accepted:{operation_id}",
            attempt_id,
            _optional_text(worker_id),
            lease_fence,
            requested_cursor,
            reported_cursor,
            next_cursor,
            raw_response_ref,
            fingerprint,
            counter_deltas if counter_deltas is not None else counters,
            event_payloads,
            next_operation,
            shard_state,
            _timestamp(now),
        )

    async def upsert_worker(
        self,
        worker: WorkerRecord | Mapping[str, Any],
        *,
        job_id: str | None = None,
    ) -> JsonDict:
        """Persist the latest durable worker record and optional job binding."""

        return await asyncio.to_thread(self._upsert_worker_sync, worker, job_id)

    async def get_worker(self, worker_id: str) -> JsonDict | None:
        """Return a worker status record."""

        return await asyncio.to_thread(self._get_worker_sync, worker_id)

    async def list_workers(
        self,
        *,
        job_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List durable worker records, optionally scoped to active job work."""

        return await asyncio.to_thread(self._list_workers_sync, job_id, cursor, limit)

    async def upsert_transport(self, transport: TransportSnapshot | Mapping[str, Any]) -> JsonDict:
        """Persist the latest durable transport snapshot."""

        return await asyncio.to_thread(self._upsert_transport_sync, transport)

    async def get_transport(self, transport_id: str) -> JsonDict | None:
        """Return one durable transport record."""

        return await asyncio.to_thread(self._get_transport_sync, transport_id)

    async def list_transports(
        self,
        *,
        job_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List transports, optionally constrained to active job workers."""

        return await asyncio.to_thread(self._list_transports_sync, job_id, cursor, limit)

    async def sync_market_account_inventory(self, accounts: Sequence[Mapping[str, Any]]) -> list[JsonDict]:
        """Mirror safe registration metadata used by the Market identity pool."""

        return await asyncio.to_thread(self._sync_market_account_inventory_sync, tuple(dict(item) for item in accounts))

    async def list_market_account_inventory(self, *, enabled_only: bool = False) -> list[JsonDict]:
        """List the safe Market-facing account inventory without credentials."""

        return await asyncio.to_thread(self._list_market_account_inventory_sync, enabled_only)

    async def set_market_account_enabled(self, registration_id: str, enabled: bool) -> JsonDict:
        """Enable or disable one locally registered account for Market leases."""

        return await asyncio.to_thread(self._set_market_account_enabled_sync, registration_id, enabled)

    async def get_market_identity_binding(self, worker_id: str) -> JsonDict | None:
        """Return the durable identity assignment owned by one worker."""

        return await asyncio.to_thread(self._get_market_identity_binding_sync, worker_id)

    async def list_market_identity_bindings(
        self,
        *,
        job_id: str | None = None,
        active_only: bool = False,
    ) -> list[JsonDict]:
        """List worker/account/IP assignments, optionally scoped to one job."""

        return await asyncio.to_thread(self._list_market_identity_bindings_sync, job_id, active_only)

    async def claim_market_identity_binding(
        self,
        *,
        worker_id: str,
        job_id: str,
        registration_id: str,
        transport_id: str,
        current_egress_ip: str,
        binding_mode: str,
        lease_token: str,
        lease_deadline: str | None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Atomically reserve one account and one unique egress route for a worker."""

        return await asyncio.to_thread(
            self._claim_market_identity_binding_sync,
            worker_id,
            job_id,
            registration_id,
            transport_id,
            current_egress_ip,
            binding_mode,
            lease_token,
            lease_deadline,
            _timestamp(now),
        )

    async def renew_market_identity_binding(
        self,
        worker_id: str,
        *,
        lease_token: str,
        lease_deadline: str | None,
        now: str | datetime | None = None,
    ) -> JsonDict | None:
        """Extend a running worker's durable account/IP lease."""

        return await asyncio.to_thread(
            self._renew_market_identity_binding_sync,
            worker_id,
            lease_token,
            lease_deadline,
            _timestamp(now),
        )

    async def release_market_identity_binding(
        self,
        worker_id: str,
        *,
        reason: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict | None:
        """Release a worker's account/IP reservation without deleting history."""

        return await asyncio.to_thread(
            self._release_market_identity_binding_sync,
            worker_id,
            reason,
            _timestamp(now),
        )

    async def clear_market_identity_route(
        self,
        worker_id: str,
        *,
        reason: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict | None:
        """Keep an account lease but return its current route for a later rebind."""

        return await asyncio.to_thread(
            self._clear_market_identity_route_sync,
            worker_id,
            reason,
            _timestamp(now),
        )

    async def get_listing(self, job_id: str, listing_id: int | str) -> JsonDict | None:
        """Return a normalized listing by its durable integer identifier."""

        return await asyncio.to_thread(self._get_listing_sync, job_id, listing_id)

    async def list_listings(
        self,
        job_id: str,
        *,
        cursor: int | str | None = None,
        limit: int = 100,
        shard_id: str | None = None,
    ) -> list[JsonDict]:
        """List normalized listings after an integer listing-ID cursor."""

        return await asyncio.to_thread(self._list_listings_sync, job_id, cursor, limit, shard_id)

    async def prepare_listing_enrichment(
        self,
        job_id: str,
        *,
        price_limit: float = 15_000,
        cache_ttl_seconds: int = 86_400,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Durably price-gate listings and queue one enrichment operation per eligible card.

        The operation queue, price rejections, and cache generations are written in
        one SQLite transaction. Repeating this call after a pause is safe: active
        operations are retained and completed, unexpired records are reused.
        """

        if price_limit <= 0 or price_limit > MAX_ENRICHMENT_PRICE:
            raise ValueError(f"price_limit must be between 0 and {int(MAX_ENRICHMENT_PRICE)}")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        return await asyncio.to_thread(
            self._prepare_listing_enrichment_sync,
            job_id,
            float(price_limit),
            int(cache_ttl_seconds),
            _timestamp(now),
        )

    async def get_listing_enrichment(self, job_id: str, listing_id: int | str) -> JsonDict | None:
        """Return local listing features and the durable last-review projection."""

        return await asyncio.to_thread(self._get_listing_enrichment_sync, job_id, listing_id)

    async def get_seller_enrichment(self, job_id: str, seller_key: str) -> JsonDict | None:
        """Return one seller snapshot kept for the current market job."""

        return await asyncio.to_thread(self._get_seller_enrichment_sync, job_id, seller_key)

    async def persist_listing_enrichment(
        self,
        *,
        job_id: str,
        listing_id: int | str,
        listing_features: Mapping[str, Any],
        seller_features: Mapping[str, Any] | None = None,
        reviews: Sequence[Mapping[str, Any]] = (),
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Persist one listing snapshot, its review projection, and an optional seller cache entry."""

        if not isinstance(listing_features, Mapping):
            raise TypeError("listing_features must be a mapping")
        if seller_features is not None and not isinstance(seller_features, Mapping):
            raise TypeError("seller_features must be a mapping when provided")
        if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes)):
            raise TypeError("reviews must be a sequence of mappings")
        if not all(isinstance(review, Mapping) for review in reviews):
            raise TypeError("reviews must contain mappings")
        return await asyncio.to_thread(
            self._persist_listing_enrichment_sync,
            job_id,
            listing_id,
            dict(listing_features),
            dict(seller_features) if seller_features is not None else None,
            tuple(dict(review) for review in reviews),
            _timestamp(now),
        )

    async def mark_listing_enrichment_failed(
        self,
        job_id: str,
        listing_id: int | str,
        *,
        generation: int,
        error: str,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Keep a terminal enrichment failure durable without erasing prior facts."""

        return await asyncio.to_thread(
            self._mark_listing_enrichment_failed_sync,
            job_id,
            listing_id,
            max(int(generation), 1),
            _optional_text(error) or "enrichment failed",
            _timestamp(now),
        )

    async def list_local_analysis_inputs(self, job_id: str) -> list[JsonDict]:
        """Return price-eligible enriched cards with seller and review features."""

        return await asyncio.to_thread(self._list_local_analysis_inputs_sync, job_id)

    async def get_embedding_cache(self, job_id: str, model_name: str) -> dict[str, list[float]]:
        """Return reusable local vectors keyed by the exact normalized text hash."""

        normalized_model = _optional_text(model_name)
        if normalized_model is None:
            raise ValueError("model_name cannot be blank")
        return await asyncio.to_thread(self._get_embedding_cache_sync, job_id, normalized_model)

    async def replace_semantic_analysis(
        self,
        job_id: str,
        analysis: Mapping[str, Any],
        *,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Atomically persist local vectors, cluster membership, and Terra dossier."""

        if not isinstance(analysis, Mapping):
            raise TypeError("analysis must be a mapping")
        return await asyncio.to_thread(
            self._replace_semantic_analysis_sync,
            job_id,
            dict(analysis),
            _timestamp(now),
        )

    async def get_semantic_analysis(self, job_id: str) -> JsonDict:
        """Load durable semantic clusters and their bounded dossier records."""

        return await asyncio.to_thread(self._get_semantic_analysis_sync, job_id)

    async def create_recommendation(
        self,
        job_id: str,
        *,
        category_id: int,
        service_summary: str,
        price: int,
        work_time: int,
        classifier_id: int | None = None,
        source_cluster_id: str | None = None,
        evidence_ids: Sequence[str] = (),
        terra_result: Mapping[str, Any] | None = None,
        recommendation_id: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Persist a proposed market recommendation before user confirmation."""

        if category_id <= 0 or price <= 0 or work_time <= 0:
            raise ValueError("category_id, price, and work_time must be positive")
        if not _optional_text(service_summary):
            raise ValueError("service_summary cannot be blank")
        return await asyncio.to_thread(
            self._create_recommendation_sync,
            job_id,
            int(category_id),
            _optional_text(service_summary) or "",
            int(price),
            int(work_time),
            int(classifier_id) if classifier_id is not None else None,
            _optional_text(source_cluster_id),
            tuple(str(item) for item in evidence_ids if _optional_text(item)),
            dict(terra_result) if isinstance(terra_result, Mapping) else {},
            recommendation_id,
            _timestamp(now),
        )

    async def get_recommendation(self, recommendation_id: str) -> JsonDict | None:
        """Return one durable recommendation."""

        return await asyncio.to_thread(self._get_recommendation_sync, recommendation_id)

    async def list_recommendations(self, job_id: str, *, limit: int = 100) -> list[JsonDict]:
        """List recommendations created for one market job."""

        return await asyncio.to_thread(self._list_recommendations_sync, job_id, limit)

    async def transition_recommendation(
        self,
        recommendation_id: str,
        state: str,
        *,
        expected_revision: int | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Confirm or reject a recommendation and create its handoff on confirm."""

        if state not in {"recommendation_confirmed", "rejected"}:
            raise ValueError("recommendation state must be recommendation_confirmed or rejected")
        return await asyncio.to_thread(
            self._transition_recommendation_sync,
            recommendation_id,
            state,
            expected_revision,
            _timestamp(now),
        )

    async def get_draft_handoff(self, handoff_id: str) -> JsonDict | None:
        """Return one durable recommendation-to-draft handoff."""

        return await asyncio.to_thread(self._get_draft_handoff_sync, handoff_id)

    async def get_draft_handoff_for_recommendation(self, recommendation_id: str) -> JsonDict | None:
        """Return the one handoff owned by a recommendation, when it exists."""

        return await asyncio.to_thread(self._get_draft_handoff_for_recommendation_sync, recommendation_id)

    async def save_draft_handoff_fields(
        self,
        handoff_id: str,
        *,
        manifest: Mapping[str, Any],
        manifest_hash: str,
        selection: Mapping[str, Any],
        selection_hash: str,
        validation: Mapping[str, Any],
        fields_confirmed: bool,
        expected_manifest_hash: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Persist one validated manifest/selection snapshot and invalidate stale drafts."""

        if not manifest_hash.startswith("sha256:") or not selection_hash.startswith("sha256:"):
            raise ValueError("manifest_hash and selection_hash must use sha256")
        if fields_confirmed and not bool(validation.get("valid")):
            raise ValueError("required Kwork fields are unresolved")
        return await asyncio.to_thread(
            self._save_draft_handoff_fields_sync,
            handoff_id,
            dict(manifest),
            manifest_hash,
            dict(selection),
            selection_hash,
            dict(validation),
            bool(fields_confirmed),
            expected_manifest_hash,
            _timestamp(now),
        )

    async def store_draft_handoff_draft(
        self,
        handoff_id: str,
        *,
        draft: Mapping[str, Any],
        draft_hash: str,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Persist a generated draft after the field snapshot was confirmed."""

        if not draft_hash:
            raise ValueError("draft_hash cannot be blank")
        return await asyncio.to_thread(
            self._store_draft_handoff_draft_sync,
            handoff_id,
            dict(draft),
            draft_hash,
            _timestamp(now),
        )

    async def record_published_listing(
        self,
        handoff_id: str,
        *,
        publish_result: Mapping[str, Any],
        kwork_id: str | int | None = None,
        draft_hash: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Link a verified live listing back to the recommendation cluster."""

        if not isinstance(publish_result, Mapping):
            raise TypeError("publish_result must be a mapping")
        return await asyncio.to_thread(
            self._record_published_listing_sync,
            handoff_id,
            dict(publish_result),
            _optional_text(kwork_id),
            _optional_text(draft_hash),
            _timestamp(now),
        )

    async def list_published_listings(self, job_id: str, *, limit: int = 100) -> list[JsonDict]:
        """Return durable feedback links grouped by the original market job."""

        return await asyncio.to_thread(self._list_published_listings_sync, job_id, limit)

    async def list_listing_metrics_inputs(self, job_id: str) -> list[JsonDict]:
        """Return all normalized listing inputs needed for server-side metrics.

        The UI remains paginated; this bounded-by-job query is only used by the
        results projector and preserves one deterministic first-observed shard
        attribution per deduplicated listing.
        """

        return await asyncio.to_thread(self._list_listing_metrics_inputs_sync, job_id)

    async def load_export_snapshot(self, job_id: str) -> JsonDict:
        """Return one consistent, durable projection used by the local exporter.

        This intentionally bypasses UI pagination: a completed job may export up
        to the configured 10,000-card target, while API views remain paginated.
        """

        return await asyncio.to_thread(self._load_export_snapshot_sync, job_id)

    async def create_checkpoint(
        self,
        job_id: str,
        *,
        frontier: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        set_latest: bool = True,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Persist a checkpoint, optionally retaining the prior result checkpoint."""

        return await asyncio.to_thread(
            self._create_checkpoint_sync,
            job_id,
            frontier or {},
            metrics,
            set_latest,
            _timestamp(now),
        )

    async def get_checkpoint(self, checkpoint_id: str) -> JsonDict | None:
        """Return one durable checkpoint."""

        return await asyncio.to_thread(self._get_checkpoint_sync, checkpoint_id)

    async def list_checkpoints(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List checkpoints newest-first after a checkpoint-ID cursor."""

        return await asyncio.to_thread(self._list_checkpoints_sync, job_id, cursor, limit)

    async def enqueue_worker_command(
        self,
        command_type: WorkerCommandKind | str,
        *,
        job_id: str | None = None,
        worker_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        command_id: str | None = None,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Queue a durable control command for a worker or a job."""

        return await asyncio.to_thread(
            self._enqueue_worker_command_sync,
            _enum_value(command_type),
            job_id,
            worker_id,
            payload or {},
            command_id,
            _timestamp(now),
        )

    async def enqueue_command(
        self,
        command_type: WorkerCommandKind | str,
        **command: Any,
    ) -> JsonDict:
        """Compatibility spelling for :meth:`enqueue_worker_command`."""

        return await self.enqueue_worker_command(command_type, **command)

    async def get_worker_command(self, command_id: str) -> JsonDict | None:
        """Return one durable worker command."""

        return await asyncio.to_thread(self._get_worker_command_sync, command_id)

    async def list_worker_commands(
        self,
        *,
        job_id: str | None = None,
        worker_id: str | None = None,
        state: CommandState | str | Iterable[CommandState | str] | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List durable worker commands after a command-ID cursor."""

        return await asyncio.to_thread(
            self._list_worker_commands_sync,
            job_id,
            worker_id,
            state,
            cursor,
            limit,
        )

    async def update_worker_command(
        self,
        command_id: str,
        state: CommandState | str,
        *,
        error: str | None | object = _UNSET,
        now: str | datetime | None = None,
    ) -> JsonDict:
        """Advance a command state and persist acknowledgement/completion times."""

        return await asyncio.to_thread(
            self._update_worker_command_sync,
            command_id,
            _enum_value(state),
            error,
            _timestamp(now),
        )

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._migrate_schema(connection)
            self._initialized = True

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """Apply additive migrations for local databases created by earlier builds."""

        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_jobs)")}
        if "account_registration_ids_json" not in job_columns:
            connection.execute(
                "ALTER TABLE market_jobs ADD COLUMN account_registration_ids_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "job_kind" not in job_columns:
            connection.execute("ALTER TABLE market_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'supply'")
        if "config_json" not in job_columns:
            connection.execute("ALTER TABLE market_jobs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")
        operation_columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_operations)")}
        if "lease_fence" not in operation_columns:
            connection.execute("ALTER TABLE market_operations ADD COLUMN lease_fence INTEGER NOT NULL DEFAULT 0")
        if "leased_attempt_id" not in operation_columns:
            connection.execute("ALTER TABLE market_operations ADD COLUMN leased_attempt_id TEXT")
        worker_columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_workers)")}
        if "job_id" not in worker_columns:
            connection.execute(
                "ALTER TABLE market_workers ADD COLUMN job_id TEXT REFERENCES market_jobs(job_id) ON DELETE SET NULL"
            )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_market_workers_job ON market_workers(job_id, worker_id)")
        transport_columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_transports)")}
        if "egress_ip" not in transport_columns:
            connection.execute("ALTER TABLE market_transports ADD COLUMN egress_ip TEXT")
        if "egress_checked_at" not in transport_columns:
            connection.execute("ALTER TABLE market_transports ADD COLUMN egress_checked_at TEXT")
        handoff_columns = {row["name"] for row in connection.execute("PRAGMA table_info(market_draft_handoffs)")}
        if "generator_request_json" not in handoff_columns:
            connection.execute(
                "ALTER TABLE market_draft_handoffs ADD COLUMN generator_request_json TEXT NOT NULL DEFAULT '{}'"
            )
        listing_feature_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(market_listing_features)")
        }
        if "resolved_seller_key" not in listing_feature_columns:
            connection.execute("ALTER TABLE market_listing_features ADD COLUMN resolved_seller_key TEXT")
        MarketJobRepository._backfill_listing_projection(connection)

    @staticmethod
    def _backfill_listing_projection(connection: sqlite3.Connection) -> None:
        """Populate legacy listing summaries from their durable raw card payload."""

        rows = connection.execute(
            """
            SELECT listing_id, title, seller_key, canonical_json
            FROM market_listings
            WHERE COALESCE(TRIM(title), '') = '' OR COALESCE(TRIM(seller_key), '') = ''
            """
        ).fetchall()
        for row in rows:
            canonical = _load_json(row["canonical_json"], {})
            if not isinstance(canonical, Mapping):
                continue
            title = _listing_title(canonical) or _optional_text(row["title"])
            seller_key = MarketJobRepository._seller_key(canonical) or _optional_text(row["seller_key"])
            if title == row["title"] and seller_key == row["seller_key"]:
                continue
            connection.execute(
                "UPDATE market_listings SET title = ?, seller_key = ? WHERE listing_id = ?",
                (title, seller_key, int(row["listing_id"])),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL").fetchone()
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _create_job_sync(self, create: MarketJobCreate, job_id: str | None) -> JsonDict:
        self._ensure_initialized()
        identifier = job_id or _new_identifier("job")
        now = utc_now()
        scope = create.scope
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO market_jobs (
                        job_id, category_id, category_name, classifier_id, classifier_name, canonical_alias,
                        scope_filters_json, profile, target_unique_cards, desired_workers, job_kind, config_json, network_policy,
                        account_registration_ids_json, source_policy, include_ai, state, phase, revision, request_budget, time_budget_seconds,
                        counters_json, created_at, started_at, finished_at, latest_checkpoint_id, last_error,
                        last_warning, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '{}', ?, NULL, NULL, NULL, NULL, NULL, ?)
                    """,
                    (
                        identifier,
                        scope.category_id,
                        scope.category_name,
                        scope.classifier_id,
                        scope.classifier_name,
                        scope.canonical_alias,
                        _dump_json(scope.filters),
                        create.profile,
                        create.target_unique_cards,
                        create.desired_workers,
                        _enum_value(create.job_kind),
                        _dump_json(create.workflow_config),
                        _enum_value(create.network_policy),
                        _dump_json(list(create.account_registration_ids)),
                        _enum_value(create.source_policy),
                        int(create.include_ai),
                        JobState.PREPARING.value,
                        JobPhase.PREPARE.value,
                        create.request_budget,
                        create.time_budget_seconds,
                        now,
                        now,
                    ),
                )
                row = self._require_job_row(connection, identifier)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._job_record(row)

    def _get_job_sync(self, job_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM market_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_record(row) if row else None

    def _list_jobs_sync(
        self,
        states: Iterable[JobState | str] | None,
        limit: int,
        offset: int,
    ) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, offset)
        values = tuple(str(_enum_value(state)) for state in states) if states is not None else ()
        query = "SELECT * FROM market_jobs"
        parameters: list[Any] = []
        if values:
            query += f" WHERE state IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        query += " ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._job_record(row) for row in rows]

    def _update_job_state_sync(
        self,
        job_id: str,
        state: str,
        phase: str | None,
        expected_revision: int | None,
        counters: Mapping[str, int | float] | None,
        last_error: str | None | object,
        last_warning: str | None | object,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._require_job_row(connection, job_id)
                if expected_revision is not None and current["revision"] != expected_revision:
                    raise MarketJobRevisionConflictError(
                        f"job {job_id} has revision {current['revision']}, expected {expected_revision}"
                    )
                merged_counters = self._merge_counter_values(_load_json(current["counters_json"], {}), counters)
                updates = ["state = ?", "revision = ?", "counters_json = ?", "updated_at = ?"]
                parameters: list[Any] = [state, int(current["revision"]) + 1, _dump_json(merged_counters), now]
                if phase is not None:
                    updates.append("phase = ?")
                    parameters.append(phase)
                if current["started_at"] is None and state not in {JobState.PREPARING.value, JobState.PAUSED.value}:
                    updates.append("started_at = ?")
                    parameters.append(now)
                if state in _TERMINAL_JOB_STATES and current["finished_at"] is None:
                    updates.append("finished_at = ?")
                    parameters.append(now)
                if last_error is not _UNSET:
                    updates.append("last_error = ?")
                    parameters.append(last_error)
                if last_warning is not _UNSET:
                    updates.append("last_warning = ?")
                    parameters.append(last_warning)
                parameters.append(job_id)
                connection.execute(f"UPDATE market_jobs SET {', '.join(updates)} WHERE job_id = ?", parameters)
                row = self._require_job_row(connection, job_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._job_record(row)

    def _update_job_configuration_sync(
        self,
        job_id: str,
        desired_workers: int | None,
        target_unique_cards: int | None,
        profile: str | None,
        request_budget: int | None | object,
        time_budget_seconds: int | None | object,
        expected_revision: int | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        if (
            desired_workers is None
            and target_unique_cards is None
            and profile is None
            and request_budget is _UNSET
            and time_budget_seconds is _UNSET
        ):
            raise ValueError("at least one job configuration value is required")
        if desired_workers is not None and (
            isinstance(desired_workers, bool) or not isinstance(desired_workers, int) or desired_workers < 1
        ):
            raise ValueError("desired_workers must be a positive integer")
        if target_unique_cards is not None and (
            isinstance(target_unique_cards, bool)
            or not isinstance(target_unique_cards, int)
            or not 1 <= target_unique_cards <= 10_000
        ):
            raise ValueError("target_unique_cards must be between 1 and 10000")
        normalized_profile = _optional_text(profile) if profile is not None else None
        if profile is not None and normalized_profile is None:
            raise ValueError("profile is required when it is updated")
        for name, value in (("request_budget", request_budget), ("time_budget_seconds", time_budget_seconds)):
            if value is _UNSET or value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer or None")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._require_job_row(connection, job_id)
                if expected_revision is not None and current["revision"] != expected_revision:
                    raise MarketJobRevisionConflictError(
                        f"job {job_id} has revision {current['revision']}, expected {expected_revision}"
                    )
                updates = ["revision = ?", "updated_at = ?"]
                parameters: list[Any] = [int(current["revision"]) + 1, now]
                if desired_workers is not None:
                    updates.append("desired_workers = ?")
                    parameters.append(desired_workers)
                if target_unique_cards is not None:
                    updates.append("target_unique_cards = ?")
                    parameters.append(target_unique_cards)
                if normalized_profile is not None:
                    updates.append("profile = ?")
                    parameters.append(normalized_profile)
                if request_budget is not _UNSET:
                    updates.append("request_budget = ?")
                    parameters.append(request_budget)
                if time_budget_seconds is not _UNSET:
                    updates.append("time_budget_seconds = ?")
                    parameters.append(time_budget_seconds)
                parameters.append(job_id)
                connection.execute(f"UPDATE market_jobs SET {', '.join(updates)} WHERE job_id = ?", parameters)
                row = self._require_job_row(connection, job_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._job_record(row)

    def _create_shard_sync(self, shard: ShardSpec | Mapping[str, Any]) -> JsonDict:
        self._ensure_initialized()
        values = self._shard_values(shard)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, values["job_id"])
                connection.execute(
                    """
                    INSERT INTO market_shards (
                        shard_id, job_id, source, alias, filters_json, expected_count, state, priority,
                        cursor_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["shard_id"],
                        values["job_id"],
                        values["source"],
                        values["alias"],
                        _dump_json(values["filters"]),
                        values["expected_count"],
                        values["state"],
                        values["priority"],
                        _dump_json(values["cursor"]) if values["cursor"] is not None else None,
                        now,
                        now,
                    ),
                )
                row = self._require_shard_row(connection, values["shard_id"])
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._shard_record(row)

    def _get_shard_sync(self, shard_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM market_shards WHERE shard_id = ?", (shard_id,)).fetchone()
        return self._shard_record(row) if row else None

    def _upsert_category_alias_sync(
        self,
        category_id: int,
        canonical_alias: str,
        validation_status: str,
        active_category_id: int | None,
        source_url: str | None,
        last_validated_at: str | None,
        protection_state: str | None,
        schema_version: int,
    ) -> JsonDict:
        self._ensure_initialized()
        alias = _optional_text(canonical_alias)
        status = _optional_text(validation_status)
        if category_id <= 0:
            raise ValueError("category_id must be positive")
        if alias is None or status is None:
            raise ValueError("canonical_alias and validation_status are required")
        if schema_version <= 0:
            raise ValueError("schema_version must be positive")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_category_aliases (
                    category_id, canonical_alias, validation_status, active_category_id, source_url,
                    last_validated_at, protection_state, schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category_id, canonical_alias) DO UPDATE SET
                    validation_status = excluded.validation_status,
                    active_category_id = excluded.active_category_id,
                    source_url = excluded.source_url,
                    last_validated_at = excluded.last_validated_at,
                    protection_state = excluded.protection_state,
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at
                """,
                (
                    category_id,
                    alias,
                    status,
                    active_category_id,
                    source_url,
                    last_validated_at,
                    protection_state,
                    schema_version,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM market_category_aliases
                WHERE category_id = ? AND canonical_alias = ?
                """,
                (category_id, alias),
            ).fetchone()
        return self._category_alias_record(row)

    def _get_category_alias_sync(self, category_id: int, canonical_alias: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM market_category_aliases
                WHERE category_id = ? AND canonical_alias = ?
                """,
                (category_id, canonical_alias),
            ).fetchone()
        return self._category_alias_record(row) if row else None

    def _list_shards_sync(self, job_id: str, limit: int, offset: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, offset)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_shards
                WHERE job_id = ?
                ORDER BY priority DESC, created_at ASC, shard_id ASC
                LIMIT ? OFFSET ?
                """,
                (job_id, limit, offset),
            ).fetchall()
        return [self._shard_record(row) for row in rows]

    def _enqueue_operation_sync(self, operation: Operation | Mapping[str, Any]) -> JsonDict:
        self._ensure_initialized()
        values = self._operation_values(operation)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._enqueue_operation_locked(connection, values, now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._operation_record(row)

    def _get_operation_sync(self, operation_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._operation_record(row) if row else None

    def _list_operation_attempts_sync(self, operation_id: str, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        with self._connect() as connection:
            self._require_operation_row(connection, operation_id)
            rows = connection.execute(
                """
                SELECT * FROM market_operation_attempts
                WHERE operation_id = ?
                ORDER BY attempt_number ASC
                LIMIT ?
                """,
                (operation_id, limit),
            ).fetchall()
        return [self._attempt_record(row) for row in rows]

    def _get_job_concurrency_signals_sync(self, job_id: str, window: int) -> JsonDict:
        self._ensure_initialized()
        self._validate_pagination(window, 0)
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT
                    attempts.state,
                    attempts.failure_kind,
                    attempts.response_status,
                    attempts.received_count,
                    attempts.new_unique_count
                FROM market_operation_attempts AS attempts
                JOIN market_operations AS operations ON operations.operation_id = attempts.operation_id
                WHERE operations.job_id = ?
                ORDER BY attempts.started_at DESC, attempts.attempt_number DESC, attempts.attempt_id DESC
                LIMIT ?
                """,
                (job_id, window),
            ).fetchall()
        successes = 0
        http_403 = 0
        http_429 = 0
        timeouts = 0
        received = 0
        new_unique = 0
        for row in rows:
            state = str(row["state"])
            failure_kind = str(row["failure_kind"] or "")
            status_code = row["response_status"]
            successes += int(state == OperationState.SUCCEEDED.value)
            http_403 += int(status_code == 403 or failure_kind == "protection")
            http_429 += int(status_code == 429 or failure_kind == "rate_limited")
            timeouts += int(failure_kind == "timeout")
            received += int(row["received_count"])
            new_unique += int(row["new_unique_count"])
        return {
            "window_attempt_count": len(rows),
            "success_count": successes,
            "http_403_count": http_403,
            "http_429_count": http_429,
            "timeout_count": timeouts,
            "received_count": received,
            "new_unique_count": new_unique,
            "novelty_rate": new_unique / received if received else None,
        }

    def _list_operations_sync(
        self,
        job_id: str,
        state: OperationState | str | Iterable[OperationState | str] | None,
        cursor: str | None,
        limit: int,
    ) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        states = self._state_filter_values(state)
        after = self._cursor_text(cursor)
        query = "SELECT * FROM market_operations WHERE job_id = ?"
        parameters: list[Any] = [job_id]
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            parameters.extend(states)
        if after is not None:
            query += " AND operation_id > ?"
            parameters.append(after)
        query += " ORDER BY operation_id ASC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._operation_record(row) for row in rows]

    def _find_active_operation_sync(
        self,
        job_id: str,
        kinds: tuple[OperationKind | str, ...],
    ) -> JsonDict | None:
        self._ensure_initialized()
        normalized_kinds = tuple(dict.fromkeys(_enum_value(kind) for kind in kinds))
        if not normalized_kinds:
            return None
        states = (
            OperationState.QUEUED.value,
            OperationState.LEASED.value,
            OperationState.RUNNING.value,
            OperationState.RETRY_WAIT.value,
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM market_operations
                WHERE job_id = ?
                  AND kind IN ({",".join("?" for _ in normalized_kinds)})
                  AND state IN ({",".join("?" for _ in states)})
                ORDER BY priority DESC, created_at ASC, operation_id ASC
                LIMIT 1
                """,
                (job_id, *normalized_kinds, *states),
            ).fetchone()
        return self._operation_record(row) if row is not None else None

    def _list_unresolved_terminal_enrichment_operations_sync(self, job_id: str, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        terminal_states = (
            OperationState.FAILED.value,
            OperationState.CONTRACT_VIOLATION.value,
            OperationState.BLOCKED.value,
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT operations.*
                FROM market_operations AS operations
                WHERE operations.job_id = ?
                  AND operations.kind = ?
                  AND operations.state IN ({",".join("?" for _ in terminal_states)})
                  AND NOT EXISTS (
                      SELECT 1
                      FROM market_operations AS retries
                      WHERE retries.job_id = operations.job_id
                        AND json_extract(retries.payload_json, '$.retry_of') = operations.operation_id
                  )
                ORDER BY operations.operation_id ASC
                LIMIT ?
                """,
                (job_id, OperationKind.ENRICH_LISTING.value, *terminal_states, limit),
            ).fetchall()
        return [self._operation_record(row) for row in rows]

    def _lease_operation_sync(
        self,
        worker_id: str,
        job_id: str | None,
        transport_id: str | None,
        lease_seconds: int,
        now: str,
    ) -> JsonDict | None:
        self._ensure_initialized()
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        deadline = _lease_deadline(now, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._recover_expired_operations_locked(connection, now)
                query = f"""
                    SELECT operations.* FROM market_operations AS operations
                    JOIN market_jobs AS jobs ON jobs.job_id = operations.job_id
                    WHERE operations.state IN (?, ?)
                      AND (operations.not_before IS NULL OR operations.not_before <= ?)
                      AND jobs.state NOT IN ({",".join("?" for _ in _NON_LEASABLE_JOB_STATES)})
                """
                parameters: list[Any] = [
                    OperationState.QUEUED.value,
                    OperationState.RETRY_WAIT.value,
                    now,
                    *_NON_LEASABLE_JOB_STATES,
                ]
                if job_id is not None:
                    query += " AND operations.job_id = ?"
                    parameters.append(job_id)
                query += " ORDER BY priority DESC, created_at ASC, operation_id ASC LIMIT 1"
                operation = connection.execute(query, parameters).fetchone()
                if operation is None:
                    connection.execute("COMMIT")
                    return None
                attempt_number = int(operation["current_attempt"]) + 1
                attempt_id = _new_identifier("attempt")
                connection.execute(
                    """
                    UPDATE market_operations
                    SET state = ?, lease_owner = ?, lease_deadline = ?, lease_fence = lease_fence + 1,
                        leased_attempt_id = ?, current_attempt = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (
                        OperationState.LEASED.value,
                        worker_id,
                        deadline,
                        attempt_id,
                        attempt_number,
                        now,
                        operation["operation_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO market_operation_attempts (
                        attempt_id, operation_id, attempt_number, state, worker_id, transport_id, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        operation["operation_id"],
                        attempt_number,
                        OperationState.LEASED.value,
                        worker_id,
                        transport_id,
                        now,
                    ),
                )
                worker = connection.execute(
                    "SELECT job_id FROM market_workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                if worker is not None and worker["job_id"] is not None and worker["job_id"] != operation["job_id"]:
                    raise MarketOperationLeaseError(
                        f"worker {worker_id} is bound to job {worker['job_id']}, not {operation['job_id']}"
                    )
                connection.execute(
                    """
                    UPDATE market_workers
                    SET job_id = ?, current_operation_id = ?, heartbeat_at = ?, updated_at = ?
                    WHERE worker_id = ?
                    """,
                    (operation["job_id"], operation["operation_id"], now, now, worker_id),
                )
                leased = self._require_operation_row(connection, operation["operation_id"])
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        result = self._operation_record(leased)
        result["attempt_id"] = attempt_id
        return result

    def _renew_operation_lease_sync(
        self,
        operation_id: str,
        worker_id: str,
        lease_seconds: int,
        attempt_id: str | None,
        lease_fence: int | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        deadline = _lease_deadline(now, lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """
                    UPDATE market_operations
                    SET lease_deadline = ?, updated_at = ?
                    WHERE operation_id = ? AND lease_owner = ? AND state IN (?, ?)
                      AND (? IS NULL OR leased_attempt_id = ?)
                      AND (? IS NULL OR lease_fence = ?)
                    """,
                    (
                        deadline,
                        now,
                        operation_id,
                        worker_id,
                        OperationState.LEASED.value,
                        OperationState.RUNNING.value,
                        attempt_id,
                        attempt_id,
                        lease_fence,
                        lease_fence,
                    ),
                ).rowcount
                if not updated:
                    raise MarketOperationNotFoundError(f"active lease not found for operation {operation_id}")
                row = self._require_operation_row(connection, operation_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._operation_record(row)

    def _complete_operation_sync(
        self,
        operation_id: str,
        worker_id: str,
        attempt_id: str | None,
        lease_fence: int | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        if not _optional_text(worker_id):
            raise ValueError("worker_id is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation = self._require_operation_row(connection, operation_id)
                self._require_lease_owner(
                    operation,
                    worker_id,
                    now,
                    attempt_id=attempt_id,
                    lease_fence=lease_fence,
                )
                connection.execute(
                    """
                    UPDATE market_operations
                    SET state = ?, lease_owner = NULL, lease_deadline = NULL, leased_attempt_id = NULL, completed_at = ?,
                        updated_at = ?, last_error = NULL
                    WHERE operation_id = ?
                    """,
                    (OperationState.SUCCEEDED.value, now, now, operation_id),
                )
                attempt_id = self._latest_attempt_id(connection, operation_id)
                if attempt_id is not None:
                    connection.execute(
                        """
                        UPDATE market_operation_attempts
                        SET state = ?, finished_at = ?, error = NULL
                        WHERE attempt_id = ?
                        """,
                        (OperationState.SUCCEEDED.value, now, attempt_id),
                    )
                self._clear_worker_operation_locked(connection, worker_id, operation_id, now)
                row = self._require_operation_row(connection, operation_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._operation_record(row)

    def _fail_operation_sync(
        self,
        operation_id: str,
        worker_id: str,
        error: str,
        retry_at: str | None,
        failure_kind: str | None,
        attempt_id: str | None,
        lease_fence: int | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        if not _optional_text(worker_id):
            raise ValueError("worker_id is required")
        state = OperationState.RETRY_WAIT.value if retry_at is not None else OperationState.FAILED.value
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation = self._require_operation_row(connection, operation_id)
                self._require_lease_owner(
                    operation,
                    worker_id,
                    now,
                    attempt_id=attempt_id,
                    lease_fence=lease_fence,
                )
                connection.execute(
                    """
                    UPDATE market_operations
                    SET state = ?, not_before = ?, lease_owner = NULL, lease_deadline = NULL, leased_attempt_id = NULL,
                        completed_at = ?, updated_at = ?, last_error = ?
                    WHERE operation_id = ?
                    """,
                    (
                        state,
                        retry_at,
                        now if state == OperationState.FAILED.value else None,
                        now,
                        error,
                        operation_id,
                    ),
                )
                attempt_id = self._latest_attempt_id(connection, operation_id)
                if attempt_id is not None:
                    connection.execute(
                        """
                        UPDATE market_operation_attempts
                        SET state = ?, failure_kind = ?, retry_after = ?, finished_at = ?, error = ?
                        WHERE attempt_id = ?
                        """,
                        (OperationState.FAILED.value, failure_kind, retry_at, now, error, attempt_id),
                    )
                self._clear_worker_operation_locked(connection, worker_id, operation_id, now)
                row = self._require_operation_row(connection, operation_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._operation_record(row)

    def _recover_expired_operations_sync(self, now: str) -> int:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                recovered = self._recover_expired_operations_locked(connection, now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return recovered

    def _cancel_collection_operations_sync(self, job_id: str, reason: str, now: str) -> int:
        self._ensure_initialized()
        collection_kinds = (
            OperationKind.MAP_SCOPE.value,
            OperationKind.RESOLVE_ALIAS.value,
            OperationKind.FETCH_BATCH.value,
            OperationKind.ENRICH_LISTING.value,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job_id)
                updated = connection.execute(
                    f"""
                    UPDATE market_operations
                    SET state = ?, completed_at = ?, updated_at = ?, last_error = ?
                    WHERE job_id = ?
                      AND kind IN ({",".join("?" for _ in collection_kinds)})
                      AND state IN (?, ?)
                    """,
                    (
                        OperationState.CANCELLED.value,
                        now,
                        now,
                        reason,
                        job_id,
                        *collection_kinds,
                        OperationState.QUEUED.value,
                        OperationState.RETRY_WAIT.value,
                    ),
                ).rowcount
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return updated

    def _append_event_sync(
        self,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        revision: int | None,
        worker_id: str | None,
        operation_id: str | None,
        emitted_at: str,
    ) -> JsonDict:
        self._ensure_initialized()
        if not event_type.strip():
            raise ValueError("event_type is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job_id)
                row = self._append_event_locked(
                    connection,
                    job_id=job_id,
                    event_type=event_type,
                    payload=payload,
                    revision=revision,
                    worker_id=worker_id,
                    operation_id=operation_id,
                    emitted_at=emitted_at,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._event_record(row)

    def _replay_events_sync(self, job_id: str, after_sequence: int, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        self._validate_pagination(limit, 0)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_events
                WHERE job_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (job_id, after_sequence, limit),
            ).fetchall()
        return [self._event_record(row) for row in rows]

    def _list_recent_events_sync(self, job_id: str, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT * FROM market_events
                WHERE job_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [self._event_record(row) for row in reversed(rows)]

    def _get_request_concurrency_summary_sync(self, job_id: str) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            job = self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT attempts.started_at, attempts.finished_at, attempts.worker_id, attempts.transport_id,
                       bindings.registration_id, bindings.current_egress_ip
                FROM market_operation_attempts AS attempts
                JOIN market_operations AS operations ON operations.operation_id = attempts.operation_id
                LEFT JOIN market_worker_identity_bindings AS bindings ON bindings.worker_id = attempts.worker_id
                WHERE operations.job_id = ? AND operations.kind = ?
                ORDER BY attempts.started_at ASC, attempts.attempt_id ASC
                """,
                (job_id, OperationKind.FETCH_BATCH.value),
            ).fetchall()
            peak_row = connection.execute(
                """
                SELECT MAX(CAST(json_extract(payload_json, '$.peak_requests') AS INTEGER)) AS peak_requests
                FROM market_events
                WHERE job_id = ? AND event_type = 'request.started'
                """,
                (job_id,),
            ).fetchone()

        timeline: list[tuple[datetime, int]] = []
        workers: set[str] = set()
        transports: set[str] = set()
        accounts: set[str] = set()
        egress_ips: set[str] = set()
        first_started: datetime | None = None
        last_finished: datetime | None = None
        for row in rows:
            try:
                started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            try:
                finished = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                finished = started
            if finished < started:
                finished = started
            timeline.extend(((started, 1), (finished, -1)))
            first_started = started if first_started is None or started < first_started else first_started
            last_finished = finished if last_finished is None or finished > last_finished else last_finished
            for target, value in (
                (workers, row["worker_id"]),
                (transports, row["transport_id"]),
                (accounts, row["registration_id"]),
                (egress_ips, row["current_egress_ip"]),
            ):
                if value:
                    target.add(str(value))

        active = 0
        peak = 0
        for _moment, delta in sorted(timeline, key=lambda item: (item[0], item[1])):
            active = max(0, active + delta)
            peak = max(peak, active)
        if peak_row is not None and peak_row["peak_requests"] is not None:
            peak = max(peak, int(peak_row["peak_requests"]))
        duration_seconds = (
            max(0, int((last_finished - first_started).total_seconds()))
            if first_started is not None and last_finished is not None
            else 0
        )
        return {
            "configured_workers": int(job["desired_workers"]),
            "fetch_attempt_count": len(rows),
            "peak_parallel_requests": peak,
            "distinct_workers": len(workers),
            "distinct_transports": len(transports),
            "distinct_accounts": len(accounts),
            "distinct_egress_ips": len(egress_ips),
            "first_request_at": _timestamp(first_started) if first_started is not None else None,
            "last_request_at": _timestamp(last_finished) if last_finished is not None else None,
            "collection_request_span_seconds": duration_seconds,
        }

    def _get_event_sequence_bounds_sync(self, job_id: str) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            row = connection.execute(
                """
                SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence
                FROM market_events
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        first = row["first_sequence"] if row is not None else None
        last = row["last_sequence"] if row is not None else None
        return {
            "first_sequence": int(first) if first is not None else None,
            "last_sequence": int(last) if last is not None else None,
        }

    def _commit_accepted_batch_sync(
        self,
        job_id: str,
        shard_id: str,
        operation_id: str,
        listings: tuple[Mapping[str, Any], ...],
        source: str | None,
        idempotency_key: str,
        attempt_id: str | None,
        worker_id: str | None,
        lease_fence: int | None,
        requested_cursor: Mapping[str, Any] | None,
        reported_cursor: Mapping[str, Any] | None,
        next_cursor: Mapping[str, Any] | None,
        raw_response_ref: str | None,
        fingerprint: str | None,
        counter_deltas: Mapping[str, int | float] | None,
        event_payloads: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
        next_operation: Mapping[str, Any] | None,
        shard_state: str,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = connection.execute(
                    """
                    SELECT * FROM market_batch_commits
                    WHERE job_id = ? AND (idempotency_key = ? OR operation_id = ?)
                    LIMIT 1
                    """,
                    (job_id, idempotency_key, operation_id),
                ).fetchone()
                if replay is not None:
                    if replay["operation_id"] != operation_id:
                        raise MarketCommitConflictError(
                            f"idempotency key {idempotency_key!r} already belongs to {replay['operation_id']}"
                        )
                    result = _load_json(replay["result_json"], {})
                    result["idempotent_replay"] = True
                    connection.execute("COMMIT")
                    return result

                job = self._require_job_row(connection, job_id)
                shard = self._require_shard_row(connection, shard_id)
                if shard["job_id"] != job_id:
                    raise MarketCommitConflictError(f"shard {shard_id} does not belong to job {job_id}")
                operation = self._require_operation_row(connection, operation_id)
                if operation["job_id"] != job_id or operation["shard_id"] != shard_id:
                    raise MarketCommitConflictError(
                        f"operation {operation_id} does not belong to the supplied job/shard"
                    )

                effective_source = source or shard["source"]
                effective_attempt_id = attempt_id or operation["leased_attempt_id"] or self._latest_attempt_id(connection, operation_id)
                if worker_id is not None:
                    self._require_lease_owner(
                        operation,
                        worker_id,
                        now,
                        attempt_id=effective_attempt_id,
                        lease_fence=lease_fence,
                    )
                new_listings = 0
                observations = 0
                requested_json = _dump_json(requested_cursor) if requested_cursor is not None else None
                reported_json = _dump_json(reported_cursor) if reported_cursor is not None else None

                for position, listing in enumerate(listings):
                    listing_key = _listing_key(listing)
                    existing = connection.execute(
                        "SELECT listing_id FROM market_listings WHERE job_id = ? AND listing_key = ?",
                        (job_id, listing_key),
                    ).fetchone()
                    if existing is None:
                        cursor = connection.execute(
                            """
                            INSERT INTO market_listings (
                                job_id, listing_key, title, seller_key, price, canonical_json,
                                first_seen_at, last_seen_at, observation_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """,
                            (
                                job_id,
                                listing_key,
                                _listing_title(listing),
                                self._seller_key(listing),
                                _optional_number(listing.get("price")),
                                _dump_json(listing),
                                now,
                                now,
                            ),
                        )
                        listing_id = int(cursor.lastrowid)
                        new_listings += 1
                    else:
                        listing_id = int(existing["listing_id"])
                        connection.execute(
                            """
                            UPDATE market_listings
                            SET title = ?, seller_key = ?, price = ?, canonical_json = ?, last_seen_at = ?
                            WHERE listing_id = ?
                            """,
                            (
                                _listing_title(listing),
                                self._seller_key(listing),
                                _optional_number(listing.get("price")),
                                _dump_json(listing),
                                now,
                                listing_id,
                            ),
                        )
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO market_listing_observations (
                            job_id, listing_id, operation_id, attempt_id, source, shard_id, response_position,
                            requested_cursor_json, reported_cursor_json, raw_response_ref, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            listing_id,
                            operation_id,
                            effective_attempt_id,
                            effective_source,
                            shard_id,
                            position,
                            requested_json,
                            reported_json,
                            raw_response_ref,
                            now,
                        ),
                    ).rowcount
                    if inserted:
                        observations += 1
                        connection.execute(
                            "UPDATE market_listings SET observation_count = observation_count + 1 WHERE listing_id = ?",
                            (listing_id,),
                        )

                duplicate_count = max(observations - new_listings, 0)
                zero_novelty = 1 if observations and new_listings == 0 else 0
                connection.execute(
                    """
                    UPDATE market_shards
                    SET state = ?, cursor_json = ?, last_fingerprint = ?, received_count = received_count + ?,
                        new_unique_count = new_unique_count + ?, duplicate_count = duplicate_count + ?,
                        consecutive_zero_novelty = CASE WHEN ? = 1 THEN consecutive_zero_novelty + 1 ELSE 0 END,
                        updated_at = ?
                    WHERE shard_id = ?
                    """,
                    (
                        shard_state,
                        _dump_json(next_cursor) if next_cursor is not None else shard["cursor_json"],
                        fingerprint,
                        observations,
                        new_listings,
                        duplicate_count,
                        zero_novelty,
                        now,
                        shard_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE market_operations
                    SET state = ?, lease_owner = NULL, lease_deadline = NULL, leased_attempt_id = NULL,
                        completed_at = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (OperationState.SUCCEEDED.value, now, now, operation_id),
                )
                if effective_attempt_id is not None:
                    connection.execute(
                        """
                        UPDATE market_operation_attempts
                        SET state = ?, requested_cursor_json = ?, reported_cursor_json = ?, received_count = ?,
                            new_unique_count = ?, duplicate_count = ?, page_fingerprint = ?, raw_response_ref = ?,
                            finished_at = ?
                        WHERE attempt_id = ?
                        """,
                        (
                            OperationState.SUCCEEDED.value,
                            requested_json,
                            reported_json,
                            observations,
                            new_listings,
                            duplicate_count,
                            fingerprint,
                            raw_response_ref,
                            now,
                            effective_attempt_id,
                        ),
                    )

                totals = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM market_listings WHERE job_id = ?) AS unique_listings,
                        (SELECT COUNT(*) FROM market_listing_observations WHERE job_id = ?) AS observations
                    """,
                    (job_id, job_id),
                ).fetchone()
                target_reached = int(totals["unique_listings"]) >= int(job["target_unique_cards"])
                merged_counters = self._merge_counter_values(_load_json(job["counters_json"], {}), counter_deltas)
                merged_counters.update(
                    {
                        "accepted_batches": int(merged_counters.get("accepted_batches", 0)) + 1,
                        "unique_listings": int(totals["unique_listings"]),
                        "unique_cards": int(totals["unique_listings"]),
                        "listing_observations": int(totals["observations"]),
                        "card_occurrences": int(totals["observations"]),
                    }
                )
                revision = int(job["revision"]) + 1
                connection.execute(
                    "UPDATE market_jobs SET revision = ?, counters_json = ?, updated_at = ? WHERE job_id = ?",
                    (revision, _dump_json(merged_counters), now, job_id),
                )

                next_operation_id: str | None = None
                continuation_allowed = not target_reached and job["state"] not in _NON_LEASABLE_JOB_STATES
                if next_operation is not None and continuation_allowed:
                    values = self._operation_values(
                        {
                            **next_operation,
                            "operation_id": next_operation.get("operation_id") or _new_identifier("op"),
                            "job_id": next_operation.get("job_id") or job_id,
                            "shard_id": next_operation.get("shard_id") or shard_id,
                        }
                    )
                    next_row = self._enqueue_operation_locked(connection, values, now)
                    next_operation_id = str(next_row["operation_id"])

                effective_shard_state = shard_state
                if next_operation is not None and next_operation_id is None:
                    effective_shard_state = "exhausted"
                connection.execute(
                    "UPDATE market_shards SET state = ? WHERE shard_id = ?",
                    (effective_shard_state, shard_id),
                )
                event_rows = self._append_commit_events_locked(
                    connection,
                    job_id=job_id,
                    operation_id=operation_id,
                    revision=revision,
                    event_payloads=event_payloads,
                    default_payload={
                        "shard_id": shard_id,
                        "new_listings": new_listings,
                        "observations": observations,
                        "next_operation_id": next_operation_id,
                    },
                    emitted_at=now,
                )
                last_sequence = (
                    event_rows[-1]["sequence"] if event_rows else self._latest_event_sequence(connection, job_id)
                )
                checkpoint_id = _new_identifier("checkpoint")
                connection.execute(
                    """
                    INSERT INTO market_checkpoints (
                        checkpoint_id, job_id, revision, phase, frontier_json, metrics_json, last_event_sequence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        job_id,
                        revision,
                        job["phase"],
                        _dump_json({"shard_id": shard_id, "next_cursor": next_cursor}),
                        _dump_json(merged_counters),
                        last_sequence,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE market_jobs SET latest_checkpoint_id = ? WHERE job_id = ?",
                    (checkpoint_id, job_id),
                )
                result: JsonDict = {
                    "job_id": job_id,
                    "shard_id": shard_id,
                    "operation_id": operation_id,
                    "attempt_id": effective_attempt_id,
                    "idempotency_key": idempotency_key,
                    "idempotent_replay": False,
                    "new_listings": new_listings,
                    "observations": observations,
                    "revision": revision,
                    "counters": merged_counters,
                    "next_operation_id": next_operation_id,
                    "target_reached": target_reached,
                    "checkpoint_id": checkpoint_id,
                    "event_sequences": [int(event["sequence"]) for event in event_rows],
                }
                connection.execute(
                    """
                    INSERT INTO market_batch_commits (commit_id, job_id, operation_id, idempotency_key, result_json, committed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (_new_identifier("commit"), job_id, operation_id, idempotency_key, _dump_json(result), now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result

    def _upsert_worker_sync(self, worker: WorkerRecord | Mapping[str, Any], job_id: str | None) -> JsonDict:
        self._ensure_initialized()
        data = asdict(worker) if is_dataclass(worker) else dict(worker)
        worker_id = _optional_text(data.get("worker_id"))
        if worker_id is None:
            raise ValueError("worker_id is required")
        payload_job_id = _optional_text(data.get("job_id"))
        requested_job_id = _optional_text(job_id)
        if requested_job_id is not None and payload_job_id is not None and requested_job_id != payload_job_id:
            raise ValueError("worker job_id conflicts with the explicit job_id")
        bound_job_id = requested_job_id or payload_job_id
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if bound_job_id is not None:
                    self._require_job_row(connection, bound_job_id)
                connection.execute(
                    """
                    INSERT INTO market_workers (
                        worker_id, job_id, generation, desired_state, actual_state, runtime_kind, transport_id,
                        current_operation_id, heartbeat_at, counters_json, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        job_id = COALESCE(excluded.job_id, market_workers.job_id),
                        generation = excluded.generation,
                        desired_state = excluded.desired_state,
                        actual_state = excluded.actual_state,
                        runtime_kind = excluded.runtime_kind,
                        transport_id = excluded.transport_id,
                        current_operation_id = excluded.current_operation_id,
                        heartbeat_at = excluded.heartbeat_at,
                        counters_json = excluded.counters_json,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        worker_id,
                        bound_job_id,
                        int(data.get("generation", 1)),
                        _enum_value(data.get("desired_state", "running")),
                        _enum_value(data.get("actual_state", "idle")),
                        data.get("runtime_kind", "in_process"),
                        data.get("transport_id"),
                        data.get("current_operation_id"),
                        data.get("heartbeat_at"),
                        _dump_json(data.get("counters", {})),
                        data.get("last_error"),
                        now,
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM market_workers WHERE worker_id = ?", (worker_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._worker_record(row)

    def _upsert_transport_sync(self, transport: TransportSnapshot | Mapping[str, Any]) -> JsonDict:
        self._ensure_initialized()
        data = asdict(transport) if is_dataclass(transport) else dict(transport)
        transport_id = _optional_text(data.get("transport_id"))
        if transport_id is None:
            raise ValueError("transport_id is required")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_transports (
                    transport_id, kind, health, slot, proxy_url, profile_id, profile_name, country, pid,
                    generation, lease_owner, quarantine_until, last_rotate_reason, egress_ip,
                    egress_checked_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transport_id) DO UPDATE SET
                    kind = excluded.kind,
                    health = excluded.health,
                    slot = excluded.slot,
                    proxy_url = excluded.proxy_url,
                    profile_id = excluded.profile_id,
                    profile_name = excluded.profile_name,
                    country = excluded.country,
                    pid = excluded.pid,
                    generation = excluded.generation,
                    lease_owner = excluded.lease_owner,
                    quarantine_until = excluded.quarantine_until,
                    last_rotate_reason = excluded.last_rotate_reason,
                    egress_ip = excluded.egress_ip,
                    egress_checked_at = excluded.egress_checked_at,
                    updated_at = excluded.updated_at
                """,
                (
                    transport_id,
                    _enum_value(data.get("kind", "direct")),
                    _enum_value(data.get("health", "unknown")),
                    data.get("slot"),
                    data.get("proxy_url"),
                    data.get("profile_id"),
                    data.get("profile_name"),
                    data.get("country"),
                    data.get("pid"),
                    int(data.get("generation", 1)),
                    data.get("lease_owner"),
                    data.get("quarantine_until"),
                    data.get("last_rotate_reason"),
                    data.get("egress_ip"),
                    data.get("egress_checked_at"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM market_transports WHERE transport_id = ?", (transport_id,)
            ).fetchone()
        return self._transport_record(row)

    def _get_worker_sync(self, worker_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM market_workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._worker_record(row) if row else None

    def _list_workers_sync(self, job_id: str | None, cursor: str | None, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        after = self._cursor_text(cursor)
        query = "SELECT workers.* FROM market_workers AS workers WHERE 1 = 1"
        parameters: list[Any] = []
        if job_id is not None:
            query += " AND workers.job_id = ?"
            parameters.append(job_id)
        if after is not None:
            query += " AND workers.worker_id > ?"
            parameters.append(after)
        query += " ORDER BY workers.worker_id ASC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._worker_record(row) for row in rows]

    def _get_transport_sync(self, transport_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_transports WHERE transport_id = ?", (transport_id,)
            ).fetchone()
        return self._transport_record(row) if row else None

    def _list_transports_sync(self, job_id: str | None, cursor: str | None, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        after = self._cursor_text(cursor)
        query = """
            SELECT DISTINCT transports.*
            FROM market_transports AS transports
            LEFT JOIN market_workers AS workers ON workers.transport_id = transports.transport_id
            WHERE 1 = 1
        """
        parameters: list[Any] = []
        if job_id is not None:
            # A released worker no longer points at its route, but the route
            # still belongs to the job's available transport pool.
            query += " AND (workers.job_id = ? OR transports.lease_owner IS NULL)"
            parameters.append(job_id)
        if after is not None:
            query += " AND transports.transport_id > ?"
            parameters.append(after)
        query += " ORDER BY transports.transport_id ASC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._transport_record(row) for row in rows]

    def _sync_market_account_inventory_sync(self, accounts: Sequence[Mapping[str, Any]]) -> list[JsonDict]:
        self._ensure_initialized()
        normalized: list[JsonDict] = []
        for raw in accounts:
            registration_id = _optional_text(raw.get("registration_id"))
            username = _optional_text(raw.get("username"))
            email = _optional_text(raw.get("email"))
            status = _optional_text(raw.get("status"))
            if registration_id is None or username is None or email is None or status is None:
                continue
            raw_slot = raw.get("registration_slot")
            try:
                preferred_slot = int(raw_slot) if raw_slot is not None else None
            except (TypeError, ValueError):
                preferred_slot = None
            if preferred_slot is not None and preferred_slot < 1:
                preferred_slot = None
            normalized.append(
                {
                    "registration_id": registration_id,
                    "username": username,
                    "email": email,
                    "status": status,
                    "market_enabled": 1 if raw.get("market_enabled") is not False else 0,
                    "session_cookie_count": max(int(raw.get("session_cookie_count") or 0), 0),
                    "signup_ip": _optional_text(raw.get("signup_ip")),
                    "preferred_slot": preferred_slot,
                    "preferred_transport_id": _optional_text(raw.get("registration_transport_id"))
                    or (f"vpnte-slot-{preferred_slot}" if preferred_slot is not None else None),
                    "persona_id": _optional_text(raw.get("persona_id")),
                }
            )
        now = utc_now()
        identifiers = [item["registration_id"] for item in normalized]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if identifiers:
                    placeholders = ", ".join("?" for _ in identifiers)
                    connection.execute(
                        f"""
                        UPDATE market_account_inventory
                        SET status = 'missing_local', market_enabled = 0, updated_at = ?
                        WHERE registration_id NOT IN ({placeholders})
                        """,
                        (now, *identifiers),
                    )
                else:
                    connection.execute(
                        "UPDATE market_account_inventory SET status = 'missing_local', market_enabled = 0, updated_at = ?",
                        (now,),
                    )
                for item in normalized:
                    connection.execute(
                        """
                        INSERT INTO market_account_inventory (
                            registration_id, username, email, status, market_enabled, session_cookie_count,
                            signup_ip, preferred_slot, preferred_transport_id, persona_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(registration_id) DO UPDATE SET
                            username = excluded.username,
                            email = excluded.email,
                            status = excluded.status,
                            market_enabled = CASE
                                WHEN market_account_inventory.market_enabled = 0 THEN 0
                                ELSE excluded.market_enabled
                            END,
                            session_cookie_count = excluded.session_cookie_count,
                            signup_ip = excluded.signup_ip,
                            preferred_slot = excluded.preferred_slot,
                            preferred_transport_id = excluded.preferred_transport_id,
                            persona_id = excluded.persona_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            item["registration_id"],
                            item["username"],
                            item["email"],
                            item["status"],
                            item["market_enabled"],
                            item["session_cookie_count"],
                            item["signup_ip"],
                            item["preferred_slot"],
                            item["preferred_transport_id"],
                            item["persona_id"],
                            now,
                            now,
                        ),
                    )
                rows = connection.execute(
                    "SELECT * FROM market_account_inventory ORDER BY username COLLATE NOCASE, registration_id"
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [self._market_account_inventory_record(row) for row in rows]

    def _list_market_account_inventory_sync(self, enabled_only: bool) -> list[JsonDict]:
        self._ensure_initialized()
        query = "SELECT * FROM market_account_inventory"
        if enabled_only:
            query += " WHERE market_enabled = 1 AND status = 'activated' AND session_cookie_count > 0"
        query += " ORDER BY last_used_at IS NOT NULL, last_used_at, username COLLATE NOCASE, registration_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._market_account_inventory_record(row) for row in rows]

    def _set_market_account_enabled_sync(self, registration_id: str, enabled: bool) -> JsonDict:
        self._ensure_initialized()
        identifier = _optional_text(registration_id)
        if identifier is None:
            raise ValueError("registration_id is required")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM market_account_inventory WHERE registration_id = ?", (identifier,)
                ).fetchone()
                if current is None:
                    raise MarketJobRepositoryError(f"market account not found: {identifier}")
                if not enabled:
                    active = connection.execute(
                        "SELECT 1 FROM market_worker_identity_bindings WHERE registration_id = ? AND state = 'active' LIMIT 1",
                        (identifier,),
                    ).fetchone()
                    if active is not None:
                        raise MarketJobRepositoryError("cannot disable an account while it is leased by a worker")
                connection.execute(
                    "UPDATE market_account_inventory SET market_enabled = ?, updated_at = ? WHERE registration_id = ?",
                    (1 if enabled else 0, now, identifier),
                )
                row = connection.execute(
                    "SELECT * FROM market_account_inventory WHERE registration_id = ?", (identifier,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._market_account_inventory_record(row)

    def _get_market_identity_binding_sync(self, worker_id: str) -> JsonDict | None:
        self._ensure_initialized()
        identifier = _optional_text(worker_id)
        if identifier is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_worker_identity_bindings WHERE worker_id = ?", (identifier,)
            ).fetchone()
        return self._market_identity_binding_record(row) if row else None

    def _list_market_identity_bindings_sync(self, job_id: str | None, active_only: bool) -> list[JsonDict]:
        self._ensure_initialized()
        query = "SELECT * FROM market_worker_identity_bindings WHERE 1 = 1"
        parameters: list[Any] = []
        if job_id is not None:
            query += " AND job_id = ?"
            parameters.append(job_id)
        if active_only:
            query += " AND state = 'active'"
        query += " ORDER BY worker_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._market_identity_binding_record(row) for row in rows]

    def _claim_market_identity_binding_sync(
        self,
        worker_id: str,
        job_id: str,
        registration_id: str,
        transport_id: str,
        current_egress_ip: str,
        binding_mode: str,
        lease_token: str,
        lease_deadline: str | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        worker = _optional_text(worker_id)
        job = _optional_text(job_id)
        account = _optional_text(registration_id)
        transport = _optional_text(transport_id)
        egress_ip = _optional_text(current_egress_ip)
        mode = _optional_text(binding_mode) or "fallback"
        token = _optional_text(lease_token)
        if worker is None or job is None or account is None or transport is None or egress_ip is None or token is None:
            raise ValueError(
                "worker_id, job_id, registration_id, transport_id, current_egress_ip and lease_token are required"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job)
                connection.execute(
                    """
                    UPDATE market_worker_identity_bindings
                    SET state = 'released', released_at = COALESCE(released_at, ?),
                        last_error = COALESCE(last_error, 'lease_expired'), updated_at = ?
                    WHERE state = 'active' AND lease_deadline IS NOT NULL AND lease_deadline <= ?
                        AND worker_id <> ?
                    """,
                    (now, now, now, worker),
                )
                inventory = connection.execute(
                    "SELECT * FROM market_account_inventory WHERE registration_id = ?", (account,)
                ).fetchone()
                if inventory is None:
                    raise MarketJobRepositoryError(f"market account not found: {account}")
                if (
                    int(inventory["market_enabled"]) != 1
                    or str(inventory["status"]) != "activated"
                    or int(inventory["session_cookie_count"]) < 1
                ):
                    raise MarketJobRepositoryError(f"market account is not eligible: {account}")
                route = connection.execute(
                    "SELECT transport_id FROM market_transports WHERE transport_id = ?", (transport,)
                ).fetchone()
                if route is None:
                    raise MarketJobRepositoryError(f"market transport not found: {transport}")
                connection.execute(
                    """
                    UPDATE market_worker_identity_bindings
                    SET state = 'released', released_at = COALESCE(released_at, ?), updated_at = ?
                    WHERE worker_id = ? AND state = 'active'
                    """,
                    (now, now, worker),
                )
                connection.execute(
                    """
                    INSERT INTO market_worker_identity_bindings (
                        worker_id, job_id, registration_id, transport_id, current_egress_ip, binding_mode,
                        state, lease_token, lease_deadline, assigned_at, released_at, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        job_id = excluded.job_id,
                        registration_id = excluded.registration_id,
                        transport_id = excluded.transport_id,
                        current_egress_ip = excluded.current_egress_ip,
                        binding_mode = excluded.binding_mode,
                        state = 'active',
                        lease_token = excluded.lease_token,
                        lease_deadline = excluded.lease_deadline,
                        assigned_at = excluded.assigned_at,
                        released_at = NULL,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (worker, job, account, transport, egress_ip, mode, token, lease_deadline, now, now),
                )
                connection.execute(
                    "UPDATE market_account_inventory SET last_used_at = ?, last_error = NULL, updated_at = ? WHERE registration_id = ?",
                    (now, now, account),
                )
                row = connection.execute(
                    "SELECT * FROM market_worker_identity_bindings WHERE worker_id = ?", (worker,)
                ).fetchone()
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise MarketJobRepositoryError("market account, route, or egress IP is already leased") from exc
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._market_identity_binding_record(row)

    def _renew_market_identity_binding_sync(
        self,
        worker_id: str,
        lease_token: str,
        lease_deadline: str | None,
        now: str,
    ) -> JsonDict | None:
        self._ensure_initialized()
        worker = _optional_text(worker_id)
        token = _optional_text(lease_token)
        if worker is None or token is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE market_worker_identity_bindings
                SET lease_deadline = ?, updated_at = ?
                WHERE worker_id = ? AND state = 'active' AND lease_token = ?
                """,
                (lease_deadline, now, worker, token),
            )
            row = connection.execute(
                "SELECT * FROM market_worker_identity_bindings WHERE worker_id = ?", (worker,)
            ).fetchone()
        return self._market_identity_binding_record(row) if row else None

    def _release_market_identity_binding_sync(self, worker_id: str, reason: str | None, now: str) -> JsonDict | None:
        self._ensure_initialized()
        worker = _optional_text(worker_id)
        if worker is None:
            return None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE market_worker_identity_bindings
                    SET state = 'released', lease_deadline = NULL, released_at = COALESCE(released_at, ?),
                        last_error = ?, updated_at = ?
                    WHERE worker_id = ? AND state = 'active'
                    """,
                    (now, _optional_text(reason), now, worker),
                )
                row = connection.execute(
                    "SELECT * FROM market_worker_identity_bindings WHERE worker_id = ?", (worker,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._market_identity_binding_record(row) if row else None

    def _clear_market_identity_route_sync(self, worker_id: str, reason: str | None, now: str) -> JsonDict | None:
        self._ensure_initialized()
        worker = _optional_text(worker_id)
        if worker is None:
            return None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE market_worker_identity_bindings
                    SET transport_id = NULL, current_egress_ip = NULL, binding_mode = 'waiting_route',
                        last_error = ?, updated_at = ?
                    WHERE worker_id = ? AND state = 'active'
                    """,
                    (_optional_text(reason), now, worker),
                )
                row = connection.execute(
                    "SELECT * FROM market_worker_identity_bindings WHERE worker_id = ?", (worker,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._market_identity_binding_record(row) if row else None

    def _get_listing_sync(self, job_id: str, listing_id: int | str) -> JsonDict | None:
        self._ensure_initialized()
        identifier = self._listing_id_cursor(listing_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_listings WHERE job_id = ? AND listing_id = ?",
                (job_id, identifier),
            ).fetchone()
        return self._listing_record(row) if row else None

    def _list_listings_sync(
        self,
        job_id: str,
        cursor: int | str | None,
        limit: int,
        shard_id: str | None,
    ) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        after = self._listing_id_cursor(cursor, allow_zero=True) if cursor is not None else 0
        query = (
            "SELECT listings.* FROM market_listings AS listings WHERE listings.job_id = ? AND listings.listing_id > ?"
        )
        parameters: list[Any] = [job_id, after]
        if shard_id is not None:
            query += """
                AND EXISTS (
                    SELECT 1 FROM market_listing_observations AS observations
                    WHERE observations.listing_id = listings.listing_id AND observations.shard_id = ?
                )
            """
            parameters.append(shard_id)
        query += " ORDER BY listings.listing_id ASC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._listing_record(row) for row in rows]

    def _prepare_listing_enrichment_sync(
        self,
        job_id: str,
        price_limit: float,
        cache_ttl_seconds: int,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        expires_at = _lease_deadline(now, cache_ttl_seconds)
        active_states = (
            OperationState.QUEUED.value,
            OperationState.LEASED.value,
            OperationState.RUNNING.value,
            OperationState.RETRY_WAIT.value,
        )
        summary = {
            "total_listings": 0,
            "eligible_listings": 0,
            "price_rejected": 0,
            "cached": 0,
            "pending": 0,
            "failed": 0,
            "queued": 0,
            "price_limit": price_limit,
            "cache_expires_at": expires_at,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job_id)
                listings = connection.execute(
                    """
                    SELECT listing_id, price
                    FROM market_listings
                    WHERE job_id = ?
                    ORDER BY listing_id ASC
                    """,
                    (job_id,),
                ).fetchall()
                summary["total_listings"] = len(listings)
                for listing in listings:
                    listing_id = int(listing["listing_id"])
                    price = _optional_number(listing["price"])
                    feature = connection.execute(
                        """
                        SELECT status, generation, expires_at
                        FROM market_listing_features
                        WHERE job_id = ? AND listing_id = ?
                        """,
                        (job_id, listing_id),
                    ).fetchone()
                    if price is not None and price > price_limit:
                        connection.execute(
                            """
                            INSERT INTO market_listing_features (
                                job_id, listing_id, status, generation, updated_at
                            ) VALUES (?, ?, 'reject_price', 0, ?)
                            ON CONFLICT(job_id, listing_id) DO UPDATE SET
                                status = 'reject_price',
                                last_error = NULL,
                                updated_at = excluded.updated_at
                            """,
                            (job_id, listing_id, now),
                        )
                        summary["price_rejected"] += 1
                        continue

                    summary["eligible_listings"] += 1
                    feature_status = str(feature["status"]) if feature is not None else ""
                    feature_expires_at = feature["expires_at"] if feature is not None else None
                    if feature_status in {"ok", "partial"} and feature_expires_at and feature_expires_at > now:
                        summary["cached"] += 1
                        continue
                    if feature_status == "failed":
                        summary["failed"] += 1
                        continue

                    active = connection.execute(
                        f"""
                        SELECT 1
                        FROM market_operations
                        WHERE job_id = ?
                          AND kind = ?
                          AND idempotency_key LIKE ?
                          AND state IN ({",".join("?" for _ in active_states)})
                        LIMIT 1
                        """,
                        (
                            job_id,
                            OperationKind.ENRICH_LISTING.value,
                            f"enrich:{listing_id}:%",
                            *active_states,
                        ),
                    ).fetchone()
                    if active is not None:
                        summary["pending"] += 1
                        continue

                    generation = int(feature["generation"] or 0) + 1 if feature is not None else 1
                    connection.execute(
                        """
                        INSERT INTO market_listing_features (
                            job_id, listing_id, status, generation, expires_at, last_error, updated_at
                        ) VALUES (?, ?, 'pending', ?, ?, NULL, ?)
                        ON CONFLICT(job_id, listing_id) DO UPDATE SET
                            status = 'pending',
                            generation = excluded.generation,
                            expires_at = excluded.expires_at,
                            last_error = NULL,
                            updated_at = excluded.updated_at
                        """,
                        (job_id, listing_id, generation, expires_at, now),
                    )
                    values = self._operation_values(
                        Operation(
                            operation_id=_new_identifier("op"),
                            job_id=job_id,
                            shard_id=None,
                            kind=OperationKind.ENRICH_LISTING,
                            state=OperationState.QUEUED,
                            priority=450,
                            idempotency_key=f"enrich:{listing_id}:{generation}",
                            payload={
                                "listing_id": listing_id,
                                "generation": generation,
                                "price_limit": price_limit,
                                "cache_expires_at": expires_at,
                            },
                        )
                    )
                    self._enqueue_operation_locked(connection, values, now)
                    summary["queued"] += 1
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return summary

    def _get_listing_enrichment_sync(self, job_id: str, listing_id: int | str) -> JsonDict | None:
        self._ensure_initialized()
        identifier = self._listing_id_cursor(listing_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT features.*, listings.seller_key
                FROM market_listing_features AS features
                JOIN market_listings AS listings ON listings.listing_id = features.listing_id
                WHERE features.job_id = ? AND features.listing_id = ?
                """,
                (job_id, identifier),
            ).fetchone()
            if row is None:
                return None
            reviews = connection.execute(
                """
                SELECT * FROM market_listing_reviews
                WHERE job_id = ? AND listing_id = ?
                ORDER BY time_added DESC, review_key ASC
                """,
                (job_id, identifier),
            ).fetchall()
        return {
            **self._listing_feature_record(row),
            "reviews": [self._listing_review_record(review) for review in reviews],
        }

    def _get_seller_enrichment_sync(self, job_id: str, seller_key: str) -> JsonDict | None:
        self._ensure_initialized()
        normalized = _optional_text(seller_key)
        if normalized is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM market_seller_features
                WHERE job_id = ? AND seller_key = ?
                """,
                (job_id, normalized),
            ).fetchone()
        return self._seller_feature_record(row) if row else None

    def _mark_listing_enrichment_failed_sync(
        self,
        job_id: str,
        listing_id: int | str,
        generation: int,
        error: str,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        identifier = self._listing_id_cursor(listing_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                listing = connection.execute(
                    "SELECT 1 FROM market_listings WHERE job_id = ? AND listing_id = ?",
                    (job_id, identifier),
                ).fetchone()
                if listing is None:
                    raise MarketJobNotFoundError(f"listing {identifier!r} was not found in job {job_id!r}")
                connection.execute(
                    """
                    INSERT INTO market_listing_features (
                        job_id, listing_id, status, generation, last_error, updated_at
                    ) VALUES (?, ?, 'failed', ?, ?, ?)
                    ON CONFLICT(job_id, listing_id) DO UPDATE SET
                        status = 'failed',
                        generation = MAX(generation, excluded.generation),
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (job_id, identifier, generation, error, now),
                )
                row = connection.execute(
                    """
                    SELECT features.*, listings.seller_key
                    FROM market_listing_features AS features
                    JOIN market_listings AS listings ON listings.listing_id = features.listing_id
                    WHERE features.job_id = ? AND features.listing_id = ?
                    """,
                    (job_id, identifier),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._listing_feature_record(row)

    def _persist_listing_enrichment_sync(
        self,
        job_id: str,
        listing_id: int | str,
        listing_features: JsonDict,
        seller_features: JsonDict | None,
        reviews: Sequence[JsonDict],
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        identifier = self._listing_id_cursor(listing_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                listing = connection.execute(
                    """
                    SELECT listing_id, seller_key
                    FROM market_listings
                    WHERE job_id = ? AND listing_id = ?
                    """,
                    (job_id, identifier),
                ).fetchone()
                if listing is None:
                    raise MarketJobNotFoundError(f"listing {identifier!r} was not found in job {job_id!r}")
                existing = connection.execute(
                    """
                    SELECT generation FROM market_listing_features
                    WHERE job_id = ? AND listing_id = ?
                    """,
                    (job_id, identifier),
                ).fetchone()
                generation = int(listing_features.get("generation") or (existing["generation"] if existing else 1) or 1)
                status = _optional_text(listing_features.get("status")) or "ok"
                expires_at = listing_features.get("expires_at")
                connection.execute(
                    """
                    INSERT INTO market_listing_features (
                        job_id, listing_id, status, generation, resolved_seller_key, detail_json, extra_json,
                        description, instructions, service_size, queue_count, work_time_seconds,
                        listing_reviews_count, good_reviews, bad_reviews, last_review_at,
                        fetched_at, expires_at, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id, listing_id) DO UPDATE SET
                        status = excluded.status,
                        generation = excluded.generation,
                        resolved_seller_key = excluded.resolved_seller_key,
                        detail_json = excluded.detail_json,
                        extra_json = excluded.extra_json,
                        description = excluded.description,
                        instructions = excluded.instructions,
                        service_size = excluded.service_size,
                        queue_count = excluded.queue_count,
                        work_time_seconds = excluded.work_time_seconds,
                        listing_reviews_count = excluded.listing_reviews_count,
                        good_reviews = excluded.good_reviews,
                        bad_reviews = excluded.bad_reviews,
                        last_review_at = excluded.last_review_at,
                        fetched_at = excluded.fetched_at,
                        expires_at = excluded.expires_at,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        job_id,
                        identifier,
                        status,
                        generation,
                        _optional_text(listing_features.get("resolved_seller_key")),
                        _dump_json(listing_features.get("detail", {})),
                        _dump_json(listing_features.get("extra", {})),
                        _optional_text(listing_features.get("description")),
                        _optional_text(listing_features.get("instructions")),
                        _optional_text(listing_features.get("service_size")),
                        _optional_int(listing_features.get("queue_count")),
                        _optional_int(listing_features.get("work_time_seconds")),
                        _optional_int(listing_features.get("listing_reviews_count")),
                        _optional_int(listing_features.get("good_reviews")),
                        _optional_int(listing_features.get("bad_reviews")),
                        _optional_text(listing_features.get("last_review_at")),
                        _optional_text(listing_features.get("fetched_at")) or now,
                        _optional_text(expires_at),
                        _optional_text(listing_features.get("last_error")),
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM market_listing_reviews WHERE job_id = ? AND listing_id = ?",
                    (job_id, identifier),
                )
                for review in reviews:
                    review_key = _optional_text(review.get("review_key"))
                    if review_key is None:
                        continue
                    connection.execute(
                        """
                        INSERT INTO market_listing_reviews (
                            job_id, listing_id, review_key, time_added, is_good, is_bad,
                            review_text, writer, answer, raw_json, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            identifier,
                            review_key,
                            _optional_text(review.get("time_added")),
                            _optional_flag(review.get("is_good")),
                            _optional_flag(review.get("is_bad")),
                            _optional_text(review.get("text")),
                            _optional_text(review.get("writer")),
                            _optional_text(review.get("answer")),
                            _dump_json(review.get("raw", review)),
                            now,
                        ),
                    )
                if seller_features is not None:
                    seller_key = _optional_text(seller_features.get("seller_key")) or _optional_text(
                        listing["seller_key"]
                    )
                    if seller_key is not None:
                        connection.execute(
                            """
                            INSERT INTO market_seller_features (
                                job_id, seller_key, seller_id, status, profile_json,
                                seller_rating, seller_rating_count, seller_reviews_count, seller_addtime,
                                completed_orders_count, active_kworks_count, fetched_at, expires_at,
                                last_error, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(job_id, seller_key) DO UPDATE SET
                                seller_id = excluded.seller_id,
                                status = excluded.status,
                                profile_json = excluded.profile_json,
                                seller_rating = excluded.seller_rating,
                                seller_rating_count = excluded.seller_rating_count,
                                seller_reviews_count = excluded.seller_reviews_count,
                                seller_addtime = excluded.seller_addtime,
                                completed_orders_count = excluded.completed_orders_count,
                                active_kworks_count = excluded.active_kworks_count,
                                fetched_at = excluded.fetched_at,
                                expires_at = excluded.expires_at,
                                last_error = excluded.last_error,
                                updated_at = excluded.updated_at
                            """,
                            (
                                job_id,
                                seller_key,
                                _optional_text(seller_features.get("seller_id")),
                                _optional_text(seller_features.get("status")) or "ok",
                                _dump_json(seller_features.get("profile", {})),
                                _optional_number(seller_features.get("seller_rating")),
                                _optional_int(seller_features.get("seller_rating_count")),
                                _optional_int(seller_features.get("seller_reviews_count")),
                                _optional_text(seller_features.get("seller_addtime")),
                                _optional_int(seller_features.get("completed_orders_count")),
                                _optional_int(seller_features.get("active_kworks_count")),
                                _optional_text(seller_features.get("fetched_at")) or now,
                                _optional_text(seller_features.get("expires_at")),
                                _optional_text(seller_features.get("last_error")),
                                now,
                            ),
                        )
                row = connection.execute(
                    """
                    SELECT features.*, listings.seller_key
                    FROM market_listing_features AS features
                    JOIN market_listings AS listings ON listings.listing_id = features.listing_id
                    WHERE features.job_id = ? AND features.listing_id = ?
                    """,
                    (job_id, identifier),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._listing_feature_record(row)

    def _list_local_analysis_inputs_sync(self, job_id: str) -> list[JsonDict]:
        self._ensure_initialized()
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT
                    listings.listing_id,
                    listings.listing_key,
                    listings.title,
                    listings.seller_key,
                    features.resolved_seller_key,
                    COALESCE(features.resolved_seller_key, listings.seller_key) AS analysis_seller_key,
                    listings.price,
                    listings.canonical_json,
                    features.status AS feature_status,
                    features.description,
                    features.instructions,
                    features.queue_count,
                    features.listing_reviews_count,
                    features.good_reviews,
                    features.bad_reviews,
                    seller.profile_json AS seller_profile_json,
                    seller.seller_rating,
                    seller.seller_rating_count,
                    seller.seller_reviews_count,
                    seller.seller_addtime,
                    seller.completed_orders_count,
                    seller.active_kworks_count
                FROM market_listings AS listings
                JOIN market_listing_features AS features
                    ON features.job_id = listings.job_id AND features.listing_id = listings.listing_id
                LEFT JOIN market_seller_features AS seller
                    ON seller.job_id = listings.job_id
                    AND seller.seller_key = COALESCE(features.resolved_seller_key, listings.seller_key) COLLATE NOCASE
                WHERE listings.job_id = ?
                  AND features.status IN ('ok', 'partial')
                  AND (listings.price IS NULL OR listings.price <= 15000)
                ORDER BY listings.listing_id ASC
                """,
                (job_id,),
            ).fetchall()
            review_rows = connection.execute(
                """
                SELECT * FROM market_listing_reviews
                WHERE job_id = ?
                ORDER BY listing_id ASC, time_added DESC, review_key ASC
                """,
                (job_id,),
            ).fetchall()
        reviews_by_listing: dict[int, list[JsonDict]] = {}
        for review in review_rows:
            reviews_by_listing.setdefault(int(review["listing_id"]), []).append(self._listing_review_record(review))
        records: list[JsonDict] = []
        for row in rows:
            listing_id = int(row["listing_id"])
            canonical = _load_json(row["canonical_json"], {})
            seller_profile = _load_json(row["seller_profile_json"], {})
            records.append(
                {
                    "listing_id": listing_id,
                    "listing_key": row["listing_key"],
                    "title": row["title"],
                    "seller_key": row["analysis_seller_key"],
                    "price": row["price"],
                    "canonical": canonical,
                    "url": canonical.get("url") or canonical.get("share_url"),
                    "status": row["feature_status"],
                    "description": row["description"],
                    "instructions": row["instructions"],
                    "queue_count": row["queue_count"],
                    "listing_reviews_count": row["listing_reviews_count"],
                    "good_reviews": row["good_reviews"],
                    "bad_reviews": row["bad_reviews"],
                    "reviews": reviews_by_listing.get(listing_id, []),
                    "seller": {
                        "profile": seller_profile,
                        "seller_rating": row["seller_rating"],
                        "seller_rating_count": row["seller_rating_count"],
                        "seller_reviews_count": row["seller_reviews_count"],
                        "seller_addtime": row["seller_addtime"],
                        "completed_orders_count": row["completed_orders_count"],
                        "active_kworks_count": row["active_kworks_count"],
                    },
                }
            )
        return records

    def _replace_semantic_analysis_sync(self, job_id: str, analysis: JsonDict, now: str) -> JsonDict:
        self._ensure_initialized()
        model_name = _optional_text(analysis.get("embedding_model"))
        embeddings = analysis.get("embeddings")
        clusters = analysis.get("clusters")
        dossier = analysis.get("dossier")
        if model_name is None:
            raise ValueError("semantic analysis requires embedding_model")
        if not isinstance(embeddings, list) or not all(isinstance(item, Mapping) for item in embeddings):
            raise TypeError("semantic analysis embeddings must be a list of mappings")
        if not isinstance(clusters, list) or not all(isinstance(item, Mapping) for item in clusters):
            raise TypeError("semantic analysis clusters must be a list of mappings")
        if not isinstance(dossier, Mapping):
            raise TypeError("semantic analysis dossier must be a mapping")
        groups = dossier.get("groups")
        group_by_id = (
            {
                _optional_text(group.get("cluster_id")): dict(group)
                for group in groups
                if isinstance(group, Mapping) and _optional_text(group.get("cluster_id"))
            }
            if isinstance(groups, list)
            else {}
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job_id)
                price_rows = connection.execute(
                    "SELECT listing_id, price FROM market_listings WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
                allowed_listing_ids = {
                    int(row["listing_id"])
                    for row in price_rows
                    if row["price"] is None or float(row["price"]) <= MAX_ENRICHMENT_PRICE
                }
                embedding_ids: set[int] = set()
                for embedding in embeddings:
                    listing_id = self._listing_id_cursor(embedding.get("listing_id"))
                    if listing_id not in allowed_listing_ids:
                        raise ValueError(
                            f"semantic embedding listing {listing_id} is not price-eligible for job {job_id}"
                        )
                    text_hash = _optional_text(embedding.get("text_hash"))
                    vector = embedding.get("vector")
                    if text_hash is None or not isinstance(vector, list) or not vector:
                        raise ValueError("semantic embedding requires text_hash and a non-empty vector")
                    values = [float(value) for value in vector]
                    connection.execute(
                        """
                        INSERT INTO market_listing_embeddings (
                            job_id, listing_id, model_name, text_hash, vector_json, dimension, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_id, listing_id) DO UPDATE SET
                            model_name = excluded.model_name,
                            text_hash = excluded.text_hash,
                            vector_json = excluded.vector_json,
                            dimension = excluded.dimension,
                            updated_at = excluded.updated_at
                        """,
                        (job_id, listing_id, model_name, text_hash, _dump_json(values), len(values), now, now),
                    )
                    embedding_ids.add(listing_id)
                if embedding_ids:
                    placeholders = ",".join("?" for _ in embedding_ids)
                    connection.execute(
                        f"DELETE FROM market_listing_embeddings WHERE job_id = ? AND listing_id NOT IN ({placeholders})",
                        (job_id, *sorted(embedding_ids)),
                    )
                else:
                    connection.execute("DELETE FROM market_listing_embeddings WHERE job_id = ?", (job_id,))

                connection.execute("DELETE FROM market_semantic_cluster_members WHERE job_id = ?", (job_id,))
                connection.execute("DELETE FROM market_semantic_clusters WHERE job_id = ?", (job_id,))
                connection.execute(
                    """
                    INSERT INTO market_semantic_dossiers (job_id, dossier_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        dossier_json = excluded.dossier_json,
                        updated_at = excluded.updated_at
                    """,
                    (job_id, _dump_json(dossier), now, now),
                )
                for cluster in clusters:
                    cluster_id = _optional_text(cluster.get("cluster_id"))
                    label = _optional_text(cluster.get("label"))
                    state = _optional_text(cluster.get("state"))
                    member_ids = cluster.get("member_listing_ids")
                    if cluster_id is None or label is None or state is None or not isinstance(member_ids, list):
                        raise ValueError("semantic cluster requires cluster_id, label, state, and member_listing_ids")
                    normalized_member_ids = [self._listing_id_cursor(value) for value in member_ids]
                    if len(set(normalized_member_ids)) != len(normalized_member_ids):
                        raise ValueError(f"semantic cluster {cluster_id} has duplicate member listings")
                    if not set(normalized_member_ids) <= allowed_listing_ids:
                        raise ValueError(f"semantic cluster {cluster_id} contains a non-eligible listing")
                    connection.execute(
                        """
                        INSERT INTO market_semantic_clusters (
                            job_id, cluster_id, label, state, confidence, member_count,
                            metrics_json, dossier_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            cluster_id,
                            label,
                            state,
                            max(min(_optional_int(cluster.get("confidence")) or 0, 100), 0),
                            len(normalized_member_ids),
                            _dump_json(cluster.get("metrics", {})),
                            _dump_json(group_by_id.get(cluster_id, {})),
                            now,
                            now,
                        ),
                    )
                    for position, listing_id in enumerate(normalized_member_ids):
                        connection.execute(
                            """
                            INSERT INTO market_semantic_cluster_members (
                                job_id, cluster_id, listing_id, position
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (job_id, cluster_id, listing_id, position),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._get_semantic_analysis_sync(job_id)

    def _get_embedding_cache_sync(self, job_id: str, model_name: str) -> dict[str, list[float]]:
        self._ensure_initialized()
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT text_hash, vector_json
                FROM market_listing_embeddings
                WHERE job_id = ? AND model_name = ?
                ORDER BY updated_at DESC, listing_id ASC
                """,
                (job_id, model_name),
            ).fetchall()
        cache: dict[str, list[float]] = {}
        for row in rows:
            if row["text_hash"] in cache:
                continue
            raw_vector = _load_json(row["vector_json"], [])
            if not isinstance(raw_vector, list) or not raw_vector:
                continue
            try:
                cache[row["text_hash"]] = [float(value) for value in raw_vector]
            except (TypeError, ValueError):
                continue
        return cache

    def _get_semantic_analysis_sync(self, job_id: str) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            clusters = connection.execute(
                """
                SELECT * FROM market_semantic_clusters
                WHERE job_id = ?
                ORDER BY confidence DESC, cluster_id ASC
                """,
                (job_id,),
            ).fetchall()
            members = connection.execute(
                """
                SELECT cluster_id, listing_id FROM market_semantic_cluster_members
                WHERE job_id = ?
                ORDER BY cluster_id ASC, position ASC
                """,
                (job_id,),
            ).fetchall()
            embedding_models = connection.execute(
                "SELECT DISTINCT model_name FROM market_listing_embeddings WHERE job_id = ? ORDER BY model_name ASC",
                (job_id,),
            ).fetchall()
            dossier_row = connection.execute(
                "SELECT dossier_json FROM market_semantic_dossiers WHERE job_id = ?", (job_id,)
            ).fetchone()
        members_by_cluster: dict[str, list[int]] = {}
        for member in members:
            members_by_cluster.setdefault(member["cluster_id"], []).append(int(member["listing_id"]))
        records = [
            {
                "cluster_id": row["cluster_id"],
                "label": row["label"],
                "state": row["state"],
                "confidence": int(row["confidence"]),
                "member_count": int(row["member_count"]),
                "member_listing_ids": members_by_cluster.get(row["cluster_id"], []),
                "metrics": _load_json(row["metrics_json"], {}),
                "dossier": _load_json(row["dossier_json"], {}),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in clusters
        ]
        stored_dossier = _load_json(dossier_row["dossier_json"], {}) if dossier_row is not None else {}
        if not isinstance(stored_dossier, Mapping):
            stored_dossier = {}
        dossier = dict(stored_dossier)
        if not dossier:
            dossier = {
                "schema_version": 1,
                "groups": [record["dossier"] for record in records if record["dossier"]],
            }
        return {
            "schema_version": 1,
            "job_id": job_id,
            "embedding_models": [row["model_name"] for row in embedding_models],
            "cluster_count": len(records),
            "clusters": records,
            "dossier": dossier,
        }

    def _create_recommendation_sync(
        self,
        job_id: str,
        category_id: int,
        service_summary: str,
        price: int,
        work_time: int,
        classifier_id: int | None,
        source_cluster_id: str | None,
        evidence_ids: tuple[str, ...],
        terra_result: JsonDict,
        recommendation_id: str | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        identifier = recommendation_id or _new_identifier("rec")
        evidence = list(dict.fromkeys(evidence_ids))
        content = {
            "category_id": category_id,
            "classifier_id": classifier_id,
            "service_summary": service_summary,
            "price": price,
            "work_time": work_time,
            "source_cluster_id": source_cluster_id,
            "evidence_ids": evidence,
            "terra_result": terra_result,
        }
        content_hash = f"sha256:{hashlib.sha256(_dump_json(content).encode('utf-8')).hexdigest()}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_job_row(connection, job_id)
                connection.execute(
                    """
                    INSERT INTO market_recommendations (
                        recommendation_id, job_id, source_cluster_id, state, category_id, classifier_id,
                        service_summary, price, work_time, evidence_ids_json, terra_result_json,
                        content_hash, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        identifier,
                        job_id,
                        source_cluster_id,
                        category_id,
                        classifier_id,
                        service_summary,
                        price,
                        work_time,
                        _dump_json(evidence),
                        _dump_json(terra_result),
                        content_hash,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM market_recommendations WHERE recommendation_id = ?", (identifier,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._recommendation_record(row)

    def _get_recommendation_sync(self, recommendation_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_recommendations WHERE recommendation_id = ?", (recommendation_id,)
            ).fetchone()
        return self._recommendation_record(row) if row is not None else None

    def _list_recommendations_sync(self, job_id: str, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        bounded_limit = max(1, min(int(limit), 1_000))
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT * FROM market_recommendations
                WHERE job_id = ?
                ORDER BY updated_at DESC, recommendation_id ASC
                LIMIT ?
                """,
                (job_id, bounded_limit),
            ).fetchall()
        return [self._recommendation_record(row) for row in rows]

    def _transition_recommendation_sync(
        self,
        recommendation_id: str,
        state: str,
        expected_revision: int | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM market_recommendations WHERE recommendation_id = ?", (recommendation_id,)
                ).fetchone()
                if row is None:
                    raise MarketJobNotFoundError(f"market recommendation {recommendation_id!r} was not found")
                if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                    raise MarketJobRevisionConflictError(
                        f"market recommendation {recommendation_id!r} revision is {row['revision']}, "
                        f"not {expected_revision}"
                    )
                current_state = row["state"]
                if current_state == "rejected" and state != "rejected":
                    raise MarketJobRepositoryError(f"market recommendation {recommendation_id!r} is rejected")
                if current_state != state:
                    connection.execute(
                        """
                        UPDATE market_recommendations
                        SET state = ?, revision = revision + 1, updated_at = ?
                        WHERE recommendation_id = ?
                        """,
                        (state, now, recommendation_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM market_recommendations WHERE recommendation_id = ?", (recommendation_id,)
                    ).fetchone()

                handoff_row = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE recommendation_id = ?", (recommendation_id,)
                ).fetchone()
                if state == "recommendation_confirmed" and handoff_row is None:
                    handoff_id = _new_identifier("handoff")
                    connection.execute(
                        """
                        INSERT INTO market_draft_handoffs (
                            handoff_id, job_id, recommendation_id, state, category_id, classifier_id,
                            service_summary, price, work_time, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, 'mapping', ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            handoff_id,
                            row["job_id"],
                            recommendation_id,
                            row["category_id"],
                            row["classifier_id"],
                            row["service_summary"],
                            row["price"],
                            row["work_time"],
                            now,
                            now,
                        ),
                    )
                    handoff_row = connection.execute(
                        "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                    ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "recommendation": self._recommendation_record(row),
            "handoff": self._draft_handoff_record(handoff_row) if handoff_row is not None else None,
        }

    def _get_draft_handoff_sync(self, handoff_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        return self._draft_handoff_record(row) if row is not None else None

    def _get_draft_handoff_for_recommendation_sync(self, recommendation_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_draft_handoffs WHERE recommendation_id = ?", (recommendation_id,)
            ).fetchone()
        return self._draft_handoff_record(row) if row is not None else None

    def _save_draft_handoff_fields_sync(
        self,
        handoff_id: str,
        manifest: JsonDict,
        manifest_hash: str,
        selection: JsonDict,
        selection_hash: str,
        validation: JsonDict,
        fields_confirmed: bool,
        expected_manifest_hash: str | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
                if row is None:
                    raise MarketJobNotFoundError(f"market draft handoff {handoff_id!r} was not found")
                if expected_manifest_hash is not None and row["manifest_hash"] != expected_manifest_hash:
                    raise MarketJobRevisionConflictError(
                        f"market draft handoff {handoff_id!r} manifest hash no longer matches"
                    )
                if row["state"] not in {"mapping", "fields_confirmed", "draft_generated"}:
                    raise MarketJobRepositoryError(f"market draft handoff {handoff_id!r} cannot accept fields")

                changed = row["manifest_hash"] != manifest_hash or row["selection_hash"] != selection_hash
                if not changed and row["state"] == "draft_generated":
                    next_state = "draft_generated"
                elif fields_confirmed or (not changed and row["state"] == "fields_confirmed"):
                    next_state = "fields_confirmed"
                else:
                    next_state = "mapping"
                clear_draft = changed or (row["state"] == "draft_generated" and next_state != "draft_generated")
                if next_state == "draft_generated":
                    generator_request = row["generator_request_json"]
                elif next_state == "fields_confirmed":
                    generator_request = _dump_json({"status": "ready"})
                else:
                    generator_request = "{}"
                draft_json = row["draft_json"]
                if clear_draft:
                    draft_json = "{}"
                connection.execute(
                    """
                    UPDATE market_draft_handoffs
                    SET state = ?, manifest_json = ?, manifest_hash = ?, selection_json = ?, selection_hash = ?,
                        validation_json = ?, generator_request_json = ?, draft_json = ?, draft_hash = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE handoff_id = ?
                    """,
                    (
                        next_state,
                        _dump_json(manifest),
                        manifest_hash,
                        _dump_json(selection),
                        selection_hash,
                        _dump_json(validation),
                        generator_request,
                        draft_json,
                        None if clear_draft else row["draft_hash"],
                        now,
                        handoff_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._draft_handoff_record(updated)

    def _store_draft_handoff_draft_sync(
        self,
        handoff_id: str,
        draft: JsonDict,
        draft_hash: str,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
                if row is None:
                    raise MarketJobNotFoundError(f"market draft handoff {handoff_id!r} was not found")
                if row["state"] not in {"fields_confirmed", "draft_generated"}:
                    raise MarketJobRepositoryError(
                        f"market draft handoff {handoff_id!r} requires fields_confirmed before draft generation"
                    )
                connection.execute(
                    """
                    UPDATE market_draft_handoffs
                    SET state = 'draft_generated', draft_json = ?, draft_hash = ?, revision = revision + 1, updated_at = ?
                    WHERE handoff_id = ?
                    """,
                    (_dump_json(draft), draft_hash, now, handoff_id),
                )
                updated = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._draft_handoff_record(updated)

    def _record_published_listing_sync(
        self,
        handoff_id: str,
        publish_result: JsonDict,
        kwork_id: str | None,
        draft_hash: str | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                handoff = connection.execute(
                    "SELECT * FROM market_draft_handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
                if handoff is None:
                    raise MarketJobNotFoundError(f"market draft handoff {handoff_id!r} was not found")
                if handoff["state"] != "draft_generated" or not handoff["draft_hash"]:
                    raise MarketJobRepositoryError(
                        f"market draft handoff {handoff_id!r} requires a generated draft before publication"
                    )
                effective_draft_hash = draft_hash or handoff["draft_hash"]
                recommendation = connection.execute(
                    "SELECT * FROM market_recommendations WHERE recommendation_id = ?",
                    (handoff["recommendation_id"],),
                ).fetchone()
                if recommendation is None:
                    raise MarketJobRepositoryError(f"market recommendation for handoff {handoff_id!r} is missing")
                existing = connection.execute(
                    """
                    SELECT * FROM market_published_listings
                    WHERE handoff_id = ? AND draft_hash = ?
                    """,
                    (handoff_id, effective_draft_hash),
                ).fetchone()
                if existing is None:
                    published_id = _new_identifier("published")
                    connection.execute(
                        """
                        INSERT INTO market_published_listings (
                            published_listing_id, job_id, recommendation_id, handoff_id, source_cluster_id,
                            kwork_id, draft_hash, publish_result_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            published_id,
                            handoff["job_id"],
                            handoff["recommendation_id"],
                            handoff_id,
                            recommendation["source_cluster_id"],
                            kwork_id,
                            effective_draft_hash,
                            _dump_json(publish_result),
                            now,
                            now,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM market_published_listings WHERE published_listing_id = ?", (published_id,)
                    ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._published_listing_record(existing)

    def _list_published_listings_sync(self, job_id: str, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        bounded_limit = max(1, min(int(limit), 1_000))
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT * FROM market_published_listings
                WHERE job_id = ?
                ORDER BY created_at DESC, published_listing_id ASC
                LIMIT ?
                """,
                (job_id, bounded_limit),
            ).fetchall()
        return [self._published_listing_record(row) for row in rows]

    def _list_listing_metrics_inputs_sync(self, job_id: str) -> list[JsonDict]:
        self._ensure_initialized()
        with self._connect() as connection:
            self._require_job_row(connection, job_id)
            rows = connection.execute(
                """
                SELECT
                    listings.listing_id,
                    listings.listing_key,
                    listings.title,
                    listings.seller_key,
                    listings.price,
                    (
                        SELECT observations.shard_id
                        FROM market_listing_observations AS observations
                        WHERE observations.listing_id = listings.listing_id
                        ORDER BY observations.observation_id ASC
                        LIMIT 1
                    ) AS shard_id
                FROM market_listings AS listings
                WHERE listings.job_id = ?
                ORDER BY listings.listing_id ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "listing_id": int(row["listing_id"]),
                "listing_key": row["listing_key"],
                "title": row["title"],
                "seller_key": row["seller_key"],
                "price": row["price"],
                "shard_id": row["shard_id"],
            }
            for row in rows
        ]

    def _load_export_snapshot_sync(self, job_id: str) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            job = self._require_job_row(connection, job_id)
            listings = connection.execute(
                "SELECT * FROM market_listings WHERE job_id = ? ORDER BY listing_id ASC",
                (job_id,),
            ).fetchall()
            observations = connection.execute(
                "SELECT * FROM market_listing_observations WHERE job_id = ? ORDER BY observation_id ASC",
                (job_id,),
            ).fetchall()
            events = connection.execute(
                "SELECT * FROM market_events WHERE job_id = ? ORDER BY sequence ASC",
                (job_id,),
            ).fetchall()
        return {
            "job": self._job_record(job),
            "listings": [self._listing_record(row) for row in listings],
            "observations": [self._observation_record(row) for row in observations],
            "events": [self._event_record(row) for row in events],
        }

    def _create_checkpoint_sync(
        self,
        job_id: str,
        frontier: Mapping[str, Any],
        metrics: Mapping[str, Any] | None,
        set_latest: bool,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = self._require_job_row(connection, job_id)
                checkpoint_id = _new_identifier("checkpoint")
                last_sequence = self._latest_event_sequence(connection, job_id)
                metric_values = metrics if metrics is not None else _load_json(job["counters_json"], {})
                connection.execute(
                    """
                    INSERT INTO market_checkpoints (
                        checkpoint_id, job_id, revision, phase, frontier_json, metrics_json, last_event_sequence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        job_id,
                        job["revision"],
                        job["phase"],
                        _dump_json(frontier),
                        _dump_json(metric_values),
                        last_sequence,
                        now,
                    ),
                )
                if set_latest:
                    connection.execute(
                        "UPDATE market_jobs SET latest_checkpoint_id = ?, updated_at = ? WHERE job_id = ?",
                        (checkpoint_id, now, job_id),
                    )
                row = connection.execute(
                    "SELECT * FROM market_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._checkpoint_record(row)

    def _get_checkpoint_sync(self, checkpoint_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        return self._checkpoint_record(row) if row else None

    def _list_checkpoints_sync(self, job_id: str, cursor: str | None, limit: int) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        query = "SELECT * FROM market_checkpoints WHERE job_id = ?"
        parameters: list[Any] = [job_id]
        after = self._cursor_text(cursor)
        with self._connect() as connection:
            if after is not None:
                anchor = connection.execute(
                    """
                    SELECT revision, created_at, checkpoint_id FROM market_checkpoints
                    WHERE job_id = ? AND checkpoint_id = ?
                    """,
                    (job_id, after),
                ).fetchone()
                if anchor is None:
                    raise ValueError("checkpoint cursor does not belong to this job")
                query += """
                    AND (
                        revision < ?
                        OR (revision = ? AND created_at < ?)
                        OR (revision = ? AND created_at = ? AND checkpoint_id < ?)
                    )
                """
                parameters.extend(
                    (
                        anchor["revision"],
                        anchor["revision"],
                        anchor["created_at"],
                        anchor["revision"],
                        anchor["created_at"],
                        anchor["checkpoint_id"],
                    )
                )
            query += " ORDER BY revision DESC, created_at DESC, checkpoint_id DESC LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
        return [self._checkpoint_record(row) for row in rows]

    def _enqueue_worker_command_sync(
        self,
        command_type: str,
        job_id: str | None,
        worker_id: str | None,
        payload: Mapping[str, Any],
        command_id: str | None,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        command = _optional_text(command_type)
        if command is None:
            raise ValueError("command_type is required")
        if job_id is None and worker_id is None:
            raise ValueError("worker command requires a job_id or worker_id")
        if not isinstance(payload, Mapping):
            raise TypeError("command payload must be a mapping")
        identifier = _optional_text(command_id) or _new_identifier("command")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if job_id is not None:
                    self._require_job_row(connection, job_id)
                if worker_id is not None:
                    worker = connection.execute(
                        "SELECT worker_id FROM market_workers WHERE worker_id = ?", (worker_id,)
                    ).fetchone()
                    if worker is None:
                        raise MarketJobRepositoryError(f"market worker not found: {worker_id}")
                connection.execute(
                    """
                    INSERT INTO market_worker_commands (
                        command_id, job_id, worker_id, command_type, payload_json, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, job_id, worker_id, command, _dump_json(payload), CommandState.QUEUED.value, now),
                )
                row = self._require_worker_command_row(connection, identifier)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._worker_command_record(row)

    def _get_worker_command_sync(self, command_id: str) -> JsonDict | None:
        self._ensure_initialized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_worker_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return self._worker_command_record(row) if row else None

    def _list_worker_commands_sync(
        self,
        job_id: str | None,
        worker_id: str | None,
        state: CommandState | str | Iterable[CommandState | str] | None,
        cursor: str | None,
        limit: int,
    ) -> list[JsonDict]:
        self._ensure_initialized()
        self._validate_pagination(limit, 0)
        states = self._state_filter_values(state)
        after = self._cursor_text(cursor)
        query = "SELECT * FROM market_worker_commands WHERE 1 = 1"
        parameters: list[Any] = []
        if job_id is not None:
            query += " AND job_id = ?"
            parameters.append(job_id)
        if worker_id is not None:
            query += " AND worker_id = ?"
            parameters.append(worker_id)
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            parameters.extend(states)
        if after is not None:
            query += " AND command_id > ?"
            parameters.append(after)
        query += " ORDER BY command_id ASC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._worker_command_record(row) for row in rows]

    def _update_worker_command_sync(
        self,
        command_id: str,
        state: str,
        error: str | None | object,
        now: str,
    ) -> JsonDict:
        self._ensure_initialized()
        state_value = _optional_text(state)
        if state_value is None:
            raise ValueError("state is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._require_worker_command_row(connection, command_id)
                updates = ["state = ?"]
                parameters: list[Any] = [state_value]
                if (
                    state_value in {CommandState.ACKNOWLEDGED.value, CommandState.COMPLETED.value}
                    and current["acknowledged_at"] is None
                ):
                    updates.append("acknowledged_at = ?")
                    parameters.append(now)
                if (
                    state_value in {CommandState.COMPLETED.value, CommandState.FAILED.value}
                    and current["completed_at"] is None
                ):
                    updates.append("completed_at = ?")
                    parameters.append(now)
                if error is not _UNSET:
                    updates.append("error = ?")
                    parameters.append(error)
                parameters.append(command_id)
                connection.execute(
                    f"UPDATE market_worker_commands SET {', '.join(updates)} WHERE command_id = ?",
                    parameters,
                )
                row = self._require_worker_command_row(connection, command_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._worker_command_record(row)

    def _enqueue_operation_locked(self, connection: sqlite3.Connection, values: JsonDict, now: str) -> sqlite3.Row:
        self._require_job_row(connection, values["job_id"])
        if values["shard_id"] is not None:
            shard = self._require_shard_row(connection, values["shard_id"])
            if shard["job_id"] != values["job_id"]:
                raise MarketCommitConflictError(f"shard {values['shard_id']} does not belong to job {values['job_id']}")
        try:
            connection.execute(
                """
                INSERT INTO market_operations (
                    operation_id, job_id, shard_id, kind, state, priority, idempotency_key, payload_json,
                    not_before, lease_owner, lease_deadline, current_attempt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["operation_id"],
                    values["job_id"],
                    values["shard_id"],
                    values["kind"],
                    values["state"],
                    values["priority"],
                    values["idempotency_key"],
                    _dump_json(values["payload"]),
                    values["not_before"],
                    values["lease_owner"],
                    values["lease_deadline"],
                    values["current_attempt"],
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            if values["idempotency_key"] is None:
                raise
            existing = connection.execute(
                """
                SELECT * FROM market_operations
                WHERE job_id = ? AND idempotency_key = ?
                """,
                (values["job_id"], values["idempotency_key"]),
            ).fetchone()
            if existing is None:
                raise
            return existing
        return self._require_operation_row(connection, values["operation_id"])

    def _recover_expired_operations_locked(self, connection: sqlite3.Connection, now: str) -> int:
        expired = connection.execute(
            """
            SELECT operation_id FROM market_operations
            WHERE state IN (?, ?) AND lease_deadline IS NOT NULL AND lease_deadline <= ?
            """,
            (OperationState.LEASED.value, OperationState.RUNNING.value, now),
        ).fetchall()
        if not expired:
            return 0
        operation_ids = tuple(row["operation_id"] for row in expired)
        placeholders = ",".join("?" for _ in operation_ids)
        connection.execute(
            f"""
            UPDATE market_operation_attempts
            SET state = 'abandoned', finished_at = ?
            WHERE operation_id IN ({placeholders}) AND finished_at IS NULL
            """,
            (now, *operation_ids),
        )
        connection.execute(
            f"""
            UPDATE market_operations
            SET state = ?, lease_owner = NULL, lease_deadline = NULL, leased_attempt_id = NULL, updated_at = ?
            WHERE operation_id IN ({placeholders})
            """,
            (OperationState.QUEUED.value, now, *operation_ids),
        )
        return len(operation_ids)

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        revision: int | None,
        worker_id: str | None,
        operation_id: str | None,
        emitted_at: str,
    ) -> sqlite3.Row:
        sequence = self._latest_event_sequence(connection, job_id) + 1
        connection.execute(
            """
            INSERT INTO market_events (
                job_id, sequence, event_type, payload_json, revision, worker_id, operation_id, emitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, sequence, event_type, _dump_json(payload), revision, worker_id, operation_id, emitted_at),
        )
        return connection.execute(
            "SELECT * FROM market_events WHERE job_id = ? AND sequence = ?", (job_id, sequence)
        ).fetchone()

    def _append_commit_events_locked(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        operation_id: str,
        revision: int,
        event_payloads: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None,
        default_payload: Mapping[str, Any],
        emitted_at: str,
    ) -> list[sqlite3.Row]:
        if event_payloads is None:
            payloads: tuple[Mapping[str, Any], ...] = ({"event_type": "batch.accepted", "payload": default_payload},)
        elif isinstance(event_payloads, Mapping):
            payloads = (event_payloads,)
        else:
            payloads = tuple(event_payloads)
        rows: list[sqlite3.Row] = []
        for item in payloads:
            if not isinstance(item, Mapping):
                raise TypeError("event_payloads must contain mappings")
            event_type = _optional_text(item.get("event_type") or item.get("type")) or "batch.accepted"
            raw_payload = item.get("payload")
            if raw_payload is None:
                raw_payload = {
                    key: value
                    for key, value in item.items()
                    if key not in {"event_type", "type", "revision", "worker_id", "operation_id"}
                }
            if not isinstance(raw_payload, Mapping):
                raise TypeError("event payload must be a mapping")
            rows.append(
                self._append_event_locked(
                    connection,
                    job_id=job_id,
                    event_type=event_type,
                    payload=raw_payload,
                    revision=int(item["revision"]) if item.get("revision") is not None else revision,
                    worker_id=_optional_text(item.get("worker_id")),
                    operation_id=_optional_text(item.get("operation_id")) or operation_id,
                    emitted_at=emitted_at,
                )
            )
        return rows

    @staticmethod
    def _latest_event_sequence(connection: sqlite3.Connection, job_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM market_events WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row["sequence"])

    @staticmethod
    def _latest_attempt_id(connection: sqlite3.Connection, operation_id: str) -> str | None:
        row = connection.execute(
            """
            SELECT attempt_id FROM market_operation_attempts
            WHERE operation_id = ?
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        return str(row["attempt_id"]) if row else None

    @staticmethod
    def _require_job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM market_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise MarketJobNotFoundError(f"market job not found: {job_id}")
        return row

    @staticmethod
    def _require_shard_row(connection: sqlite3.Connection, shard_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM market_shards WHERE shard_id = ?", (shard_id,)).fetchone()
        if row is None:
            raise MarketJobRepositoryError(f"market shard not found: {shard_id}")
        return row

    @staticmethod
    def _require_operation_row(connection: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM market_operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise MarketOperationNotFoundError(f"market operation not found: {operation_id}")
        return row

    @staticmethod
    def _require_worker_command_row(connection: sqlite3.Connection, command_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM market_worker_commands WHERE command_id = ?", (command_id,)).fetchone()
        if row is None:
            raise MarketJobRepositoryError(f"market worker command not found: {command_id}")
        return row

    @staticmethod
    def _require_lease_owner(
        operation: sqlite3.Row,
        worker_id: str,
        now: str,
        *,
        attempt_id: str | None = None,
        lease_fence: int | None = None,
    ) -> None:
        deadline = operation["lease_deadline"]
        if (
            operation["lease_owner"] != worker_id
            or operation["state"] not in {OperationState.LEASED.value, OperationState.RUNNING.value}
            or deadline is None
            or deadline <= now
            or (attempt_id is not None and operation["leased_attempt_id"] != attempt_id)
            or (lease_fence is not None and int(operation["lease_fence"]) != lease_fence)
        ):
            raise MarketOperationLeaseLostError(
                f"worker {worker_id} does not own the current fenced lease for operation {operation['operation_id']}"
            )

    @staticmethod
    def _clear_worker_operation_locked(
        connection: sqlite3.Connection,
        worker_id: str,
        operation_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE market_workers
            SET current_operation_id = NULL, heartbeat_at = ?, updated_at = ?
            WHERE worker_id = ? AND current_operation_id = ?
            """,
            (now, now, worker_id, operation_id),
        )

    @staticmethod
    def _merge_counter_values(
        current: Mapping[str, Any],
        deltas: Mapping[str, int | float] | None,
    ) -> JsonDict:
        merged: JsonDict = dict(current)
        if deltas is None:
            return merged
        for key, delta in deltas.items():
            if isinstance(delta, bool) or not isinstance(delta, (int, float)):
                raise TypeError(f"counter delta {key!r} must be a number")
            existing = merged.get(key, 0)
            if isinstance(existing, bool) or not isinstance(existing, (int, float)):
                existing = 0
            merged[str(key)] = existing + delta
        return merged

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset cannot be negative")

    @staticmethod
    def _state_filter_values(
        state: str | Enum | Iterable[str | Enum] | None,
    ) -> tuple[str, ...]:
        if state is None:
            return ()
        if isinstance(state, (str, Enum)):
            values = (state,)
        else:
            values = tuple(state)
        return tuple(str(_enum_value(value)) for value in values)

    @staticmethod
    def _cursor_text(cursor: str | None) -> str | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str):
            raise TypeError("cursor must be a string")
        normalized = cursor.strip()
        if not normalized:
            raise ValueError("cursor cannot be blank")
        return normalized

    @staticmethod
    def _listing_id_cursor(value: int | str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool):
            raise TypeError("listing cursor must be an integer")
        try:
            identifier = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("listing cursor must be an integer") from error
        minimum = 0 if allow_zero else 1
        if identifier < minimum:
            raise ValueError(f"listing cursor must be at least {minimum}")
        return identifier

    @staticmethod
    def _seller_key(listing: Mapping[str, Any]) -> str | None:
        for key in (
            "seller_key",
            "seller",
            "username",
            "userName",
            "user_name",
            "seller_name",
            "seller_id",
            "user_id",
            "userId",
        ):
            value = _optional_text(listing.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _shard_values(shard: ShardSpec | Mapping[str, Any]) -> JsonDict:
        data = asdict(shard) if is_dataclass(shard) else dict(shard)
        shard_id = _optional_text(data.get("shard_id"))
        job_id = _optional_text(data.get("job_id"))
        source = _optional_text(data.get("source"))
        alias = _optional_text(data.get("alias"))
        if not all((shard_id, job_id, source, alias)):
            raise ValueError("shard_id, job_id, source, and alias are required")
        filters = data.get("filters", {})
        if not isinstance(filters, Mapping):
            raise TypeError("shard filters must be a mapping")
        cursor = data.get("cursor")
        if cursor is not None and not isinstance(cursor, Mapping):
            cursor = _jsonable(cursor)
        return {
            "shard_id": shard_id,
            "job_id": job_id,
            "source": source,
            "alias": alias,
            "filters": filters,
            "expected_count": data.get("expected_count"),
            "state": _enum_value(data.get("state", "queued")),
            "priority": int(data.get("priority", 0)),
            "cursor": cursor,
        }

    @staticmethod
    def _operation_values(operation: Operation | Mapping[str, Any]) -> JsonDict:
        data = asdict(operation) if is_dataclass(operation) else dict(operation)
        operation_id = _optional_text(data.get("operation_id")) or _new_identifier("op")
        job_id = _optional_text(data.get("job_id"))
        kind = _optional_text(_enum_value(data.get("kind")))
        if job_id is None or kind is None:
            raise ValueError("operation job_id and kind are required")
        payload = data.get("payload", {})
        if not isinstance(payload, Mapping):
            raise TypeError("operation payload must be a mapping")
        return {
            "operation_id": operation_id,
            "job_id": job_id,
            "shard_id": _optional_text(data.get("shard_id")),
            "kind": kind,
            "state": _enum_value(data.get("state", OperationState.QUEUED.value)),
            "priority": int(data.get("priority", 0)),
            "idempotency_key": _optional_text(data.get("idempotency_key")),
            "payload": payload,
            "not_before": data.get("not_before"),
            "lease_owner": data.get("lease_owner"),
            "lease_deadline": data.get("lease_deadline"),
            "current_attempt": int(data.get("current_attempt", 0)),
        }

    @staticmethod
    def _job_record(row: sqlite3.Row) -> JsonDict:
        raw_account_ids = _load_json(row["account_registration_ids_json"], [])
        account_registration_ids = (
            [str(value).strip() for value in raw_account_ids if isinstance(value, str) and str(value).strip()]
            if isinstance(raw_account_ids, list)
            else []
        )
        return {
            "job_id": row["job_id"],
            "scope": {
                "category_id": int(row["category_id"]),
                "category_name": row["category_name"],
                "classifier_id": row["classifier_id"],
                "classifier_name": row["classifier_name"],
                "canonical_alias": row["canonical_alias"],
                "filters": _load_json(row["scope_filters_json"], {}),
            },
            "profile": row["profile"],
            "target_unique_cards": int(row["target_unique_cards"]),
            "desired_workers": int(row["desired_workers"]),
            "account_registration_ids": account_registration_ids,
            "job_kind": row["job_kind"],
            "workflow_config": _load_json(row["config_json"], {}),
            "network_policy": row["network_policy"],
            "source_policy": row["source_policy"],
            "include_ai": bool(row["include_ai"]),
            "state": row["state"],
            "phase": row["phase"],
            "revision": int(row["revision"]),
            "request_budget": row["request_budget"],
            "time_budget_seconds": row["time_budget_seconds"],
            "counters": _load_json(row["counters_json"], {}),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "latest_checkpoint_id": row["latest_checkpoint_id"],
            "last_error": row["last_error"],
            "last_warning": row["last_warning"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _shard_record(row: sqlite3.Row) -> JsonDict:
        return {
            "shard_id": row["shard_id"],
            "job_id": row["job_id"],
            "source": row["source"],
            "alias": row["alias"],
            "filters": _load_json(row["filters_json"], {}),
            "expected_count": row["expected_count"],
            "state": row["state"],
            "priority": int(row["priority"]),
            "cursor": _load_json(row["cursor_json"], None),
            "last_fingerprint": row["last_fingerprint"],
            "counters": _load_json(row["counters_json"], {}),
            "received_count": int(row["received_count"]),
            "new_unique_count": int(row["new_unique_count"]),
            "duplicate_count": int(row["duplicate_count"]),
            "consecutive_zero_novelty": int(row["consecutive_zero_novelty"]),
            "cooldown_until": row["cooldown_until"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _category_alias_record(row: sqlite3.Row) -> JsonDict:
        return {
            "category_id": int(row["category_id"]),
            "canonical_alias": row["canonical_alias"],
            "validation_status": row["validation_status"],
            "active_category_id": row["active_category_id"],
            "source_url": row["source_url"],
            "last_validated_at": row["last_validated_at"],
            "protection_state": row["protection_state"],
            "schema_version": int(row["schema_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _recommendation_record(row: sqlite3.Row) -> JsonDict:
        return {
            "recommendation_id": row["recommendation_id"],
            "job_id": row["job_id"],
            "source_cluster_id": row["source_cluster_id"],
            "state": row["state"],
            "category_id": int(row["category_id"]),
            "classifier_id": row["classifier_id"],
            "service_summary": row["service_summary"],
            "price": int(row["price"]),
            "work_time": int(row["work_time"]),
            "evidence_ids": _load_json(row["evidence_ids_json"], []),
            "terra_result": _load_json(row["terra_result_json"], {}),
            "content_hash": row["content_hash"],
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _draft_handoff_record(row: sqlite3.Row) -> JsonDict:
        return {
            "handoff_id": row["handoff_id"],
            "job_id": row["job_id"],
            "recommendation_id": row["recommendation_id"],
            "state": row["state"],
            "category_id": int(row["category_id"]),
            "classifier_id": row["classifier_id"],
            "service_summary": row["service_summary"],
            "price": int(row["price"]),
            "work_time": int(row["work_time"]),
            "attribute_manifest": _load_json(row["manifest_json"], {}),
            "attribute_manifest_hash": row["manifest_hash"],
            "attribute_selection": _load_json(row["selection_json"], {}),
            "selection_hash": row["selection_hash"],
            "validation": _load_json(row["validation_json"], {}),
            "generator_request": _load_json(row["generator_request_json"], {}),
            "draft": _load_json(row["draft_json"], {}),
            "draft_hash": row["draft_hash"],
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _published_listing_record(row: sqlite3.Row) -> JsonDict:
        return {
            "published_listing_id": row["published_listing_id"],
            "job_id": row["job_id"],
            "recommendation_id": row["recommendation_id"],
            "handoff_id": row["handoff_id"],
            "source_cluster_id": row["source_cluster_id"],
            "kwork_id": row["kwork_id"],
            "draft_hash": row["draft_hash"],
            "publish_result": _load_json(row["publish_result_json"], {}),
            "feedback": _load_json(row["feedback_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _operation_record(row: sqlite3.Row) -> JsonDict:
        return {
            "operation_id": row["operation_id"],
            "job_id": row["job_id"],
            "shard_id": row["shard_id"],
            "kind": row["kind"],
            "state": row["state"],
            "priority": int(row["priority"]),
            "idempotency_key": row["idempotency_key"],
            "payload": _load_json(row["payload_json"], {}),
            "not_before": row["not_before"],
            "lease_owner": row["lease_owner"],
            "lease_deadline": row["lease_deadline"],
            "lease_fence": int(row["lease_fence"]),
            "leased_attempt_id": row["leased_attempt_id"],
            "current_attempt": int(row["current_attempt"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "last_error": row["last_error"],
        }

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> JsonDict:
        return {
            "attempt_id": row["attempt_id"],
            "operation_id": row["operation_id"],
            "attempt_number": int(row["attempt_number"]),
            "state": row["state"],
            "worker_id": row["worker_id"],
            "transport_id": row["transport_id"],
            "requested_cursor": _load_json(row["requested_cursor_json"], None),
            "reported_cursor": _load_json(row["reported_cursor_json"], None),
            "request": _load_json(row["request_json"], None),
            "response_status": row["response_status"],
            "response_bytes": row["response_bytes"],
            "duration_ms": row["duration_ms"],
            "received_count": int(row["received_count"]),
            "new_unique_count": int(row["new_unique_count"]),
            "duplicate_count": int(row["duplicate_count"]),
            "page_fingerprint": row["page_fingerprint"],
            "raw_response_ref": row["raw_response_ref"],
            "failure_kind": row["failure_kind"],
            "retry_after": row["retry_after"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
        }

    @staticmethod
    def _listing_record(row: sqlite3.Row) -> JsonDict:
        return {
            "listing_id": int(row["listing_id"]),
            "job_id": row["job_id"],
            "listing_key": row["listing_key"],
            "title": row["title"],
            "seller_key": row["seller_key"],
            "price": row["price"],
            "canonical": _load_json(row["canonical_json"], {}),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "observation_count": int(row["observation_count"]),
        }

    @staticmethod
    def _listing_feature_record(row: sqlite3.Row) -> JsonDict:
        return {
            "job_id": row["job_id"],
            "listing_id": int(row["listing_id"]),
            "seller_key": row["seller_key"],
            "resolved_seller_key": row["resolved_seller_key"],
            "status": row["status"],
            "generation": int(row["generation"]),
            "detail": _load_json(row["detail_json"], {}),
            "extra": _load_json(row["extra_json"], {}),
            "description": row["description"],
            "instructions": row["instructions"],
            "service_size": row["service_size"],
            "queue_count": row["queue_count"],
            "work_time_seconds": row["work_time_seconds"],
            "listing_reviews_count": row["listing_reviews_count"],
            "good_reviews": row["good_reviews"],
            "bad_reviews": row["bad_reviews"],
            "last_review_at": row["last_review_at"],
            "fetched_at": row["fetched_at"],
            "expires_at": row["expires_at"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _seller_feature_record(row: sqlite3.Row) -> JsonDict:
        return {
            "job_id": row["job_id"],
            "seller_key": row["seller_key"],
            "seller_id": row["seller_id"],
            "status": row["status"],
            "profile": _load_json(row["profile_json"], {}),
            "seller_rating": row["seller_rating"],
            "seller_rating_count": row["seller_rating_count"],
            "seller_reviews_count": row["seller_reviews_count"],
            "seller_addtime": row["seller_addtime"],
            "completed_orders_count": row["completed_orders_count"],
            "active_kworks_count": row["active_kworks_count"],
            "fetched_at": row["fetched_at"],
            "expires_at": row["expires_at"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _listing_review_record(row: sqlite3.Row) -> JsonDict:
        return {
            "review_key": row["review_key"],
            "time_added": row["time_added"],
            "is_good": bool(row["is_good"]) if row["is_good"] is not None else None,
            "is_bad": bool(row["is_bad"]) if row["is_bad"] is not None else None,
            "text": row["review_text"],
            "writer": row["writer"],
            "answer": row["answer"],
            "raw": _load_json(row["raw_json"], {}),
            "observed_at": row["observed_at"],
        }

    @staticmethod
    def _observation_record(row: sqlite3.Row) -> JsonDict:
        return {
            "observation_id": int(row["observation_id"]),
            "job_id": row["job_id"],
            "listing_id": int(row["listing_id"]),
            "operation_id": row["operation_id"],
            "attempt_id": row["attempt_id"],
            "source": row["source"],
            "shard_id": row["shard_id"],
            "response_position": int(row["response_position"]),
            "requested_cursor": _load_json(row["requested_cursor_json"], None),
            "reported_cursor": _load_json(row["reported_cursor_json"], None),
            "raw_response_ref": row["raw_response_ref"],
            "observed_at": row["observed_at"],
        }

    @staticmethod
    def _event_record(row: sqlite3.Row) -> JsonDict:
        return {
            "job_id": row["job_id"],
            "sequence": int(row["sequence"]),
            "event_type": row["event_type"],
            "payload": _load_json(row["payload_json"], {}),
            "revision": row["revision"],
            "worker_id": row["worker_id"],
            "operation_id": row["operation_id"],
            "emitted_at": row["emitted_at"],
        }

    @staticmethod
    def _worker_record(row: sqlite3.Row) -> JsonDict:
        return {
            "worker_id": row["worker_id"],
            "job_id": row["job_id"],
            "generation": int(row["generation"]),
            "desired_state": row["desired_state"],
            "actual_state": row["actual_state"],
            "runtime_kind": row["runtime_kind"],
            "transport_id": row["transport_id"],
            "current_operation_id": row["current_operation_id"],
            "heartbeat_at": row["heartbeat_at"],
            "counters": _load_json(row["counters_json"], {}),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _market_account_inventory_record(row: sqlite3.Row) -> JsonDict:
        return {
            "registration_id": row["registration_id"],
            "username": row["username"],
            "email": row["email"],
            "status": row["status"],
            "market_enabled": bool(int(row["market_enabled"])),
            "session_cookie_count": int(row["session_cookie_count"]),
            "signup_ip": row["signup_ip"],
            "preferred_slot": row["preferred_slot"],
            "preferred_transport_id": row["preferred_transport_id"],
            "persona_id": row["persona_id"],
            "last_used_at": row["last_used_at"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _market_identity_binding_record(row: sqlite3.Row) -> JsonDict:
        return {
            "worker_id": row["worker_id"],
            "job_id": row["job_id"],
            "registration_id": row["registration_id"],
            "transport_id": row["transport_id"],
            "current_egress_ip": row["current_egress_ip"],
            "binding_mode": row["binding_mode"],
            "state": row["state"],
            "lease_token": row["lease_token"],
            "lease_deadline": row["lease_deadline"],
            "assigned_at": row["assigned_at"],
            "released_at": row["released_at"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _transport_record(row: sqlite3.Row) -> JsonDict:
        return {
            "transport_id": row["transport_id"],
            "kind": row["kind"],
            "health": row["health"],
            "slot": row["slot"],
            "proxy_url": row["proxy_url"],
            "profile_id": row["profile_id"],
            "profile_name": row["profile_name"],
            "country": row["country"],
            "pid": row["pid"],
            "generation": int(row["generation"]),
            "lease_owner": row["lease_owner"],
            "quarantine_until": row["quarantine_until"],
            "last_rotate_reason": row["last_rotate_reason"],
            "egress_ip": row["egress_ip"],
            "egress_checked_at": row["egress_checked_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _checkpoint_record(row: sqlite3.Row) -> JsonDict:
        return {
            "checkpoint_id": row["checkpoint_id"],
            "job_id": row["job_id"],
            "revision": int(row["revision"]),
            "phase": row["phase"],
            "frontier": _load_json(row["frontier_json"], {}),
            "metrics": _load_json(row["metrics_json"], {}),
            "last_event_sequence": int(row["last_event_sequence"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _worker_command_record(row: sqlite3.Row) -> JsonDict:
        return {
            "command_id": row["command_id"],
            "job_id": row["job_id"],
            "worker_id": row["worker_id"],
            "command_type": row["command_type"],
            "payload": _load_json(row["payload_json"], {}),
            "state": row["state"],
            "created_at": row["created_at"],
            "acknowledged_at": row["acknowledged_at"],
            "completed_at": row["completed_at"],
            "error": row["error"],
        }


SqliteMarketJobRepository = MarketJobRepository


__all__ = [
    "MarketCommitConflictError",
    "MarketJobNotFoundError",
    "MarketJobRepository",
    "MarketJobRepositoryError",
    "MarketJobRevisionConflictError",
    "MarketOperationLeaseError",
    "MarketOperationNotFoundError",
    "SqliteMarketJobRepository",
]
