"""Durable SQLite storage for the Buyer Search bounded context.

The repository deliberately accepts plain mappings.  The domain models and
FastAPI schemas can evolve independently while the durable contract remains
stable.  It does not import or modify ``kwork_supply``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]


class BuyerSearchRepositoryError(RuntimeError):
    """Base exception for Buyer Search persistence failures."""


class BuyerRunNotFoundError(BuyerSearchRepositoryError):
    """Raised when a requested Buyer Search run does not exist."""


class BuyerQueryTaskLeaseLostError(BuyerSearchRepositoryError):
    """Raised when a task commit no longer owns its current lease fence."""


class BuyerRepositoryValidationError(BuyerSearchRepositoryError):
    """Raised for incomplete or invalid durable Buyer Search input."""


_SECRET_MARKERS = ("authorization", "cookie", "csrf", "password", "secret", "token", "credential")
_SAFE_HEADERS = {"content-type", "content-length", "etag", "last-modified", "retry-after", "x-request-id"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json(value: Any, *, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _from_json(value: Any, *, default: Any) -> Any:
    if not isinstance(value, str) or not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _filter_hash(filter_json: str) -> str:
    return hashlib.sha256(filter_json.encode("utf-8")).hexdigest()


def _discovery_endpoint(value: Any, *, fallback: str) -> str:
    """Return a stable endpoint identity without query/fragment credentials."""

    endpoint = _text(value) or _text(fallback)
    endpoint = endpoint.split("?", 1)[0].split("#", 1)[0].strip()
    if not endpoint:
        raise BuyerRepositoryValidationError("discovery endpoint is required")
    if len(endpoint) > 512:
        raise BuyerRepositoryValidationError("discovery endpoint cannot exceed 512 characters")
    return endpoint


def _cursor_encode(payload: Mapping[str, Any]) -> str:
    raw = _json(dict(payload), default={}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(cursor: str | None) -> JsonDict | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise BuyerRepositoryValidationError("invalid cursor") from None
    return dict(value) if isinstance(value, Mapping) else None


def _redact(value: Any, *, key: str = "") -> Any:
    key_l = key.lower()
    if any(marker in key_l for marker in _SECRET_MARKERS):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _safe_headers(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _redact(item, key=str(key))
        for key, item in value.items()
        if str(key).lower() in _SAFE_HEADERS
    }


def _normalize_committed_card(value: Mapping[str, Any]) -> JsonDict:
    """Accept either a legacy source card or the mapper's canonical/evidence pair."""

    if not isinstance(value, Mapping):
        raise BuyerRepositoryValidationError("committed projects must contain mappings")
    canonical = value.get("canonical")
    observation = value.get("observation")
    if not isinstance(canonical, Mapping) or not isinstance(observation, Mapping):
        return dict(value)
    remote_project_id = _text(canonical.get("remote_project_id") or observation.get("remote_project_id"))
    if not remote_project_id:
        raise BuyerRepositoryValidationError("mapped project requires remote_project_id")
    card: JsonDict = dict(canonical)
    card.update(
        {
            "id": remote_project_id,
            "remote_project_id": remote_project_id,
            "title": observation.get("title") or canonical.get("latest_title"),
            "description": observation.get("description") or canonical.get("latest_description"),
            "status": observation.get("remote_status") or canonical.get("latest_status"),
            "category_id": observation.get("category_id") or canonical.get("latest_category_id"),
            "budget_min": observation.get("budget_min"),
            "budget_max": observation.get("budget_max"),
            "offers": observation.get("offers"),
            "views": observation.get("views"),
            "orders": observation.get("orders"),
            "parent_category_id": observation.get("parent_category_id"),
            "buyer_hired_percent": observation.get("buyer_hired_percent"),
            "buyer_projects_count": observation.get("buyer_projects_count"),
            "buyer_active_projects_count": observation.get("buyer_active_projects_count"),
            "remote_updated_at": canonical.get("latest_remote_updated_at"),
        }
    )
    return card


def _non_negative_integer_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BuyerRepositoryValidationError("value must be a non-negative integer")
    if isinstance(value, float) and not value.is_integer():
        raise BuyerRepositoryValidationError("value must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerRepositoryValidationError("value must be a non-negative integer") from exc
    if parsed < 0:
        raise BuyerRepositoryValidationError("value must be a non-negative integer")
    return parsed


def _filter_number(value: Any, name: str) -> float:
    number = _number(value)
    if number is None or not math.isfinite(number):
        raise BuyerRepositoryValidationError(f"{name} must be a finite number")
    return number


def _filter_text_values(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise BuyerRepositoryValidationError(f"{name} must be a string or array of strings")
    normalized = tuple(_text(item) for item in values if _text(item))
    return tuple(dict.fromkeys(normalized))


def _normalized_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        raise BuyerRepositoryValidationError("tags must be a string or array of strings")
    tags: list[str] = []
    seen: set[str] = set()
    for item in values:
        tag = _text(item)
        if not tag:
            continue
        if len(tag) > 80:
            raise BuyerRepositoryValidationError("shortlist tag cannot exceed 80 characters")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tuple(tags)


def _shortlist_note_payloads(payload: Mapping[str, Any], default_author: str | None) -> tuple[JsonDict, ...]:
    notes: list[JsonDict] = []
    if "note" in payload:
        raw_note = payload.get("note")
        if isinstance(raw_note, Mapping):
            notes.append(dict(raw_note))
        elif raw_note is not None:
            notes.append({"body": raw_note, "author": default_author})
    if "notes" in payload:
        raw_notes = payload.get("notes")
        if isinstance(raw_notes, Mapping):
            notes.append(dict(raw_notes))
        elif isinstance(raw_notes, Sequence) and not isinstance(raw_notes, (str, bytes, bytearray)):
            for raw_note in raw_notes:
                if isinstance(raw_note, Mapping):
                    notes.append(dict(raw_note))
                elif raw_note is not None:
                    notes.append({"body": raw_note, "author": default_author})
        elif raw_notes is not None:
            raise BuyerRepositoryValidationError("notes must be an array")
    return tuple(notes)


def _progress_payload(value: Any) -> JsonDict:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(_redact(value))
    if isinstance(value, bool):
        raise BuyerRepositoryValidationError("export progress must be an object or number")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return {"completed": float(value)}
    raise BuyerRepositoryValidationError("export progress must be an object or number")


def _score_kind(value: Any, payload: Mapping[str, Any]) -> str:
    candidate = _text(value).casefold()
    if not candidate:
        candidate = "final" if _text(payload.get("provider")) or _text(payload.get("model")) else "preliminary"
    aliases = {
        "deterministic": "preliminary",
        "preliminary": "preliminary",
        "ai": "final",
        "final": "final",
    }
    normalized = aliases.get(candidate)
    if normalized is None:
        raise BuyerRepositoryValidationError("score_kind must be preliminary or final")
    return normalized


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS buyer_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    brief TEXT,
    category_scope_json TEXT NOT NULL DEFAULT '[]',
    filter_json TEXT NOT NULL DEFAULT '{}',
    requested_workers INTEGER NOT NULL DEFAULT 1,
    query_batch_size INTEGER NOT NULL DEFAULT 1,
    target_unique_projects INTEGER,
    enrichment_policy TEXT NOT NULL DEFAULT 'none',
    scoring_profile_id TEXT,
    state TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT,
    config_version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    counters_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    last_failure_kind TEXT,
    last_task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    stopped_at TEXT
);
CREATE INDEX IF NOT EXISTS buyer_runs_state_created_idx ON buyer_runs(state, created_at DESC);

CREATE TABLE IF NOT EXISTS buyer_queries (
    query_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    origin TEXT NOT NULL,
    parent_query_id TEXT,
    rationale TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    category_id TEXT NOT NULL DEFAULT '',
    category_path TEXT,
    filter_json TEXT NOT NULL DEFAULT '{}',
    filter_hash TEXT NOT NULL,
    semantic_fingerprint TEXT,
    predicted_total INTEGER,
    approved INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft',
    pages_scheduled INTEGER NOT NULL DEFAULT 0,
    pages_completed INTEGER NOT NULL DEFAULT 0,
    projects_seen INTEGER NOT NULL DEFAULT 0,
    unique_projects INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, category_id, normalized_text, filter_hash)
);
CREATE INDEX IF NOT EXISTS buyer_queries_run_state_priority_idx ON buyer_queries(run_id, state, priority DESC, query_id);
CREATE INDEX IF NOT EXISTS buyer_queries_fingerprint_idx ON buyer_queries(run_id, semantic_fingerprint);

CREATE TABLE IF NOT EXISTS buyer_query_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES buyer_queries(query_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '',
    page INTEGER,
    cursor_json TEXT,
    request_fingerprint TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    not_before TEXT,
    lease_owner TEXT,
    lease_deadline TEXT,
    lease_fence INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT,
    account_registration_id TEXT,
    transport_id TEXT,
    egress_ip TEXT,
    route_generation INTEGER,
    response_artifact_id TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    total_hint INTEGER,
    latency_ms INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    error_text TEXT,
    commit_result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, request_fingerprint)
);
CREATE INDEX IF NOT EXISTS buyer_query_tasks_lease_idx ON buyer_query_tasks(run_id, state, not_before, priority DESC, task_id);
CREATE INDEX IF NOT EXISTS buyer_query_tasks_query_idx ON buyer_query_tasks(query_id, state);

CREATE TABLE IF NOT EXISTS buyer_discovery_quarantines (
    quarantine_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    account_registration_id TEXT NOT NULL,
    transport_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    failure_kind TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 1,
    last_task_id TEXT NOT NULL REFERENCES buyer_query_tasks(task_id) ON DELETE CASCADE,
    last_attempt_id TEXT NOT NULL,
    last_lease_fence INTEGER NOT NULL,
    last_worker_id TEXT NOT NULL,
    last_egress_ip TEXT,
    last_route_generation INTEGER,
    state TEXT NOT NULL DEFAULT 'active',
    expires_at TEXT NOT NULL,
    released_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, account_registration_id, transport_id, endpoint)
);
CREATE INDEX IF NOT EXISTS buyer_discovery_quarantines_active_idx
    ON buyer_discovery_quarantines(run_id, account_registration_id, transport_id, endpoint, state, expires_at);
CREATE INDEX IF NOT EXISTS buyer_discovery_quarantines_identity_idx
    ON buyer_discovery_quarantines(account_registration_id, transport_id, endpoint, state, expires_at);

CREATE TABLE IF NOT EXISTS buyer_projects (
    buyer_project_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    remote_project_id TEXT NOT NULL,
    canonical_url TEXT,
    latest_title TEXT,
    latest_description TEXT,
    latest_status TEXT,
    latest_category_id TEXT,
    buyer_remote_user_id TEXT,
    buyer_username TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    latest_remote_updated_at TEXT,
    canonical_hash TEXT,
    UNIQUE(platform, remote_project_id)
);
CREATE INDEX IF NOT EXISTS buyer_projects_updated_idx ON buyer_projects(last_seen_at DESC, buyer_project_id);

CREATE TABLE IF NOT EXISTS buyer_raw_artifacts (
    artifact_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT,
    request_fingerprint TEXT,
    account_registration_id TEXT,
    transport_id TEXT,
    egress_ip TEXT,
    route_generation INTEGER,
    status_code INTEGER,
    headers_json TEXT NOT NULL DEFAULT '{}',
    body_json TEXT,
    content_type TEXT,
    sha256 TEXT NOT NULL,
    parser_version TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buyer_raw_artifacts_request_idx ON buyer_raw_artifacts(request_fingerprint, observed_at DESC);

CREATE TABLE IF NOT EXISTS buyer_project_enrichments (
    enrichment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    endpoint TEXT,
    raw_artifact_id TEXT REFERENCES buyer_raw_artifacts(artifact_id) ON DELETE SET NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    error TEXT,
    account_registration_id TEXT,
    transport_id TEXT,
    egress_ip TEXT,
    route_generation INTEGER,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, project_id, kind, content_hash)
);
CREATE INDEX IF NOT EXISTS buyer_project_enrichments_run_project_idx
    ON buyer_project_enrichments(run_id, project_id, observed_at DESC, enrichment_id);
CREATE INDEX IF NOT EXISTS buyer_project_enrichments_artifact_idx
    ON buyer_project_enrichments(raw_artifact_id);

CREATE TABLE IF NOT EXISTS buyer_project_observations (
    observation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES buyer_queries(query_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES buyer_query_tasks(task_id) ON DELETE CASCADE,
    attempt_id TEXT,
    worker_id TEXT,
    account_registration_id TEXT,
    transport_id TEXT,
    egress_ip TEXT,
    route_generation INTEGER,
    source TEXT NOT NULL,
    page INTEGER,
    response_position INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    title TEXT,
    description TEXT,
    budget_min REAL,
    budget_max REAL,
    offers INTEGER,
    views INTEGER,
    orders INTEGER,
    remote_status TEXT,
    category_id TEXT,
    parent_category_id TEXT,
    buyer_hired_percent REAL,
    buyer_projects_count INTEGER,
    buyer_active_projects_count INTEGER,
    expires_at TEXT,
    raw_artifact_id TEXT REFERENCES buyer_raw_artifacts(artifact_id),
    normalized_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS buyer_observations_run_project_idx ON buyer_project_observations(run_id, project_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS buyer_observations_task_idx ON buyer_project_observations(task_id);

CREATE TABLE IF NOT EXISTS buyer_project_matches (
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL REFERENCES buyer_queries(query_id) ON DELETE CASCADE,
    first_observation_id TEXT NOT NULL REFERENCES buyer_project_observations(observation_id),
    last_observation_id TEXT NOT NULL REFERENCES buyer_project_observations(observation_id),
    match_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(run_id, project_id, query_id)
);

CREATE TABLE IF NOT EXISTS buyer_run_projects (
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    latest_observation_id TEXT NOT NULL REFERENCES buyer_project_observations(observation_id),
    title TEXT,
    description_excerpt TEXT,
    budget_min REAL,
    budget_max REAL,
    offers INTEGER,
    views INTEGER,
    age_seconds INTEGER,
    category_id TEXT,
    category_path TEXT,
    buyer_hired_percent REAL,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    attachment_parse_state TEXT,
    preliminary_score REAL,
    final_score REAL,
    matched_query_count INTEGER NOT NULL DEFAULT 1,
    shortlist_state TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    proposal_state TEXT,
    conversation_state TEXT,
    unseen INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, project_id)
);
CREATE INDEX IF NOT EXISTS buyer_run_projects_updated_idx ON buyer_run_projects(run_id, updated_at DESC, project_id);

CREATE TABLE IF NOT EXISTS buyer_attachments (
    attachment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    source_observation_id TEXT REFERENCES buyer_project_observations(observation_id) ON DELETE SET NULL,
    remote_url TEXT,
    resolved_download_url TEXT,
    filename TEXT,
    content_type TEXT,
    detected_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    object_ref TEXT,
    state TEXT NOT NULL DEFAULT 'discovered',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    downloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS buyer_attachments_project_idx ON buyer_attachments(project_id, created_at DESC, attachment_id);
CREATE INDEX IF NOT EXISTS buyer_attachments_project_state_idx ON buyer_attachments(project_id, state, detected_type);
CREATE UNIQUE INDEX IF NOT EXISTS buyer_attachments_project_remote_url_idx
    ON buyer_attachments(project_id, remote_url) WHERE remote_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS buyer_attachment_derivatives (
    derivative_id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES buyer_attachments(attachment_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    parser_name TEXT,
    parser_version TEXT,
    model TEXT,
    model_version TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    extracted_text TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    token_count INTEGER,
    state TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(attachment_id, kind, content_hash)
);
CREATE INDEX IF NOT EXISTS buyer_attachment_derivatives_attachment_idx
    ON buyer_attachment_derivatives(attachment_id, state, created_at DESC, derivative_id);

CREATE TABLE IF NOT EXISTS buyer_scores (
    score_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    score_kind TEXT NOT NULL,
    score_profile_id TEXT NOT NULL DEFAULT '',
    score_profile_version TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    input_context_hash TEXT NOT NULL DEFAULT '',
    total_score REAL NOT NULL,
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    rationale TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(
        run_id, project_id, score_kind, score_profile_id, score_profile_version,
        provider, model, prompt_version, input_context_hash
    )
);
CREATE INDEX IF NOT EXISTS buyer_scores_run_project_kind_idx
    ON buyer_scores(run_id, project_id, score_kind, created_at DESC, score_id);

CREATE TABLE IF NOT EXISTS buyer_shortlist_items (
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES buyer_projects(buyer_project_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    rank INTEGER,
    selected_by TEXT,
    selected_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, project_id)
);
CREATE INDEX IF NOT EXISTS buyer_shortlist_items_run_state_rank_idx
    ON buyer_shortlist_items(run_id, state, rank, selected_at DESC, project_id);

CREATE TABLE IF NOT EXISTS buyer_shortlist_tags (
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, project_id, tag),
    FOREIGN KEY(run_id, project_id) REFERENCES buyer_shortlist_items(run_id, project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS buyer_shortlist_tags_run_tag_idx ON buyer_shortlist_tags(run_id, tag, project_id);

CREATE TABLE IF NOT EXISTS buyer_shortlist_notes (
    note_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id, project_id) REFERENCES buyer_shortlist_items(run_id, project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS buyer_shortlist_notes_run_project_idx
    ON buyer_shortlist_notes(run_id, project_id, created_at, note_id);

CREATE TABLE IF NOT EXISTS buyer_exports (
    export_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    selection_json TEXT NOT NULL DEFAULT '{}',
    format TEXT NOT NULL,
    include_attachments INTEGER NOT NULL DEFAULT 0,
    include_raw INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    progress_json TEXT NOT NULL DEFAULT '{}',
    object_ref TEXT,
    filename TEXT,
    content_type TEXT,
    bytes INTEGER,
    manifest_json TEXT,
    manifest_hash TEXT,
    sha256 TEXT,
    error TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS buyer_exports_run_created_idx ON buyer_exports(run_id, created_at DESC, export_id);
CREATE INDEX IF NOT EXISTS buyer_exports_run_state_idx ON buyer_exports(run_id, state, updated_at DESC, export_id);

CREATE TABLE IF NOT EXISTS buyer_events (
    run_id TEXT NOT NULL REFERENCES buyer_runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS buyer_workspace_state (
    workspace_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""


class BuyerSearchRepository:
    """SQLite repository for durable Buyer Search data and event replay."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._initialized = False
        self._initialization_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def close(self) -> None:
        # Connections are short lived, so there is no persistent handle to close.
        return None

    async def create_run(self, run: Mapping[str, Any]) -> JsonDict:
        payload = dict(run)
        return await self._call(self._create_run_sync, payload)

    async def get_run(self, run_id: str) -> JsonDict:
        return await self._call(self._get_run_sync, _text(run_id))

    async def list_runs(
        self,
        *,
        states: Sequence[str] | None = None,
        state: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[JsonDict]:
        selected = tuple(str(item) for item in states or (() if state is None else (state,)))
        return await self._call(self._list_runs_sync, selected, int(limit), cursor)

    async def update_run(self, run_id: str, changes: Mapping[str, Any]) -> JsonDict:
        return await self._call(self._update_run_sync, _text(run_id), dict(changes))

    async def delete_run(self, run_id: str) -> JsonDict:
        """Delete one terminal run and its run-scoped durable evidence."""

        return await self._call(self._delete_run_sync, _text(run_id))

    async def complete_run_if_exhausted(self, run_id: str) -> JsonDict | None:
        """Atomically mark a running run complete after its task queue drains."""

        return await self._call(self._complete_run_if_exhausted_sync, _text(run_id))

    async def requeue_malformed_response_tasks(self, run_id: str, *, limit: int = 100) -> JsonDict:
        """Retry failed pages whose response did not contain a project card."""

        return await self._call(
            self._requeue_malformed_response_tasks_sync,
            _text(run_id),
            max(1, min(int(limit), 500)),
        )

    async def reconcile_task_counters(self, run_id: str) -> JsonDict:
        """Rebuild task-state counters so retries do not inflate real errors."""

        return await self._call(self._reconcile_task_counters_sync, _text(run_id))

    async def create_queries(self, run_id: str, queries: Sequence[Mapping[str, Any]]) -> list[JsonDict]:
        return await self._call(self._create_queries_sync, _text(run_id), [dict(query) for query in queries])

    async def list_queries(
        self,
        run_id: str,
        *,
        states: Sequence[str] | None = None,
        state: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> list[JsonDict]:
        selected = tuple(str(item) for item in states or (() if state is None else (state,)))
        return await self._call(self._list_queries_sync, _text(run_id), selected, int(limit), cursor)

    async def update_query(self, run_id: str, query_id: str, changes: Mapping[str, Any]) -> JsonDict:
        return await self._call(self._update_query_sync, _text(run_id), _text(query_id), dict(changes))

    async def list_query_coverage(self, run_id: str) -> list[JsonDict]:
        """Return the durable project-ID coverage for each query in a run."""

        return await self._call(self._list_query_coverage_sync, _text(run_id))

    async def compact_query_plan(self, run_id: str, retain_query_ids: Sequence[str]) -> JsonDict:
        """Disable redundant query rows and cancel their pending page tasks."""

        retained = tuple(dict.fromkeys(_text(query_id) for query_id in retain_query_ids if _text(query_id)))
        return await self._call(self._compact_query_plan_sync, _text(run_id), retained)

    async def list_query_tasks(
        self,
        run_id: str,
        *,
        query_id: str | None = None,
        states: Sequence[str] | None = None,
        state: str | None = None,
        limit: int = 1_000,
    ) -> list[JsonDict]:
        selected = tuple(str(item) for item in states or (() if state is None else (state,)))
        return await self._call(
            self._list_query_tasks_sync,
            _text(run_id),
            _optional_text(query_id),
            selected,
            max(1, min(int(limit), 5_000)),
        )

    async def restart_query_tasks(self, run_id: str) -> JsonDict:
        """Fence enabled query tasks and make them available for an explicit rescan."""

        return await self._call(self._restart_query_tasks_sync, _text(run_id))

    async def enqueue_query_task(self, run_id: str, task: Mapping[str, Any]) -> JsonDict:
        return await self._call(self._enqueue_query_task_sync, _text(run_id), dict(task))

    async def lease_query_task(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        identity: Mapping[str, Any] | None = None,
    ) -> JsonDict | None:
        return await self._call(
            self._lease_query_task_sync,
            _text(run_id),
            _text(worker_id),
            int(lease_seconds),
            dict(identity or {}),
        )

    async def quarantine_discovery_identity(
        self,
        run_id: str,
        *,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        identity: Mapping[str, Any],
        endpoint: str,
        failure_kind: str,
        retry_after_seconds: float,
    ) -> JsonDict:
        """Fence and persist a temporary account/route/endpoint quarantine.

        A quarantine is intentionally scoped to the source endpoint instead of
        the whole account.  It protects a route that returned a protection or
        rate-limit response while allowing a different read endpoint to remain
        eligible when that is independently safe.
        """

        return await self._call(
            self._quarantine_discovery_identity_sync,
            _text(run_id),
            _text(task_id),
            _text(worker_id),
            _text(attempt_id),
            int(lease_fence),
            dict(identity),
            _text(endpoint),
            _text(failure_kind),
            retry_after_seconds,
        )

    async def list_discovery_quarantines(
        self,
        run_id: str,
        *,
        active_only: bool = False,
        limit: int = 500,
    ) -> list[JsonDict]:
        """List durable discovery route quarantines, expiring stale rows first."""

        return await self._call(
            self._list_discovery_quarantines_sync,
            _text(run_id),
            bool(active_only),
            max(1, min(int(limit), 5_000)),
        )

    async def retry_query_task(
        self,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        *,
        retry_after_seconds: float = 0,
        retry_at: str | None = None,
        failure_kind: str | None = None,
        error: str | None = None,
    ) -> JsonDict:
        return await self._call(
            self._retry_query_task_sync,
            _text(task_id),
            _text(worker_id),
            _text(attempt_id),
            int(lease_fence),
            retry_after_seconds,
            _optional_text(retry_at),
            _optional_text(failure_kind),
            _optional_text(error),
        )

    async def fail_query_task(
        self,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        *,
        failure_kind: str | None = None,
        error: str | None = None,
    ) -> JsonDict:
        return await self._call(
            self._fail_query_task_sync,
            _text(task_id),
            _text(worker_id),
            _text(attempt_id),
            int(lease_fence),
            _optional_text(failure_kind),
            _optional_text(error),
        )

    async def commit_observed_page(
        self,
        *,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        projects: Sequence[Mapping[str, Any]],
        source: str | None = None,
        raw_artifact_id: str | None = None,
        latency_ms: int | None = None,
        total_hint: int | None = None,
        observed_at: str | None = None,
        next_task: Mapping[str, Any] | None = None,
        next_tasks: Sequence[Mapping[str, Any]] | None = None,
    ) -> JsonDict:
        continuation_tasks = [dict(task) for task in next_tasks or ()]
        if next_task is not None:
            continuation_tasks.insert(0, dict(next_task))
        return await self._call(
            self._commit_observed_page_sync,
            _text(task_id),
            _text(worker_id),
            _text(attempt_id),
            int(lease_fence),
            [dict(project) for project in projects],
            _optional_text(source),
            _optional_text(raw_artifact_id),
            latency_ms,
            total_hint,
            observed_at or _now(),
            continuation_tasks,
        )

    async def list_run_projects(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        sort: str = "updated_desc",
        filters: Mapping[str, Any] | None = None,
    ) -> JsonDict:
        return await self._call(
            self._list_run_projects_sync,
            _text(run_id),
            cursor,
            max(1, min(int(limit), 500)),
            sort,
            dict(filters or {}),
        )

    async def get_run_project(self, run_id: str, project_id: str) -> JsonDict:
        return await self._call(self._get_run_project_sync, _text(run_id), _text(project_id))

    async def record_project_enrichment(
        self,
        run_id: str,
        project_id: str,
        enrichment: Mapping[str, Any],
    ) -> JsonDict:
        """Store one normalized selective-enrichment result for a project."""

        return await self._call(
            self._record_project_enrichment_sync,
            _text(run_id),
            _text(project_id),
            dict(enrichment),
        )

    async def get_facets(self, run_id: str, *, filters: Mapping[str, Any] | None = None) -> JsonDict:
        return await self._call(self._get_facets_sync, _text(run_id), dict(filters or {}))

    async def get_workspace_state(self, workspace_id: str = "default") -> JsonDict:
        return await self._call(self._get_workspace_state_sync, _text(workspace_id))

    async def put_workspace_state(
        self,
        state: Mapping[str, Any],
        *,
        workspace_id: str = "default",
        schema_version: int = 1,
    ) -> JsonDict:
        return await self._call(
            self._put_workspace_state_sync,
            _text(workspace_id),
            int(schema_version),
            dict(state),
        )

    async def upsert_attachment(
        self,
        run_id: str,
        project_id: str,
        attachment: Mapping[str, Any],
    ) -> JsonDict:
        """Persist canonical attachment metadata and refresh every affected projection."""

        return await self._call(
            self._upsert_attachment_sync,
            _text(run_id),
            _text(project_id),
            dict(attachment),
        )

    async def record_attachment_derivative(
        self,
        attachment_id: str,
        derivative: Mapping[str, Any],
    ) -> JsonDict:
        """Record one bounded parser/OCR/vision result for an attachment."""

        return await self._call(
            self._record_attachment_derivative_sync,
            _text(attachment_id),
            dict(derivative),
        )

    async def record_score(
        self,
        run_id: str,
        project_id: str,
        score: Mapping[str, Any],
    ) -> JsonDict:
        """Persist deterministic or AI score evidence and update the grid projection."""

        return await self._call(self._record_score_sync, _text(run_id), _text(project_id), dict(score))

    async def set_shortlist(
        self,
        run_id: str,
        project_id: str,
        shortlist: Mapping[str, Any],
    ) -> JsonDict:
        """Upsert operator shortlist state, normalized tags, and append-only notes."""

        return await self._call(self._set_shortlist_sync, _text(run_id), _text(project_id), dict(shortlist))

    async def create_export(self, run_id: str, export: Mapping[str, Any]) -> JsonDict:
        """Create a durable export job/audit row before its bytes are available."""

        return await self._call(self._create_export_sync, _text(run_id), dict(export))

    async def get_export(self, run_id: str, export_id: str) -> JsonDict:
        return await self._call(self._get_export_sync, _text(run_id), _text(export_id))

    async def update_export(self, run_id: str, export_id: str, changes: Mapping[str, Any]) -> JsonDict:
        return await self._call(
            self._update_export_sync,
            _text(run_id),
            _text(export_id),
            dict(changes),
        )

    async def store_raw_artifact(self, artifact: Mapping[str, Any]) -> JsonDict:
        return await self._call(self._store_raw_artifact_sync, dict(artifact))

    async def get_raw_artifact(self, artifact_id: str) -> JsonDict:
        return await self._call(self._get_raw_artifact_sync, _text(artifact_id))

    async def append_event(self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> JsonDict:
        return await self._call(self._append_event_sync, _text(run_id), _text(event_type), dict(payload or {}))

    async def replay_events(self, run_id: str, *, after_seq: int = 0, limit: int = 1_000) -> list[JsonDict]:
        return await self._call(self._replay_events_sync, _text(run_id), int(after_seq), max(1, min(int(limit), 10_000)))

    async def _call(self, function: Any, *args: Any) -> Any:
        await self._ensure_initialized()
        return await asyncio.to_thread(function, *args)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_SCHEMA)
            self._ensure_column(connection, "buyer_runs", "counters_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "buyer_runs", "last_error", "TEXT")
            self._ensure_column(connection, "buyer_runs", "last_failure_kind", "TEXT")
            self._ensure_column(connection, "buyer_runs", "last_task_id", "TEXT")
            self._ensure_column(connection, "buyer_query_tasks", "endpoint", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "buyer_query_tasks", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "buyer_project_observations", "expires_at", "TEXT")
            connection.execute(
                "UPDATE buyer_query_tasks SET endpoint = source WHERE endpoint IS NULL OR endpoint = ''"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS buyer_query_tasks_endpoint_idx "
                "ON buyer_query_tasks(run_id, endpoint, state, not_before)"
            )
            # These fields were introduced as a projection extension.  Add them
            # explicitly because CREATE TABLE IF NOT EXISTS cannot evolve a
            # persisted SQLite table from the first Buyer Search rollout.
            for column, definition in (
                ("attachment_count", "INTEGER NOT NULL DEFAULT 0"),
                ("attachment_parse_state", "TEXT"),
                ("preliminary_score", "REAL"),
                ("final_score", "REAL"),
                ("shortlist_state", "TEXT"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("proposal_state", "TEXT"),
                ("conversation_state", "TEXT"),
                ("unseen", "INTEGER NOT NULL DEFAULT 1"),
            ):
                self._ensure_column(connection, "buyer_run_projects", column, definition)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS buyer_run_projects_score_idx
                    ON buyer_run_projects(run_id, final_score DESC, preliminary_score DESC, project_id);
                CREATE INDEX IF NOT EXISTS buyer_run_projects_attachment_idx
                    ON buyer_run_projects(run_id, attachment_parse_state, attachment_count DESC, project_id);
                CREATE INDEX IF NOT EXISTS buyer_run_projects_shortlist_idx
                    ON buyer_run_projects(run_id, shortlist_state, updated_at DESC, project_id);
                """
            )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def _create_run_sync(self, payload: JsonDict) -> JsonDict:
        run_id = _text(payload.get("run_id")) or _new_id("buyer_run")
        mode = _text(payload.get("mode"))
        if not mode:
            raise BuyerRepositoryValidationError("run mode is required")
        requested_workers = max(1, _integer(payload.get("requested_workers"), default=1))
        now = _now()
        category_scope = payload.get("category_scope", payload.get("category_scope_json", []))
        filters = payload.get("filters", payload.get("filter_json", {}))
        config = payload.get("config", payload.get("config_json", {}))
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_runs (
                        run_id, job_id, name, mode, brief, category_scope_json, filter_json,
                        requested_workers, query_batch_size, target_unique_projects,
                        enrichment_policy, scoring_profile_id, state, created_by,
                        config_version, config_json, counters_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _optional_text(payload.get("job_id")),
                        _text(payload.get("name")) or run_id,
                        mode,
                        _optional_text(payload.get("brief")),
                        _json(category_scope, default=[]),
                        _json(filters, default={}),
                        requested_workers,
                        max(1, _integer(payload.get("query_batch_size"), default=1)),
                        _integer(payload.get("target_unique_projects"), default=0) or None,
                        _json(payload.get("enrichment_policy"), default={}),
                        _optional_text(payload.get("scoring_profile_id")),
                        _text(payload.get("state"), default="draft"),
                        _optional_text(payload.get("created_by")),
                        max(1, _integer(payload.get("config_version"), default=1)),
                        _json(config, default={}), _json(payload.get("counters"), default={}),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerRepositoryValidationError(f"run already exists: {run_id}") from exc
            row = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_record(row)

    def _get_run_sync(self, run_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise BuyerRunNotFoundError(run_id)
        return self._run_record(row)

    def _list_runs_sync(self, states: tuple[str, ...], limit: int, cursor: str | None) -> list[JsonDict]:
        query = "SELECT * FROM buyer_runs"
        parameters: list[Any] = []
        if states:
            query += f" WHERE state IN ({','.join('?' for _ in states)})"
            parameters.extend(states)
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded:
                query += " AND (created_at < ? OR (created_at = ? AND run_id > ?))" if " WHERE " in query else " WHERE (created_at < ? OR (created_at = ? AND run_id > ?))"
                parameters.extend([decoded.get("created_at"), decoded.get("created_at"), decoded.get("run_id")])
        query += " ORDER BY created_at DESC, run_id LIMIT ?"
        parameters.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._run_record(row) for row in rows]

    def _update_run_sync(self, run_id: str, changes: JsonDict) -> JsonDict:
        allowed = {
            "name", "brief", "category_scope", "filters", "requested_workers", "query_batch_size",
            "target_unique_projects", "enrichment_policy", "scoring_profile_id", "state", "config", "counters", "job_id",
            "started_at", "completed_at", "stopped_at", "updated_at", "last_error", "last_failure_kind", "last_task_id",
        }
        unknown = set(changes).difference(allowed)
        if unknown:
            raise BuyerRepositoryValidationError(f"unsupported run fields: {', '.join(sorted(unknown))}")
        assignments: list[str] = []
        values: list[Any] = []
        key_map = {"category_scope": "category_scope_json", "filters": "filter_json", "config": "config_json", "counters": "counters_json"}
        for key, value in changes.items():
            column = key_map.get(key, key)
            if key in {"category_scope", "filters", "config", "counters", "enrichment_policy"}:
                value = _json(value, default=[] if key == "category_scope" else {})
            elif key in {"requested_workers", "query_batch_size"}:
                value = max(1, _integer(value, default=1))
            elif key == "target_unique_projects":
                value = _integer(value, default=0) or None
            assignments.append(f"{column} = ?")
            values.append(value)
        if not assignments:
            return self._get_run_sync(run_id)
        assignments.append("config_version = config_version + 1")
        if "updated_at" not in changes:
            assignments.append("updated_at = ?")
            values.append(_now())
        values.append(run_id)
        with self._connect() as connection:
            result = connection.execute(f"UPDATE buyer_runs SET {', '.join(assignments)} WHERE run_id = ?", values)
            if result.rowcount != 1:
                raise BuyerRunNotFoundError(run_id)
            row = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_record(row)

    def _delete_run_sync(self, run_id: str) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                state = _text(run["state"]).casefold()
                if state in {"planning", "running", "pausing", "stopping", "completing"}:
                    raise BuyerRepositoryValidationError("active Buyer Search run must be stopped before deletion")
                connection.execute("DELETE FROM buyer_runs WHERE run_id = ?", (run_id,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"run_id": run_id, "deleted": True, "job_id": _optional_text(run["job_id"])}

    def _complete_run_if_exhausted_sync(self, run_id: str) -> JsonDict | None:
        """Finish only a truly drained running run, without racing a new lease."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                if _text(run["state"]).casefold() != "running":
                    connection.execute("COMMIT")
                    return None
                task_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS value
                        FROM buyer_query_tasks task
                        JOIN buyer_queries query ON query.query_id = task.query_id
                        WHERE task.run_id = ? AND query.enabled = 1
                        """,
                        (run_id,),
                    ).fetchone()["value"]
                )
                if task_count == 0:
                    connection.execute("COMMIT")
                    return None
                active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) AS value
                        FROM buyer_query_tasks task
                        JOIN buyer_queries query ON query.query_id = task.query_id
                        WHERE task.run_id = ? AND query.enabled = 1
                          AND task.state IN ('queued', 'leased', 'retry_wait')
                        """,
                        (run_id,),
                    ).fetchone()["value"]
                )
                if active_count:
                    connection.execute("COMMIT")
                    return None
                counters = _from_json(run["counters_json"], default={})
                counters = counters if isinstance(counters, Mapping) else {}
                unique_projects = _integer(counters.get("unique_projects"), default=0)
                target_unique_projects = _integer(run["target_unique_projects"], default=0)
                reason = "target_reached" if target_unique_projects and unique_projects >= target_unique_projects else "sources_exhausted"
                connection.execute(
                    """
                    UPDATE buyer_runs
                    SET state = 'completed', completed_at = COALESCE(completed_at, ?), updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, now, run_id),
                )
                self._append_event_in_transaction(
                    connection,
                    run_id,
                    "run.completed",
                    {
                        "state": "completed",
                        "reason": reason,
                        "unique_projects": unique_projects,
                        "target_unique_projects": target_unique_projects or None,
                        "task_count": task_count,
                    },
                )
                completed = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._run_record(completed)

    def _requeue_malformed_response_tasks_sync(self, run_id: str, limit: int) -> JsonDict:
        """Return only malformed response pages to the queue for a fresh read."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                if _text(run["state"]).casefold() != "running":
                    connection.execute("COMMIT")
                    return {"run_id": run_id, "requeued_task_count": 0}
                rows = connection.execute(
                    """
                    SELECT task_id, query_id
                    FROM buyer_query_tasks
                    WHERE run_id = ?
                      AND state = 'failed'
                      AND (
                          error_text LIKE '%payload has no project ID%'
                          OR error_text LIKE '%пустая карточка в %выдаче Kwork: нет ID проекта%'
                      )
                    ORDER BY completed_at, task_id
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
                if not rows:
                    connection.execute("COMMIT")
                    return {"run_id": run_id, "requeued_task_count": 0}
                task_ids = [str(row["task_id"]) for row in rows]
                query_ids = sorted({str(row["query_id"]) for row in rows})
                placeholders = ", ".join("?" for _ in task_ids)
                connection.execute(
                    f"""
                    UPDATE buyer_query_tasks
                    SET state = 'queued', not_before = NULL, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL, error_kind = NULL, error_text = NULL, completed_at = NULL
                    WHERE task_id IN ({placeholders})
                    """,
                    task_ids,
                )
                self._increment_run_counters(
                    connection,
                    run_id,
                    {
                        "queued_tasks": len(task_ids),
                        "failed_tasks": -len(task_ids),
                        "errors": -len(task_ids),
                    },
                    updated_at=now,
                )
                for query_id in query_ids:
                    self._refresh_query_terminal_state(
                        connection,
                        run_id=run_id,
                        query_id=query_id,
                        updated_at=now,
                    )
                self._append_event_in_transaction(
                    connection,
                    run_id,
                    "query.page.requeued",
                    {"task_count": len(task_ids), "reason": "malformed_project_response"},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"run_id": run_id, "requeued_task_count": len(task_ids)}

    def _reconcile_task_counters_sync(self, run_id: str) -> JsonDict:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                rows = connection.execute(
                    """
                    SELECT task.state, COUNT(*) AS value, COALESCE(SUM(task.retry_count), 0) AS retries
                    FROM buyer_query_tasks task
                    JOIN buyer_queries query ON query.query_id = task.query_id
                    WHERE task.run_id = ? AND query.enabled = 1
                    GROUP BY task.state
                    """,
                    (run_id,),
                ).fetchall()
                counts = {str(row["state"]): int(row["value"]) for row in rows}
                total_retries = sum(int(row["retries"] or 0) for row in rows)
                counters = _from_json(run["counters_json"], default={})
                reconciled = dict(counters) if isinstance(counters, Mapping) else {}
                reconciled.update(
                    {
                        "queued_tasks": counts.get("queued", 0),
                        "active_tasks": counts.get("leased", 0),
                        "retry_tasks": counts.get("retry_wait", 0),
                        "completed_tasks": counts.get("completed", 0),
                        "failed_tasks": counts.get("failed", 0),
                        "errors": counts.get("failed", 0),
                        "retries": total_retries,
                    }
                )
                connection.execute(
                    "UPDATE buyer_runs SET counters_json = ?, updated_at = ? WHERE run_id = ?",
                    (_json(reconciled, default={}), now, run_id),
                )
                updated = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._run_record(updated)

    def _create_queries_sync(self, run_id: str, queries: list[JsonDict]) -> list[JsonDict]:
        if not queries:
            return []
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            records: list[JsonDict] = []
            try:
                for payload in queries:
                    query_id = _text(payload.get("query_id")) or _new_id("buyer_query")
                    text = _text(payload.get("text"))
                    normalized_text = _text(payload.get("normalized_text")) or " ".join(text.lower().split())
                    if not text or not normalized_text:
                        raise BuyerRepositoryValidationError("query text is required")
                    filter_value = payload.get("filters", payload.get("filter_json", {}))
                    filter_json = _json(filter_value, default={})
                    filter_hash = _text(payload.get("filter_hash")) or _filter_hash(filter_json)
                    connection.execute(
                        """
                        INSERT INTO buyer_queries (
                            query_id, run_id, text, normalized_text, origin, parent_query_id, rationale,
                            priority, category_id, category_path, filter_json, filter_hash,
                            semantic_fingerprint, predicted_total, approved, enabled, state,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            query_id, run_id, text, normalized_text,
                            _text(payload.get("origin"), default="manual"),
                            _optional_text(payload.get("parent_query_id")), _optional_text(payload.get("rationale")),
                            _integer(payload.get("priority")), _text(payload.get("category_id")),
                            _optional_text(payload.get("category_path")), filter_json, filter_hash,
                            _optional_text(payload.get("semantic_fingerprint")),
                            _integer(payload.get("predicted_total"), default=0) or None,
                            int(bool(payload.get("approved", False))), int(payload.get("enabled", True) is not False),
                            _text(payload.get("state"), default="draft"), now, now,
                        ),
                    )
                    row = connection.execute("SELECT * FROM buyer_queries WHERE query_id = ?", (query_id,)).fetchone()
                    records.append(self._query_record(row))
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise BuyerRepositoryValidationError("duplicate or invalid buyer query") from exc
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        return records

    def _list_queries_sync(self, run_id: str, states: tuple[str, ...], limit: int, cursor: str | None) -> list[JsonDict]:
        query = "SELECT * FROM buyer_queries WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            parameters.extend(states)
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded:
                query += " AND (priority < ? OR (priority = ? AND query_id > ?))"
                parameters.extend([decoded.get("priority"), decoded.get("priority"), decoded.get("query_id")])
        query += " ORDER BY priority DESC, created_at, query_id LIMIT ?"
        parameters.append(max(1, min(limit, 1_000)))
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._query_record(row) for row in rows]

    def _update_query_sync(self, run_id: str, query_id: str, changes: JsonDict) -> JsonDict:
        allowed = {
            "text",
            "normalized_text",
            "filters",
            "category_id",
            "category_path",
            "rationale",
            "origin",
            "parent_query_id",
            "priority",
            "predicted_total",
            "approved",
            "enabled",
            "state",
        }
        unknown = set(changes).difference(allowed)
        if unknown:
            raise BuyerRepositoryValidationError(f"unsupported query fields: {', '.join(sorted(unknown))}")
        if not changes:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM buyer_queries WHERE run_id = ? AND query_id = ?", (run_id, query_id)
                ).fetchone()
            if row is None:
                raise BuyerSearchRepositoryError(f"query not found: {query_id}")
            return self._query_record(row)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM buyer_queries WHERE run_id = ? AND query_id = ?", (run_id, query_id)
                ).fetchone()
                if row is None:
                    raise BuyerSearchRepositoryError(f"query not found: {query_id}")
                text = _text(changes.get("text"), default=str(row["text"]))
                if not text:
                    raise BuyerRepositoryValidationError("query text is required")
                normalized_text = _text(changes.get("normalized_text"))
                if not normalized_text:
                    normalized_text = " ".join(text.lower().split())
                filters = changes.get("filters") if "filters" in changes else _from_json(row["filter_json"], default={})
                if not isinstance(filters, Mapping):
                    raise BuyerRepositoryValidationError("query filters must be an object")
                filter_json = _json(filters, default={})
                filter_hash = _filter_hash(filter_json)
                category_id = _text(changes.get("category_id"), default=str(row["category_id"] or ""))
                connection.execute(
                    """
                    UPDATE buyer_queries SET
                        text = ?, normalized_text = ?, origin = ?, parent_query_id = ?, rationale = ?,
                        priority = ?, category_id = ?, category_path = ?, filter_json = ?, filter_hash = ?,
                        predicted_total = ?, approved = ?, enabled = ?, state = ?, updated_at = ?
                    WHERE query_id = ? AND run_id = ?
                    """,
                    (
                        text,
                        normalized_text,
                        _text(changes.get("origin"), default=str(row["origin"])),
                        _optional_text(changes.get("parent_query_id")) if "parent_query_id" in changes else row["parent_query_id"],
                        _optional_text(changes.get("rationale")) if "rationale" in changes else row["rationale"],
                        _integer(changes.get("priority"), default=int(row["priority"])) if "priority" in changes else int(row["priority"]),
                        category_id,
                        _optional_text(changes.get("category_path")) if "category_path" in changes else row["category_path"],
                        filter_json,
                        filter_hash,
                        _integer(changes.get("predicted_total"), default=0) or None if "predicted_total" in changes else row["predicted_total"],
                        int(bool(changes.get("approved"))) if "approved" in changes else int(row["approved"]),
                        int(changes.get("enabled") is not False) if "enabled" in changes else int(row["enabled"]),
                        _text(changes.get("state"), default=str(row["state"])),
                        _now(),
                        query_id,
                        run_id,
                    ),
                )
                updated = connection.execute("SELECT * FROM buyer_queries WHERE query_id = ?", (query_id,)).fetchone()
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise BuyerRepositoryValidationError("duplicate buyer query identity") from exc
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._query_record(updated)

    def _list_query_coverage_sync(self, run_id: str) -> list[JsonDict]:
        """Return project IDs matched by each query without loading observations."""

        with self._connect() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT query_id, project_id
                FROM buyer_project_matches
                WHERE run_id = ?
                ORDER BY query_id, project_id
                """,
                (run_id,),
            ).fetchall()
        coverage: dict[str, list[str]] = {}
        for row in rows:
            coverage.setdefault(str(row["query_id"]), []).append(str(row["project_id"]))
        return [
            {"query_id": query_id, "project_ids": project_ids}
            for query_id, project_ids in coverage.items()
        ]

    def _compact_query_plan_sync(self, run_id: str, retain_query_ids: tuple[str, ...]) -> JsonDict:
        """Archive excess query variants while preserving their immutable evidence."""

        if not retain_query_ids:
            raise BuyerRepositoryValidationError("query plan compaction requires at least one retained query")
        placeholders = ", ".join("?" for _ in retain_query_ids)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                leased = connection.execute(
                    "SELECT COUNT(*) AS value FROM buyer_query_tasks WHERE run_id = ? AND state = 'leased'",
                    (run_id,),
                ).fetchone()
                if int(leased["value"] if leased is not None else 0):
                    raise BuyerRepositoryValidationError("cannot compact a query plan while page tasks are leased")

                known = {
                    str(row["query_id"])
                    for row in connection.execute(
                        "SELECT query_id FROM buyer_queries WHERE run_id = ?", (run_id,)
                    ).fetchall()
                }
                missing = sorted(set(retain_query_ids).difference(known))
                if missing:
                    raise BuyerRepositoryValidationError(f"retained queries do not belong to run: {', '.join(missing)}")

                connection.execute(
                    f"""
                    UPDATE buyer_queries
                    SET enabled = 0, state = 'disabled', updated_at = ?
                    WHERE run_id = ? AND query_id NOT IN ({placeholders})
                    """,
                    (now, run_id, *retain_query_ids),
                )
                connection.execute(
                    f"""
                    UPDATE buyer_queries
                    SET enabled = 1, approved = 1, updated_at = ?
                    WHERE run_id = ? AND query_id IN ({placeholders})
                    """,
                    (now, run_id, *retain_query_ids),
                )
                connection.execute(
                    f"""
                    UPDATE buyer_query_tasks
                    SET state = 'cancelled', not_before = NULL, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL, error_kind = NULL, error_text = NULL
                    WHERE run_id = ? AND query_id NOT IN ({placeholders})
                      AND state IN ('queued', 'retry_wait')
                    """,
                    (run_id, *retain_query_ids),
                )
                connection.execute(
                    f"""
                    UPDATE buyer_query_tasks
                    SET state = 'cancelled', not_before = NULL, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL, error_kind = NULL, error_text = NULL
                    WHERE run_id = ? AND query_id IN ({placeholders})
                      AND state IN ('queued', 'retry_wait')
                    """,
                    (run_id, *retain_query_ids),
                )

                compaction_cycle = int(run["config_version"] or 0) + 1
                fresh_task_count = 0
                for query_id in retain_query_ids:
                    unique = connection.execute(
                        """
                        SELECT COUNT(DISTINCT project_id) AS value
                        FROM buyer_project_matches
                        WHERE run_id = ? AND query_id = ?
                        """,
                        (run_id, query_id),
                    ).fetchone()
                    connection.execute(
                        """
                        UPDATE buyer_queries
                        SET pages_scheduled = 2, pages_completed = 0, projects_seen = 0,
                            unique_projects = ?, state = 'assigned', updated_at = ?
                        WHERE run_id = ? AND query_id = ?
                        """,
                        (
                            int(unique["value"] if unique is not None else 0),
                            now,
                            run_id,
                            query_id,
                        ),
                    )
                    for source in ("mobile_projects", "web_projects"):
                        cursor = {"plan_compaction": compaction_cycle}
                        fingerprint = hashlib.sha256(
                            _json(
                                {
                                    "query_id": query_id,
                                    "source": source,
                                    "page": 1,
                                    "cursor": cursor,
                                },
                                default={},
                            ).encode("utf-8")
                        ).hexdigest()
                        connection.execute(
                            """
                            INSERT INTO buyer_query_tasks (
                                task_id, run_id, query_id, source, endpoint, page, cursor_json, request_fingerprint,
                                priority, state, not_before, created_at
                            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0, 'queued', NULL, ?)
                            """,
                            (
                                _new_id("buyer_task"),
                                run_id,
                                query_id,
                                source,
                                source,
                                _json(cursor, default={}),
                                fingerprint,
                                now,
                            ),
                        )
                        fresh_task_count += 1

                state_counts = {"queued": fresh_task_count}
                counters = _from_json(run["counters_json"], default={})
                if not isinstance(counters, dict):
                    counters = {}
                counters.update(
                    {
                        "planned_queries": len(retain_query_ids),
                        "scheduled_tasks": fresh_task_count,
                        "queued_tasks": fresh_task_count,
                        "active_tasks": 0,
                        "retry_tasks": 0,
                        "completed_tasks": 0,
                        "pages_completed": 0,
                        "projects_seen": 0,
                        "errors": 0,
                        "failed_tasks": 0,
                        "retries": 0,
                        "auto_query_replenished_queries": 0,
                        "auto_query_replenishment_attempts": 0,
                        "auto_query_replenishment_batches": 0,
                        "auto_query_replenishment_empty_attempts": 0,
                        "auto_project_refresh_mode": False,
                        "auto_project_refresh_epoch": compaction_cycle,
                        "auto_project_refresh_cycles": 0,
                        "auto_project_refresh_tasks": 0,
                    }
                )
                connection.execute(
                    """
                    UPDATE buyer_runs
                    SET counters_json = ?, config_version = config_version + 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_json(counters, default={}), now, run_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return {
            "run_id": run_id,
            "retained_query_ids": list(retain_query_ids),
            "retained_query_count": len(retain_query_ids),
            "archived_query_count": max(0, len(known) - len(retain_query_ids)),
            "task_states": state_counts,
        }

    def _list_query_tasks_sync(
        self,
        run_id: str,
        query_id: str | None,
        states: tuple[str, ...],
        limit: int,
    ) -> list[JsonDict]:
        query = "SELECT * FROM buyer_query_tasks WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if query_id:
            query += " AND query_id = ?"
            parameters.append(query_id)
        if states:
            query += f" AND state IN ({', '.join('?' for _ in states)})"
            parameters.extend(states)
        query += " ORDER BY created_at, task_id LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(query, parameters).fetchall()
        return [self._task_record(row) for row in rows]

    def _enqueue_query_task_sync(self, run_id: str, payload: JsonDict) -> JsonDict:
        query_id = _text(payload.get("query_id"))
        source = _text(payload.get("source"), default="mobile_projects")
        if not query_id:
            raise BuyerRepositoryValidationError("query_id is required")
        endpoint = _discovery_endpoint(payload.get("endpoint"), fallback=source)
        cursor_value = payload.get("cursor")
        cursor_json = _json(cursor_value, default={}) if cursor_value is not None else None
        fingerprint = _text(payload.get("request_fingerprint"))
        if not fingerprint:
            fingerprint_payload: JsonDict = {
                "query_id": query_id,
                "source": source,
                "page": payload.get("page"),
                "cursor": cursor_value,
            }
            if endpoint != source:
                fingerprint_payload["endpoint"] = endpoint
            fingerprint = hashlib.sha256(
                _json(fingerprint_payload, default={}).encode("utf-8")
            ).hexdigest()
        task_id = _text(payload.get("task_id")) or _new_id("buyer_task")
        now = _now()
        with self._connect() as connection:
            self._require_run(connection, run_id)
            query_row = connection.execute("SELECT 1 FROM buyer_queries WHERE query_id = ? AND run_id = ?", (query_id, run_id)).fetchone()
            if query_row is None:
                raise BuyerRepositoryValidationError(f"query does not belong to run: {query_id}")
            try:
                connection.execute(
                    """
                    INSERT INTO buyer_query_tasks (
                        task_id, run_id, query_id, source, endpoint, page, cursor_json, request_fingerprint,
                        priority, state, not_before, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, run_id, query_id, source, endpoint, _integer(payload.get("page"), default=0) or None,
                     cursor_json, fingerprint, _integer(payload.get("priority")),
                     _text(payload.get("state"), default="queued"), _optional_text(payload.get("not_before")), now),
                )
            except sqlite3.IntegrityError as exc:
                raise BuyerRepositoryValidationError(f"duplicate request fingerprint for run: {fingerprint}") from exc
            connection.execute("UPDATE buyer_queries SET pages_scheduled = pages_scheduled + 1, updated_at = ? WHERE query_id = ?", (now, query_id))
            self._increment_run_counters(connection, run_id, {"scheduled_tasks": 1, "queued_tasks": 1}, updated_at=now)
            row = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._task_record(row)

    def _restart_query_tasks_sync(self, run_id: str) -> JsonDict:
        """Reset task execution state without deleting durable discovery evidence.

        Incrementing every task fence makes an in-flight pre-restart worker
        unable to commit its stale page after the operator requests a replay.
        Project rows, observations, raw artifacts, and query identities remain
        immutable audit evidence from the earlier pass.
        """

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = self._require_run(connection, run_id)
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS task_count
                    FROM buyer_query_tasks task
                    JOIN buyer_queries query ON query.query_id = task.query_id
                    WHERE task.run_id = ? AND query.enabled = 1
                    """,
                    (run_id,),
                ).fetchone()
                task_count = int(row["task_count"] if row is not None else 0)
                connection.execute(
                    """
                    UPDATE buyer_query_tasks AS task
                    SET state = 'queued', not_before = NULL, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL, lease_fence = lease_fence + 1,
                        retry_count = 0, error_kind = NULL, error_text = NULL, completed_at = NULL
                    WHERE run_id = ?
                      AND EXISTS (
                          SELECT 1 FROM buyer_queries query
                          WHERE query.query_id = task.query_id AND query.enabled = 1
                      )
                    """,
                    (run_id,),
                )
                connection.execute(
                    """
                    UPDATE buyer_query_tasks AS task
                    SET state = 'cancelled', not_before = NULL, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL
                    WHERE run_id = ?
                      AND state IN ('queued', 'retry_wait')
                      AND EXISTS (
                          SELECT 1 FROM buyer_queries query
                          WHERE query.query_id = task.query_id AND query.enabled = 0
                      )
                    """,
                    (run_id,),
                )
                connection.execute(
                    """
                    UPDATE buyer_queries
                    SET pages_completed = 0, projects_seen = 0, state = 'assigned', updated_at = ?
                    WHERE run_id = ? AND enabled = 1
                    """,
                    (now, run_id),
                )
                counters = _from_json(run["counters_json"], default={})
                if not isinstance(counters, dict):
                    counters = {}
                counters["queued_tasks"] = task_count
                counters["active_tasks"] = 0
                counters["retry_tasks"] = 0
                counters["completed_tasks"] = 0
                counters["pages_completed"] = 0
                counters["projects_seen"] = 0
                counters["restart_count"] = int(counters.get("restart_count") or 0) + 1
                connection.execute(
                    "UPDATE buyer_runs SET counters_json = ?, updated_at = ? WHERE run_id = ?",
                    (_json(counters, default={}), now, run_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "run_id": run_id,
            "requeued_task_count": task_count,
            "restart_count": counters["restart_count"],
        }

    def _lease_query_task_sync(self, run_id: str, worker_id: str, lease_seconds: int, identity: JsonDict) -> JsonDict | None:
        if not worker_id:
            raise BuyerRepositoryValidationError("worker_id is required")
        now = _now()
        deadline = (datetime.now(UTC) + timedelta(seconds=max(1, lease_seconds))).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        account_registration_id = _optional_text(identity.get("account_registration_id"))
        transport_id = _optional_text(identity.get("transport_id"))
        if account_registration_id:
            account_registration_id = account_registration_id.casefold()
        if transport_id:
            transport_id = transport_id.casefold()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._expire_discovery_quarantines(connection, now=now)
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET state = 'queued', lease_owner = NULL, lease_deadline = NULL, attempt_id = NULL
                    WHERE run_id = ? AND state = 'leased' AND lease_deadline <= ?
                    """,
                    (run_id, now),
                )
                query = """
                    SELECT task.task_id, task.state FROM buyer_query_tasks task
                    JOIN buyer_queries query ON query.query_id = task.query_id
                    WHERE task.run_id = ?
                      AND task.state IN ('queued', 'retry_wait')
                      AND task.query_id = query.query_id
                      AND query.enabled = 1
                      AND (not_before IS NULL OR not_before <= ?)
                """
                parameters: list[Any] = [run_id, now]
                if account_registration_id and transport_id:
                    query += """
                      AND NOT EXISTS (
                          SELECT 1 FROM buyer_discovery_quarantines quarantine
                          WHERE quarantine.account_registration_id = ?
                            AND quarantine.transport_id = ?
                            AND quarantine.endpoint = COALESCE(NULLIF(task.endpoint, ''), task.source)
                            AND quarantine.state = 'active'
                            AND quarantine.expires_at > ?
                      )
                    """
                    parameters.extend([account_registration_id, transport_id, now])
                query += """
                    ORDER BY task.priority DESC, task.created_at, task.task_id
                    LIMIT 1
                """
                row = connection.execute(
                    query,
                    parameters,
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                attempt_id = _new_id("buyer_attempt")
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET state = 'leased', lease_owner = ?, lease_deadline = ?, lease_fence = lease_fence + 1,
                        attempt_id = ?, account_registration_id = ?, transport_id = ?, egress_ip = ?, route_generation = ?,
                        error_kind = NULL, error_text = NULL
                    WHERE task_id = ?
                    """,
                    (worker_id, deadline, attempt_id, account_registration_id,
                     transport_id, _optional_text(identity.get("egress_ip")),
                     _integer(identity.get("route_generation"), default=0) or None, row["task_id"]),
                )
                pending_counter = "retry_tasks" if row["state"] == "retry_wait" else "queued_tasks"
                self._increment_run_counters(connection, run_id, {pending_counter: -1, "active_tasks": 1}, updated_at=now)
                leased = connection.execute(
                    """
                    SELECT task.*, query.text AS query_text, query.origin AS query_origin,
                        query.category_id AS query_category_id,
                        query.filter_json AS query_filter_json, query.priority AS query_priority,
                        query.enabled AS query_enabled
                    FROM buyer_query_tasks task
                    JOIN buyer_queries query ON query.query_id = task.query_id
                    WHERE task.task_id = ?
                    """,
                    (row["task_id"],),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        record = self._task_record(leased)
        record["query_text"] = leased["query_text"]
        record["query_origin"] = leased["query_origin"]
        record["category_id"] = leased["query_category_id"]
        record["filters"] = _from_json(leased["query_filter_json"], default={})
        record["priority"] = leased["query_priority"]
        record["query_enabled"] = bool(leased["query_enabled"])
        return record

    @staticmethod
    def _expire_discovery_quarantines(
        connection: sqlite3.Connection,
        *,
        now: str,
        run_id: str | None = None,
    ) -> int:
        """Mark elapsed quarantines as history before selecting the next task."""

        query = """
            UPDATE buyer_discovery_quarantines
            SET state = 'expired', released_at = COALESCE(released_at, ?), updated_at = ?
            WHERE state = 'active' AND expires_at <= ?
        """
        parameters: list[Any] = [now, now, now]
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        result = connection.execute(query, parameters)
        return max(0, int(result.rowcount or 0))

    def _quarantine_discovery_identity_sync(
        self,
        run_id: str,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        identity: JsonDict,
        endpoint: str,
        failure_kind: str,
        retry_after_seconds: float,
    ) -> JsonDict:
        """Persist a fenced, auditable quarantine for one protected endpoint."""

        if failure_kind not in {"http_403", "http_429"}:
            raise BuyerRepositoryValidationError("discovery quarantine requires http_403 or http_429")
        try:
            requested_delay = float(retry_after_seconds)
        except (TypeError, ValueError) as exc:
            raise BuyerRepositoryValidationError("retry_after_seconds must be a finite positive number") from exc
        if not math.isfinite(requested_delay) or requested_delay <= 0:
            raise BuyerRepositoryValidationError("retry_after_seconds must be a finite positive number")
        account_registration_id = _text(identity.get("account_registration_id")).casefold()
        transport_id = _text(identity.get("transport_id")).casefold()
        if not account_registration_id or not transport_id:
            raise BuyerRepositoryValidationError("quarantine identity requires account_registration_id and transport_id")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                self._require_task_lease(task, worker_id, attempt_id, lease_fence, now)
                if str(task["run_id"]) != run_id:
                    raise BuyerQueryTaskLeaseLostError(task_id)
                task_endpoint = _discovery_endpoint(task["endpoint"], fallback=str(task["source"]))
                supplied_endpoint = _discovery_endpoint(endpoint, fallback=task_endpoint)
                if supplied_endpoint != task_endpoint:
                    raise BuyerRepositoryValidationError("quarantine endpoint must match the leased task endpoint")

                existing = connection.execute(
                    """
                    SELECT * FROM buyer_discovery_quarantines
                    WHERE run_id = ? AND account_registration_id = ? AND transport_id = ? AND endpoint = ?
                    """,
                    (run_id, account_registration_id, transport_id, task_endpoint),
                ).fetchone()
                failure_count = int(existing["failure_count"] if existing is not None else 0) + 1
                # Preserve Retry-After as the base while making consecutive
                # protection signals converge to a bounded deterministic pause.
                multiplier = 2 ** min(failure_count - 1, 4)
                duration_seconds = min(3_600.0, max(1.0, requested_delay) * multiplier)
                expires_at = (
                    datetime.now(UTC) + timedelta(seconds=duration_seconds)
                ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                quarantine_id = _new_id("buyer_quarantine")
                connection.execute(
                    """
                    INSERT INTO buyer_discovery_quarantines (
                        quarantine_id, run_id, account_registration_id, transport_id, endpoint,
                        failure_kind, failure_count, last_task_id, last_attempt_id, last_lease_fence,
                        last_worker_id, last_egress_ip, last_route_generation, state, expires_at,
                        released_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?)
                    ON CONFLICT(run_id, account_registration_id, transport_id, endpoint) DO UPDATE SET
                        failure_kind = excluded.failure_kind,
                        failure_count = excluded.failure_count,
                        last_task_id = excluded.last_task_id,
                        last_attempt_id = excluded.last_attempt_id,
                        last_lease_fence = excluded.last_lease_fence,
                        last_worker_id = excluded.last_worker_id,
                        last_egress_ip = excluded.last_egress_ip,
                        last_route_generation = excluded.last_route_generation,
                        state = 'active',
                        expires_at = excluded.expires_at,
                        released_at = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        quarantine_id,
                        run_id,
                        account_registration_id,
                        transport_id,
                        task_endpoint,
                        failure_kind,
                        failure_count,
                        task_id,
                        attempt_id,
                        lease_fence,
                        worker_id,
                        _optional_text(identity.get("egress_ip")),
                        _integer(identity.get("route_generation"), default=0) or None,
                        expires_at,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM buyer_discovery_quarantines
                    WHERE run_id = ? AND account_registration_id = ? AND transport_id = ? AND endpoint = ?
                    """,
                    (run_id, account_registration_id, transport_id, task_endpoint),
                ).fetchone()
                assert row is not None
                record = self._quarantine_record(row)
                self._append_event_in_transaction(
                    connection,
                    run_id,
                    "identity.quarantined",
                    {
                        "policy": "account_transport_endpoint",
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "lease_fence": lease_fence,
                        "worker_id": worker_id,
                        "account_registration_id": account_registration_id,
                        "transport_id": transport_id,
                        "egress_ip": _optional_text(identity.get("egress_ip")),
                        "route_generation": _integer(identity.get("route_generation"), default=0) or None,
                        "endpoint": task_endpoint,
                        "failure_kind": failure_kind,
                        "failure_count": failure_count,
                        "duration_seconds": duration_seconds,
                        "expires_at": record["expires_at"],
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return record

    def _list_discovery_quarantines_sync(
        self,
        run_id: str,
        active_only: bool,
        limit: int,
    ) -> list[JsonDict]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run(connection, run_id)
                self._expire_discovery_quarantines(connection, run_id=run_id, now=now)
                query = "SELECT * FROM buyer_discovery_quarantines WHERE run_id = ?"
                parameters: list[Any] = [run_id]
                if active_only:
                    query += " AND state = 'active'"
                query += " ORDER BY updated_at DESC, quarantine_id DESC LIMIT ?"
                parameters.append(limit)
                rows = connection.execute(query, parameters).fetchall()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [self._quarantine_record(row) for row in rows]

    def _retry_query_task_sync(
        self,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        retry_after_seconds: float,
        retry_at: str | None,
        failure_kind: str | None,
        error: str | None,
    ) -> JsonDict:
        try:
            delay = float(retry_after_seconds)
        except (TypeError, ValueError) as exc:
            raise BuyerRepositoryValidationError("retry_after_seconds must be a finite non-negative number") from exc
        if not math.isfinite(delay) or delay < 0:
            raise BuyerRepositoryValidationError("retry_after_seconds must be a finite non-negative number")
        now = _now()
        not_before = retry_at or (datetime.now(UTC) + timedelta(seconds=min(delay, 7 * 24 * 60 * 60))).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                self._require_task_lease(task, worker_id, attempt_id, lease_fence, now)
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET state = 'retry_wait', not_before = ?, lease_owner = NULL, lease_deadline = NULL,
                        attempt_id = NULL, retry_count = retry_count + 1, error_kind = ?, error_text = ?
                    WHERE task_id = ?
                    """,
                    (not_before, failure_kind, error, task_id),
                )
                self._increment_run_counters(
                    connection,
                    str(task["run_id"]),
                    {"retries": 1, "active_tasks": -1, "retry_tasks": 1},
                    updated_at=now,
                )
                updated = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._task_record(updated)

    def _fail_query_task_sync(
        self,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        failure_kind: str | None,
        error: str | None,
    ) -> JsonDict:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                self._require_task_lease(task, worker_id, attempt_id, lease_fence, now)
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET state = 'failed', lease_owner = NULL, lease_deadline = NULL, attempt_id = NULL,
                        error_kind = ?, error_text = ?, completed_at = ?
                    WHERE task_id = ?
                    """,
                    (failure_kind, error, now, task_id),
                )
                self._increment_run_counters(
                    connection,
                    str(task["run_id"]),
                    {"failed_tasks": 1, "errors": 1, "active_tasks": -1},
                    updated_at=now,
                )
                self._refresh_query_terminal_state(
                    connection,
                    run_id=str(task["run_id"]),
                    query_id=str(task["query_id"]),
                    updated_at=now,
                )
                updated = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._task_record(updated)

    def _commit_observed_page_sync(
        self,
        task_id: str,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        projects: list[JsonDict],
        source: str | None,
        raw_artifact_id: str | None,
        latency_ms: int | None,
        total_hint: int | None,
        observed_at: str,
        next_tasks: list[JsonDict],
    ) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute("SELECT * FROM buyer_query_tasks WHERE task_id = ?", (task_id,)).fetchone()
                if task is None:
                    raise BuyerQueryTaskLeaseLostError(task_id)
                if task["state"] == "completed" and task["attempt_id"] == attempt_id and task["lease_fence"] == lease_fence:
                    result = _from_json(task["commit_result_json"], default=None)
                    connection.execute("COMMIT")
                    return result if isinstance(result, dict) else {"task_id": task_id, "idempotent": True}
                if (
                    task["state"] != "leased"
                    or task["lease_owner"] != worker_id
                    or task["attempt_id"] != attempt_id
                    or task["lease_fence"] != lease_fence
                    or not task["lease_deadline"]
                    or str(task["lease_deadline"]) <= observed_at
                ):
                    raise BuyerQueryTaskLeaseLostError(task_id)

                run_id = str(task["run_id"])
                query_id = str(task["query_id"])
                task_source = source or str(task["source"])
                new_projects = 0
                new_matches = 0
                new_run_projects = 0
                for position, raw_card in enumerate(projects):
                    card = _normalize_committed_card(raw_card)
                    project_id, was_new = self._upsert_project(connection, card, observed_at)
                    new_projects += int(was_new)
                    observation_id = self._insert_observation(
                        connection, task, project_id, card, task_source, position, raw_artifact_id, observed_at,
                    )
                    match_new = self._upsert_match(connection, run_id, project_id, query_id, observation_id, observed_at)
                    new_matches += int(match_new)
                    run_project_new = self._upsert_run_project(connection, run_id, project_id, observation_id, card, observed_at)
                    new_run_projects += int(run_project_new)
                connection.execute(
                    """
                    UPDATE buyer_queries
                    SET pages_completed = pages_completed + 1,
                        projects_seen = projects_seen + ?,
                        unique_projects = unique_projects + ?,
                        predicted_total = CASE
                            WHEN ? IS NULL THEN predicted_total
                            ELSE MAX(COALESCE(predicted_total, 0), ?)
                        END,
                        updated_at = ?
                    WHERE query_id = ?
                    """,
                    (len(projects), new_matches, total_hint, total_hint, observed_at, query_id),
                )
                self._increment_run_counters(
                    connection,
                    run_id,
                    {
                        "pages_completed": 1,
                        "projects_seen": len(projects),
                        "unique_projects": new_run_projects,
                        "completed_tasks": 1,
                        "active_tasks": -1,
                    },
                    updated_at=observed_at,
                )
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET state = 'completed', response_artifact_id = COALESCE(?, response_artifact_id),
                        result_count = ?, total_hint = COALESCE(?, total_hint), latency_ms = COALESCE(?, latency_ms),
                        completed_at = ?
                    WHERE task_id = ?
                    """,
                    (raw_artifact_id, len(projects), total_hint, latency_ms, observed_at, task_id),
                )
                continuation_task_ids = [
                    continuation_task_id
                    for next_task in next_tasks
                    if (
                        continuation_task_id := self._enqueue_continuation_task(
                            connection,
                            task=task,
                            next_task=next_task,
                            observed_at=observed_at,
                        )
                    )
                ]
                query_state = self._refresh_query_terminal_state(
                    connection,
                    run_id=run_id,
                    query_id=query_id,
                    updated_at=observed_at,
                )
                result: JsonDict = {
                    "task_id": task_id,
                    "run_id": run_id,
                    "query_id": query_id,
                    "observed_count": len(projects),
                    "new_project_count": new_projects,
                    "new_match_count": new_matches,
                    "new_run_project_count": new_run_projects,
                    "continuation_task_id": continuation_task_ids[0] if continuation_task_ids else None,
                    "continuation_task_ids": continuation_task_ids,
                    "continuation_enqueued": bool(continuation_task_ids),
                    "continuation_count": len(continuation_task_ids),
                    "query_state": query_state,
                    "idempotent": False,
                }
                connection.execute(
                    """
                    UPDATE buyer_query_tasks
                    SET commit_result_json = ?
                    WHERE task_id = ?
                    """,
                    (_json(result, default={}), task_id),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _enqueue_continuation_task(
        self,
        connection: sqlite3.Connection,
        *,
        task: sqlite3.Row,
        next_task: JsonDict | None,
        observed_at: str,
    ) -> str | None:
        """Insert the next immutable page request in the page-commit transaction."""

        if not next_task:
            return None
        run_id = str(task["run_id"])
        query_id = str(task["query_id"])
        run = self._require_run(connection, run_id)
        target_unique_projects = _integer(run["target_unique_projects"], default=0)
        counters = _from_json(run["counters_json"], default={})
        unique_projects = _integer(counters.get("unique_projects"), default=0) if isinstance(counters, Mapping) else 0
        if target_unique_projects and unique_projects >= target_unique_projects:
            return None
        query = connection.execute(
            "SELECT enabled FROM buyer_queries WHERE query_id = ? AND run_id = ?", (query_id, run_id)
        ).fetchone()
        if query is None or not bool(query["enabled"]):
            return None
        current_page = _integer(task["page"], default=1) or 1
        page = _integer(next_task.get("page"), default=current_page + 1) or current_page + 1
        if page <= current_page:
            raise BuyerRepositoryValidationError("continuation page must advance beyond the committed page")
        source = _text(next_task.get("source"), default=str(task["source"]))
        if not source:
            raise BuyerRepositoryValidationError("continuation source is required")
        endpoint = _discovery_endpoint(next_task.get("endpoint"), fallback=_text(task["endpoint"], default=source))
        cursor = next_task.get("cursor")
        cursor_json = _json(cursor, default={}) if cursor is not None else None
        fingerprint = _text(next_task.get("request_fingerprint"))
        if not fingerprint:
            fingerprint_payload: JsonDict = {
                "query_id": query_id,
                "source": source,
                "page": page,
                "cursor": cursor,
            }
            if endpoint != source:
                fingerprint_payload["endpoint"] = endpoint
            fingerprint = hashlib.sha256(
                _json(fingerprint_payload, default={}).encode("utf-8")
            ).hexdigest()
        task_id = _text(next_task.get("task_id")) or _new_id("buyer_task")
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO buyer_query_tasks (
                task_id, run_id, query_id, source, endpoint, page, cursor_json, request_fingerprint,
                priority, state, not_before, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                task_id,
                run_id,
                query_id,
                source,
                endpoint,
                page,
                cursor_json,
                fingerprint,
                _integer(next_task.get("priority"), default=_integer(task["priority"])),
                _optional_text(next_task.get("not_before")),
                observed_at,
            ),
        )
        if inserted.rowcount != 1:
            return None
        connection.execute(
            "UPDATE buyer_queries SET pages_scheduled = pages_scheduled + 1, updated_at = ? WHERE query_id = ?",
            (observed_at, query_id),
        )
        self._increment_run_counters(connection, run_id, {"scheduled_tasks": 1, "queued_tasks": 1}, updated_at=observed_at)
        return task_id

    def _list_run_projects_sync(self, run_id: str, cursor: str | None, limit: int, sort: str | None, filters: JsonDict) -> JsonDict:
        sort_specs = {
            "updated_desc": ("rp.updated_at", "DESC"),
            "score_desc": ("COALESCE(rp.final_score, rp.preliminary_score, -1.0e308)", "DESC"),
            "preliminary_score_desc": ("COALESCE(rp.preliminary_score, -1.0e308)", "DESC"),
            "final_score_desc": ("COALESCE(rp.final_score, -1.0e308)", "DESC"),
            "budget_desc": ("COALESCE(rp.budget_max, rp.budget_min, -1.0e308)", "DESC"),
            "offers_asc": ("COALESCE(rp.offers, 1.0e308)", "ASC"),
            "views_desc": ("COALESCE(rp.views, -1.0e308)", "DESC"),
            "attachment_count_desc": ("rp.attachment_count", "DESC"),
            "matched_queries_desc": ("rp.matched_query_count", "DESC"),
            "project_id_asc": ("rp.project_id", "ASC"),
        }
        sort = sort or "updated_desc"
        if sort not in sort_specs:
            raise BuyerRepositoryValidationError(f"unsupported project sort: {sort}")
        sort_expr, direction = sort_specs[sort]
        base_where, base_parameters = self._project_filters(run_id, filters)
        where = list(base_where)
        parameters = list(base_parameters)
        decoded = _cursor_decode(cursor)
        if decoded:
            if decoded.get("sort") != sort:
                raise BuyerRepositoryValidationError("cursor sort does not match request")
            value = decoded.get("value")
            project_id = _text(decoded.get("project_id"))
            comparator = "<" if direction == "DESC" else ">"
            where.append(f"({sort_expr} {comparator} ? OR ({sort_expr} = ? AND rp.project_id > ?))")
            parameters.extend([value, value, project_id])
        sql = f"""
            SELECT rp.*, p.remote_project_id, p.canonical_url, p.buyer_username,
                   latest.orders, latest.buyer_projects_count, latest.buyer_active_projects_count, latest.expires_at,
                   {sort_expr} AS _sort_value
            FROM buyer_run_projects rp
            JOIN buyer_projects p ON p.buyer_project_id = rp.project_id
            LEFT JOIN buyer_project_observations latest ON latest.observation_id = rp.latest_observation_id
            WHERE {' AND '.join(where)}
            ORDER BY {sort_expr} {direction}, rp.project_id ASC
            LIMIT ?
        """
        parameters.append(limit + 1)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM buyer_run_projects rp WHERE {' AND '.join(base_where)}",
                    base_parameters,
                ).fetchone()["total"]
            )
            run_total = int(
                connection.execute(
                    "SELECT COUNT(*) AS total FROM buyer_run_projects WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["total"]
            )
            rows = connection.execute(sql, parameters).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._run_project_record(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _cursor_encode({"sort": sort, "value": last["_sort_value"], "project_id": last["project_id"]})
        return {"items": items, "next_cursor": next_cursor, "total": total, "run_total": run_total}

    def _get_workspace_state_sync(self, workspace_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_workspace_state WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                return {
                    "workspace_id": workspace_id,
                    "schema_version": 1,
                    "state": {},
                    "updated_at": None,
                }
            state = _from_json(row["state_json"], default={})
            sanitized = self._sanitize_workspace_state(connection, state)
            return {
                "workspace_id": workspace_id,
                "schema_version": int(row["schema_version"]),
                "state": sanitized,
                "updated_at": row["updated_at"],
            }

    def _put_workspace_state_sync(
        self,
        workspace_id: str,
        schema_version: int,
        state: JsonDict,
    ) -> JsonDict:
        if schema_version != 1:
            raise BuyerRepositoryValidationError("unsupported workspace schema version")
        updated_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sanitized = self._sanitize_workspace_state(connection, state)
            connection.execute(
                """
                INSERT INTO buyer_workspace_state (workspace_id, schema_version, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, schema_version, _json(sanitized, default={}), updated_at),
            )
            connection.execute("COMMIT")
        return {
            "workspace_id": workspace_id,
            "schema_version": schema_version,
            "state": sanitized,
            "updated_at": updated_at,
        }

    @staticmethod
    def _sanitize_workspace_state(connection: sqlite3.Connection, state: Mapping[str, Any]) -> JsonDict:
        sanitized = dict(state)
        view = dict(sanitized.get("view") or {})
        selected_run_id = _optional_text(view.get("selected_run_id"))
        if selected_run_id:
            run_exists = connection.execute(
                "SELECT 1 FROM buyer_runs WHERE run_id = ?",
                (selected_run_id,),
            ).fetchone()
            if run_exists is None:
                selected_run_id = None
        selected_project_id = _optional_text(view.get("selected_project_id"))
        if selected_project_id and selected_run_id:
            project_exists = connection.execute(
                "SELECT 1 FROM buyer_run_projects WHERE run_id = ? AND project_id = ?",
                (selected_run_id, selected_project_id),
            ).fetchone()
            if project_exists is None:
                selected_project_id = None
        elif selected_project_id:
            selected_project_id = None
        view["selected_run_id"] = selected_run_id
        view["selected_project_id"] = selected_project_id
        if not selected_run_id:
            view["cursor"] = None
            view["cursor_history"] = []
            view["selected_project_ids"] = []
        sanitized["view"] = view
        return sanitized

    def _get_run_project_sync(self, run_id: str, project_id: str) -> JsonDict:
        with self._connect() as connection:
            projection = connection.execute(
                "SELECT * FROM buyer_run_projects WHERE run_id = ? AND project_id = ?", (run_id, project_id)
            ).fetchone()
            if projection is None:
                raise BuyerSearchRepositoryError(f"project is not in run: {project_id}")
            project = connection.execute("SELECT * FROM buyer_projects WHERE buyer_project_id = ?", (project_id,)).fetchone()
            observations = connection.execute(
                "SELECT * FROM buyer_project_observations WHERE run_id = ? AND project_id = ? ORDER BY observed_at DESC, observation_id DESC",
                (run_id, project_id),
            ).fetchall()
            matches = connection.execute(
                """
                SELECT m.*, q.text AS query_text, q.normalized_text AS normalized_query_text
                FROM buyer_project_matches m JOIN buyer_queries q ON q.query_id = m.query_id
                WHERE m.run_id = ? AND m.project_id = ? ORDER BY m.first_seen_at, m.query_id
                """,
                (run_id, project_id),
            ).fetchall()
            enrichments = connection.execute(
                """
                SELECT * FROM buyer_project_enrichments
                WHERE run_id = ? AND project_id = ?
                ORDER BY observed_at DESC, enrichment_id DESC
                """,
                (run_id, project_id),
            ).fetchall()
            attachments = connection.execute(
                "SELECT * FROM buyer_attachments WHERE project_id = ? ORDER BY created_at, attachment_id",
                (project_id,),
            ).fetchall()
            derivatives = connection.execute(
                """
                SELECT * FROM buyer_attachment_derivatives
                WHERE attachment_id IN (SELECT attachment_id FROM buyer_attachments WHERE project_id = ?)
                ORDER BY created_at, derivative_id
                """,
                (project_id,),
            ).fetchall()
            scores = connection.execute(
                """
                SELECT * FROM buyer_scores WHERE run_id = ? AND project_id = ?
                ORDER BY created_at DESC, score_id DESC
                """,
                (run_id, project_id),
            ).fetchall()
            shortlist = connection.execute(
                "SELECT * FROM buyer_shortlist_items WHERE run_id = ? AND project_id = ?",
                (run_id, project_id),
            ).fetchone()
            tags = connection.execute(
                "SELECT tag FROM buyer_shortlist_tags WHERE run_id = ? AND project_id = ? ORDER BY tag",
                (run_id, project_id),
            ).fetchall()
            notes = connection.execute(
                """
                SELECT * FROM buyer_shortlist_notes WHERE run_id = ? AND project_id = ?
                ORDER BY created_at, note_id
                """,
                (run_id, project_id),
            ).fetchall()
        derivatives_by_attachment: dict[str, list[JsonDict]] = {}
        for derivative in derivatives:
            derivatives_by_attachment.setdefault(str(derivative["attachment_id"]), []).append(
                self._derivative_record(derivative)
            )
        attachment_records = []
        for attachment in attachments:
            record = self._attachment_record(attachment)
            record["derivatives"] = derivatives_by_attachment.get(str(attachment["attachment_id"]), [])
            attachment_records.append(record)
        return {
            "project": self._project_record(project),
            "run_project": self._run_project_record(projection),
            "observations": [self._observation_record(row) for row in observations],
            "matches": [self._match_record(row) for row in matches],
            "enrichments": [self._enrichment_record(row) for row in enrichments],
            "attachments": attachment_records,
            "scores": [self._score_record(row) for row in scores],
            "shortlist": self._shortlist_record(
                shortlist,
                tags=[str(row["tag"]) for row in tags],
                notes=[self._shortlist_note_record(row) for row in notes],
            ) if shortlist is not None else None,
        }

    def _get_facets_sync(self, run_id: str, filters: JsonDict) -> JsonDict:
        where, parameters = self._project_filters(run_id, filters)
        clause = " AND ".join(where)
        with self._connect() as connection:
            summary = connection.execute(
                f"""
                SELECT COUNT(*) AS total, MIN(budget_min) AS budget_min, MAX(budget_max) AS budget_max,
                    MIN(preliminary_score) AS min_preliminary_score, MAX(preliminary_score) AS max_preliminary_score,
                    MIN(final_score) AS min_final_score, MAX(final_score) AS max_final_score,
                    MIN(COALESCE(final_score, preliminary_score)) AS min_effective_score,
                    MAX(COALESCE(final_score, preliminary_score)) AS max_effective_score
                FROM buyer_run_projects rp WHERE {clause}
                """,
                parameters,
            ).fetchone()
            categories = connection.execute(
                f"SELECT COALESCE(category_id, '') AS value, COUNT(*) AS count FROM buyer_run_projects rp WHERE {clause} GROUP BY COALESCE(category_id, '') ORDER BY count DESC, value",
                parameters,
            ).fetchall()
            states = connection.execute(
                f"SELECT COALESCE(shortlist_state, '') AS value, COUNT(*) AS count FROM buyer_run_projects rp WHERE {clause} GROUP BY COALESCE(shortlist_state, '') ORDER BY count DESC, value",
                parameters,
            ).fetchall()
            attachment_states = connection.execute(
                f"SELECT COALESCE(attachment_parse_state, '') AS value, COUNT(*) AS count FROM buyer_run_projects rp WHERE {clause} GROUP BY COALESCE(attachment_parse_state, '') ORDER BY count DESC, value",
                parameters,
            ).fetchall()
            attachment_types = connection.execute(
                f"""
                SELECT COALESCE(attachment.detected_type, '') AS value, COUNT(DISTINCT rp.project_id) AS count
                FROM buyer_run_projects rp
                JOIN buyer_attachments attachment ON attachment.project_id = rp.project_id
                WHERE {clause}
                GROUP BY COALESCE(attachment.detected_type, '') ORDER BY count DESC, value
                """,
                parameters,
            ).fetchall()
            proposal_states = connection.execute(
                f"SELECT COALESCE(proposal_state, '') AS value, COUNT(*) AS count FROM buyer_run_projects rp WHERE {clause} GROUP BY COALESCE(proposal_state, '') ORDER BY count DESC, value",
                parameters,
            ).fetchall()
            conversation_states = connection.execute(
                f"SELECT COALESCE(conversation_state, '') AS value, COUNT(*) AS count FROM buyer_run_projects rp WHERE {clause} GROUP BY COALESCE(conversation_state, '') ORDER BY count DESC, value",
                parameters,
            ).fetchall()
            tags = connection.execute(
                f"""
                SELECT shortlist_tag.tag AS value, COUNT(DISTINCT rp.project_id) AS count
                FROM buyer_run_projects rp
                JOIN buyer_shortlist_tags shortlist_tag
                    ON shortlist_tag.run_id = rp.run_id AND shortlist_tag.project_id = rp.project_id
                WHERE {clause}
                GROUP BY shortlist_tag.tag ORDER BY count DESC, value
                """,
                parameters,
            ).fetchall()
        return {
            "total": int(summary["total"] or 0),
            "budget": {"min": summary["budget_min"], "max": summary["budget_max"]},
            "preliminary_score": {"min": summary["min_preliminary_score"], "max": summary["max_preliminary_score"]},
            "final_score": {"min": summary["min_final_score"], "max": summary["max_final_score"]},
            "score": {"min": summary["min_effective_score"], "max": summary["max_effective_score"]},
            "categories": [{"value": row["value"], "count": int(row["count"])} for row in categories],
            "shortlist_states": [{"value": row["value"], "count": int(row["count"])} for row in states],
            "attachment_parse_states": [{"value": row["value"], "count": int(row["count"])} for row in attachment_states],
            "attachment_types": [{"value": row["value"], "count": int(row["count"])} for row in attachment_types],
            "proposal_states": [{"value": row["value"], "count": int(row["count"])} for row in proposal_states],
            "conversation_states": [{"value": row["value"], "count": int(row["count"])} for row in conversation_states],
            "tags": [{"value": row["value"], "count": int(row["count"])} for row in tags],
        }

    def _record_project_enrichment_sync(self, run_id: str, project_id: str, payload: JsonDict) -> JsonDict:
        kind = _text(payload.get("kind"))
        source = _text(payload.get("source"))
        if not kind:
            raise BuyerRepositoryValidationError("enrichment kind is required")
        if not source:
            raise BuyerRepositoryValidationError("enrichment source is required")
        state = _text(payload.get("state"), default="completed").casefold()
        if state not in {"completed", "failed", "skipped", "unavailable"}:
            raise BuyerRepositoryValidationError("enrichment state is invalid")
        normalized_value = payload.get("normalized", {})
        if not isinstance(normalized_value, Mapping):
            raise BuyerRepositoryValidationError("enrichment normalized value must be an object")
        normalized = _redact(dict(normalized_value))
        error = _optional_text(payload.get("error"))
        if error is not None:
            error = error[:2_000]
        content_hash = _text(payload.get("content_hash"))
        if not content_hash:
            content_hash = hashlib.sha256(
                _json(
                    {
                        "kind": kind,
                        "state": state,
                        "normalized": normalized,
                        "error": error,
                    },
                    default={},
                ).encode("utf-8")
            ).hexdigest()
        enrichment_id = _text(payload.get("enrichment_id")) or _new_id("buyer_enrichment")
        observed_at = _text(payload.get("observed_at")) or _now()
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run(connection, run_id)
                project = connection.execute(
                    "SELECT 1 FROM buyer_run_projects WHERE run_id = ? AND project_id = ?",
                    (run_id, project_id),
                ).fetchone()
                if project is None:
                    raise BuyerSearchRepositoryError(f"project is not in run: {project_id}")
                connection.execute(
                    """
                    INSERT INTO buyer_project_enrichments (
                        enrichment_id, run_id, project_id, kind, source, endpoint, raw_artifact_id,
                        normalized_json, content_hash, state, error, account_registration_id,
                        transport_id, egress_ip, route_generation, observed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, project_id, kind, content_hash) DO UPDATE SET
                        source = excluded.source,
                        endpoint = excluded.endpoint,
                        raw_artifact_id = COALESCE(excluded.raw_artifact_id, buyer_project_enrichments.raw_artifact_id),
                        normalized_json = excluded.normalized_json,
                        state = excluded.state,
                        error = excluded.error,
                        account_registration_id = COALESCE(excluded.account_registration_id, buyer_project_enrichments.account_registration_id),
                        transport_id = COALESCE(excluded.transport_id, buyer_project_enrichments.transport_id),
                        egress_ip = COALESCE(excluded.egress_ip, buyer_project_enrichments.egress_ip),
                        route_generation = COALESCE(excluded.route_generation, buyer_project_enrichments.route_generation),
                        observed_at = excluded.observed_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        enrichment_id,
                        run_id,
                        project_id,
                        kind,
                        source,
                        _optional_text(payload.get("endpoint")),
                        _optional_text(payload.get("raw_artifact_id")),
                        _json(normalized, default={}),
                        content_hash,
                        state,
                        error,
                        _optional_text(payload.get("account_registration_id")),
                        _optional_text(payload.get("transport_id")),
                        _optional_text(payload.get("egress_ip")),
                        _integer(payload.get("route_generation"), default=0) or None,
                        observed_at,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM buyer_project_enrichments
                    WHERE run_id = ? AND project_id = ? AND kind = ? AND content_hash = ?
                    """,
                    (run_id, project_id, kind, content_hash),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._enrichment_record(row)

    def _upsert_attachment_sync(self, run_id: str, project_id: str, payload: JsonDict) -> JsonDict:
        now = _now()
        remote_url = _optional_text(payload.get("remote_url") or payload.get("url"))
        supplied_id = _optional_text(payload.get("attachment_id"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run_project(connection, run_id, project_id)
                existing = None
                if supplied_id:
                    existing = connection.execute(
                        "SELECT * FROM buyer_attachments WHERE attachment_id = ?", (supplied_id,)
                    ).fetchone()
                    if existing is not None and str(existing["project_id"]) != project_id:
                        raise BuyerRepositoryValidationError("attachment_id belongs to another project")
                if existing is None and remote_url:
                    existing = connection.execute(
                        "SELECT * FROM buyer_attachments WHERE project_id = ? AND remote_url = ?",
                        (project_id, remote_url),
                    ).fetchone()

                source_observation_id = _optional_text(payload.get("source_observation_id"))
                if source_observation_id:
                    observation = connection.execute(
                        "SELECT project_id FROM buyer_project_observations WHERE observation_id = ?",
                        (source_observation_id,),
                    ).fetchone()
                    if observation is None or str(observation["project_id"]) != project_id:
                        raise BuyerRepositoryValidationError("source_observation_id must belong to the project")

                if existing is None:
                    attachment_id = supplied_id or _new_id("buyer_attachment")
                    state = _text(payload.get("state"), default="discovered")
                    downloaded_at = _optional_text(payload.get("downloaded_at"))
                    if downloaded_at is None and state.casefold() in {"downloaded", "parsed", "completed"}:
                        downloaded_at = now
                    connection.execute(
                        """
                        INSERT INTO buyer_attachments (
                            attachment_id, project_id, source_observation_id, remote_url, resolved_download_url,
                            filename, content_type, detected_type, size_bytes, sha256, object_ref, state, error,
                            created_at, updated_at, downloaded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            attachment_id,
                            project_id,
                            source_observation_id,
                            remote_url,
                            _optional_text(payload.get("resolved_download_url")),
                            _optional_text(payload.get("filename")),
                            _optional_text(payload.get("content_type") or payload.get("mime_type")),
                            _optional_text(payload.get("detected_type") or payload.get("attachment_type")),
                            _non_negative_integer_or_none(payload.get("size_bytes")),
                            _optional_text(payload.get("sha256")),
                            _optional_text(payload.get("object_ref") or payload.get("storage_ref")),
                            state,
                            _optional_text(payload.get("error")),
                            _text(payload.get("created_at")) or now,
                            now,
                            downloaded_at,
                        ),
                    )
                else:
                    attachment_id = str(existing["attachment_id"])
                    state = _text(payload.get("state"), default=str(existing["state"]))
                    downloaded_at = (
                        _optional_text(payload.get("downloaded_at"))
                        if "downloaded_at" in payload
                        else existing["downloaded_at"]
                    )
                    if downloaded_at is None and state.casefold() in {"downloaded", "parsed", "completed"}:
                        downloaded_at = now
                    connection.execute(
                        """
                        UPDATE buyer_attachments SET
                            source_observation_id = COALESCE(?, source_observation_id),
                            remote_url = COALESCE(?, remote_url),
                            resolved_download_url = COALESCE(?, resolved_download_url),
                            filename = COALESCE(?, filename),
                            content_type = COALESCE(?, content_type),
                            detected_type = COALESCE(?, detected_type),
                            size_bytes = COALESCE(?, size_bytes), sha256 = COALESCE(?, sha256),
                            object_ref = COALESCE(?, object_ref), state = ?,
                            error = ?, updated_at = ?, downloaded_at = ?
                        WHERE attachment_id = ?
                        """,
                        (
                            source_observation_id,
                            remote_url,
                            _optional_text(payload.get("resolved_download_url")),
                            _optional_text(payload.get("filename")),
                            _optional_text(payload.get("content_type") or payload.get("mime_type")),
                            _optional_text(payload.get("detected_type") or payload.get("attachment_type")),
                            _non_negative_integer_or_none(payload.get("size_bytes")),
                            _optional_text(payload.get("sha256")),
                            _optional_text(payload.get("object_ref") or payload.get("storage_ref")),
                            state,
                            _optional_text(payload.get("error")) if "error" in payload else existing["error"],
                            now,
                            downloaded_at,
                            attachment_id,
                        ),
                    )
                self._refresh_attachment_projections(connection, project_id, now)
                row = connection.execute("SELECT * FROM buyer_attachments WHERE attachment_id = ?", (attachment_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._attachment_record(row)

    def _record_attachment_derivative_sync(self, attachment_id: str, payload: JsonDict) -> JsonDict:
        kind = _text(payload.get("kind"))
        if not kind:
            raise BuyerRepositoryValidationError("attachment derivative kind is required")
        now = _now()
        metadata = _redact(payload.get("metadata") or {})
        extracted_text = _optional_text(payload.get("extracted_text") or payload.get("text"))
        content_hash = _optional_text(payload.get("content_hash")) or hashlib.sha256(
            _json({"kind": kind, "metadata": metadata, "text": extracted_text}, default={}).encode("utf-8")
        ).hexdigest()
        supplied_id = _optional_text(payload.get("derivative_id"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                attachment = connection.execute(
                    "SELECT * FROM buyer_attachments WHERE attachment_id = ?", (attachment_id,)
                ).fetchone()
                if attachment is None:
                    raise BuyerSearchRepositoryError(f"attachment not found: {attachment_id}")
                existing = None
                if supplied_id:
                    existing = connection.execute(
                        "SELECT * FROM buyer_attachment_derivatives WHERE derivative_id = ?", (supplied_id,)
                    ).fetchone()
                    if existing is not None and str(existing["attachment_id"]) != attachment_id:
                        raise BuyerRepositoryValidationError("derivative_id belongs to another attachment")
                if existing is None:
                    existing = connection.execute(
                        "SELECT * FROM buyer_attachment_derivatives WHERE attachment_id = ? AND kind = ? AND content_hash = ?",
                        (attachment_id, kind, content_hash),
                    ).fetchone()
                state = _text(payload.get("state"), default="parsed" if extracted_text else "queued")
                if existing is None:
                    derivative_id = supplied_id or _new_id("buyer_derivative")
                    connection.execute(
                        """
                        INSERT INTO buyer_attachment_derivatives (
                            derivative_id, attachment_id, kind, parser_name, parser_version, model, model_version,
                            content_hash, extracted_text, metadata_json, token_count, state, error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            derivative_id,
                            attachment_id,
                            kind,
                            _optional_text(payload.get("parser_name") or payload.get("parser")),
                            _optional_text(payload.get("parser_version")),
                            _optional_text(payload.get("model")),
                            _optional_text(payload.get("model_version")),
                            content_hash,
                            extracted_text,
                            _json(metadata, default={}),
                            _non_negative_integer_or_none(payload.get("token_count")),
                            state,
                            _optional_text(payload.get("error")),
                            _text(payload.get("created_at")) or now,
                            now,
                        ),
                    )
                else:
                    derivative_id = str(existing["derivative_id"])
                    connection.execute(
                        """
                        UPDATE buyer_attachment_derivatives SET
                            parser_name = COALESCE(?, parser_name), parser_version = COALESCE(?, parser_version),
                            model = COALESCE(?, model), model_version = COALESCE(?, model_version),
                            extracted_text = COALESCE(?, extracted_text), metadata_json = ?,
                            token_count = COALESCE(?, token_count), state = ?, error = ?, updated_at = ?
                        WHERE derivative_id = ?
                        """,
                        (
                            _optional_text(payload.get("parser_name") or payload.get("parser")),
                            _optional_text(payload.get("parser_version")),
                            _optional_text(payload.get("model")),
                            _optional_text(payload.get("model_version")),
                            extracted_text,
                            _json(metadata, default={}),
                            _non_negative_integer_or_none(payload.get("token_count")),
                            state,
                            _optional_text(payload.get("error")) if "error" in payload else existing["error"],
                            now,
                            derivative_id,
                        ),
                    )
                self._refresh_attachment_projections(connection, str(attachment["project_id"]), now)
                row = connection.execute(
                    "SELECT * FROM buyer_attachment_derivatives WHERE derivative_id = ?", (derivative_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._derivative_record(row)

    def _record_score_sync(self, run_id: str, project_id: str, payload: JsonDict) -> JsonDict:
        total_score = _number(payload.get("total_score", payload.get("total", payload.get("score"))))
        if total_score is None or not math.isfinite(total_score) or not 0 <= total_score <= 100:
            raise BuyerRepositoryValidationError("score total must be a finite number between 0 and 100")
        score_kind = _score_kind(payload.get("score_kind") or payload.get("kind"), payload)
        profile_id = _text(payload.get("score_profile_id") or payload.get("profile_id"))
        profile_version = _text(payload.get("score_profile_version") or payload.get("profile_version") or payload.get("version"))
        provider = _text(payload.get("provider"))
        model = _text(payload.get("model"))
        prompt_version = _text(payload.get("prompt_version"))
        input_context_hash = _text(payload.get("input_context_hash") or payload.get("context_hash"))
        breakdown = _redact(payload.get("breakdown") or payload.get("feature_breakdown") or {})
        if not isinstance(breakdown, Mapping):
            raise BuyerRepositoryValidationError("score breakdown must be an object")
        now = _now()
        supplied_id = _optional_text(payload.get("score_id"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run_project(connection, run_id, project_id)
                existing = None
                if supplied_id:
                    existing = connection.execute("SELECT * FROM buyer_scores WHERE score_id = ?", (supplied_id,)).fetchone()
                    if existing is not None and (str(existing["run_id"]) != run_id or str(existing["project_id"]) != project_id):
                        raise BuyerRepositoryValidationError("score_id belongs to another run/project")
                if existing is None:
                    existing = connection.execute(
                        """
                        SELECT * FROM buyer_scores
                        WHERE run_id = ? AND project_id = ? AND score_kind = ? AND score_profile_id = ?
                          AND score_profile_version = ? AND provider = ? AND model = ?
                          AND prompt_version = ? AND input_context_hash = ?
                        """,
                        (run_id, project_id, score_kind, profile_id, profile_version, provider, model, prompt_version, input_context_hash),
                    ).fetchone()
                if existing is None:
                    score_id = supplied_id or _new_id("buyer_score")
                    connection.execute(
                        """
                        INSERT INTO buyer_scores (
                            score_id, run_id, project_id, score_kind, score_profile_id, score_profile_version,
                            provider, model, prompt_version, input_context_hash, total_score, breakdown_json,
                            rationale, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            score_id,
                            run_id,
                            project_id,
                            score_kind,
                            profile_id,
                            profile_version,
                            provider,
                            model,
                            prompt_version,
                            input_context_hash,
                            total_score,
                            _json(breakdown, default={}),
                            _optional_text(payload.get("rationale")),
                            _text(payload.get("created_at")) or now,
                        ),
                    )
                else:
                    score_id = str(existing["score_id"])
                    connection.execute(
                        """
                        UPDATE buyer_scores SET total_score = ?, breakdown_json = ?, rationale = ?
                        WHERE score_id = ?
                        """,
                        (
                            total_score,
                            _json(breakdown, default={}),
                            _optional_text(payload.get("rationale")) if "rationale" in payload else existing["rationale"],
                            score_id,
                        ),
                    )
                self._refresh_score_projection(connection, run_id, project_id, now)
                row = connection.execute("SELECT * FROM buyer_scores WHERE score_id = ?", (score_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._score_record(row)

    def _set_shortlist_sync(self, run_id: str, project_id: str, payload: JsonDict) -> JsonDict:
        now = _now()
        state = _text(payload.get("state"), default="shortlisted").casefold()
        if state in {"none", "unshortlisted", "unshortlist"}:
            state = "removed"
        rank = _non_negative_integer_or_none(payload.get("rank"))
        selected_by = _optional_text(payload.get("selected_by") or payload.get("author"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run_project(connection, run_id, project_id)
                existing = connection.execute(
                    "SELECT * FROM buyer_shortlist_items WHERE run_id = ? AND project_id = ?", (run_id, project_id)
                ).fetchone()
                selected_at = _optional_text(payload.get("selected_at"))
                if selected_at is None:
                    selected_at = existing["selected_at"] if existing is not None else now
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO buyer_shortlist_items (
                            run_id, project_id, state, rank, selected_by, selected_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, project_id, state, rank, selected_by, selected_at, now, now),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE buyer_shortlist_items SET state = ?, rank = COALESCE(?, rank),
                            selected_by = COALESCE(?, selected_by), selected_at = ?, updated_at = ?
                        WHERE run_id = ? AND project_id = ?
                        """,
                        (state, rank, selected_by, selected_at, now, run_id, project_id),
                    )
                if "tags" in payload:
                    tags = _normalized_tags(payload.get("tags"))
                    connection.execute(
                        "DELETE FROM buyer_shortlist_tags WHERE run_id = ? AND project_id = ?", (run_id, project_id)
                    )
                    connection.executemany(
                        "INSERT INTO buyer_shortlist_tags (run_id, project_id, tag, created_at) VALUES (?, ?, ?, ?)",
                        [(run_id, project_id, tag, now) for tag in tags],
                    )
                for note_payload in _shortlist_note_payloads(payload, selected_by):
                    note_id = _optional_text(note_payload.get("note_id")) or _new_id("buyer_note")
                    note = _text(note_payload.get("body"))
                    if not note:
                        continue
                    note_existing = connection.execute(
                        "SELECT note_id FROM buyer_shortlist_notes WHERE note_id = ?", (note_id,)
                    ).fetchone()
                    if note_existing is None:
                        connection.execute(
                            """
                            INSERT INTO buyer_shortlist_notes (
                                note_id, run_id, project_id, body, author, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (note_id, run_id, project_id, note, _optional_text(note_payload.get("author")) or selected_by, now, now),
                        )
                    else:
                        connection.execute(
                            "UPDATE buyer_shortlist_notes SET body = ?, author = ?, updated_at = ? WHERE note_id = ?",
                            (note, _optional_text(note_payload.get("author")) or selected_by, now, note_id),
                        )
                self._refresh_shortlist_projection(connection, run_id, project_id, now)
                row = connection.execute(
                    "SELECT * FROM buyer_shortlist_items WHERE run_id = ? AND project_id = ?", (run_id, project_id)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._shortlist_record(row, tags=self._shortlist_tags_sync(run_id, project_id), notes=self._shortlist_notes_sync(run_id, project_id))

    def _create_export_sync(self, run_id: str, payload: JsonDict) -> JsonDict:
        export_id = _optional_text(payload.get("export_id")) or _new_id("buyer_export")
        export_format = _text(payload.get("format"))
        if not export_format:
            raise BuyerRepositoryValidationError("export format is required")
        selection = payload.get("selection")
        if selection is None:
            selection = {
                "filters": payload.get("filters") or {},
                "selected_project_ids": payload.get("selected_project_ids") or [],
            }
        if not isinstance(selection, Mapping):
            raise BuyerRepositoryValidationError("export selection must be an object")
        manifest = payload.get("manifest")
        if manifest is not None and not isinstance(manifest, Mapping):
            raise BuyerRepositoryValidationError("export manifest must be an object")
        now = _now()
        state = _text(payload.get("state"), default="queued")
        progress = _progress_payload(payload.get("progress"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_run(connection, run_id)
                existing = connection.execute("SELECT * FROM buyer_exports WHERE export_id = ?", (export_id,)).fetchone()
                if existing is not None:
                    if str(existing["run_id"]) != run_id:
                        raise BuyerRepositoryValidationError("export_id belongs to another run")
                    connection.execute("COMMIT")
                    return self._export_record(existing)
                manifest_json = _json(_redact(manifest), default={}) if manifest is not None else None
                manifest_hash = _optional_text(payload.get("manifest_hash"))
                if manifest_hash is None and manifest_json is not None:
                    manifest_hash = f"sha256:{hashlib.sha256(manifest_json.encode('utf-8')).hexdigest()}"
                completed_at = _optional_text(payload.get("completed_at"))
                if completed_at is None and state.casefold() == "completed":
                    completed_at = now
                connection.execute(
                    """
                    INSERT INTO buyer_exports (
                        export_id, run_id, selection_json, format, include_attachments, include_raw, state,
                        progress_json, object_ref, filename, content_type, bytes, manifest_json, manifest_hash,
                        sha256, error, created_by, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        export_id,
                        run_id,
                        _json(_redact(selection), default={}),
                        export_format,
                        int(bool(payload.get("include_attachments", False))),
                        int(bool(payload.get("include_raw", False))),
                        state,
                        _json(progress, default={}),
                        _optional_text(payload.get("object_ref") or payload.get("path")),
                        _optional_text(payload.get("filename")),
                        _optional_text(payload.get("content_type")),
                        _non_negative_integer_or_none(payload.get("bytes")),
                        manifest_json,
                        manifest_hash,
                        _optional_text(payload.get("sha256")),
                        _optional_text(payload.get("error")),
                        _optional_text(payload.get("created_by")),
                        _text(payload.get("created_at")) or now,
                        now,
                        completed_at,
                    ),
                )
                row = connection.execute("SELECT * FROM buyer_exports WHERE export_id = ?", (export_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._export_record(row)

    def _get_export_sync(self, run_id: str, export_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM buyer_exports WHERE run_id = ? AND export_id = ?", (run_id, export_id)
            ).fetchone()
        if row is None:
            raise BuyerSearchRepositoryError(f"export not found: {export_id}")
        return self._export_record(row)

    def _update_export_sync(self, run_id: str, export_id: str, changes: JsonDict) -> JsonDict:
        allowed = {
            "state",
            "progress",
            "object_ref",
            "path",
            "filename",
            "content_type",
            "bytes",
            "manifest",
            "manifest_hash",
            "sha256",
            "error",
            "completed_at",
        }
        unknown = set(changes).difference(allowed)
        if unknown:
            raise BuyerRepositoryValidationError(f"unsupported export fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self._get_export_sync(run_id, export_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM buyer_exports WHERE run_id = ? AND export_id = ?", (run_id, export_id)
                ).fetchone()
                if row is None:
                    raise BuyerSearchRepositoryError(f"export not found: {export_id}")
                state = _text(changes.get("state"), default=str(row["state"]))
                manifest = changes.get("manifest") if "manifest" in changes else _from_json(row["manifest_json"], default=None)
                if manifest is not None and not isinstance(manifest, Mapping):
                    raise BuyerRepositoryValidationError("export manifest must be an object")
                manifest_json = _json(_redact(manifest), default={}) if manifest is not None else None
                manifest_hash = _optional_text(changes.get("manifest_hash")) if "manifest_hash" in changes else row["manifest_hash"]
                if manifest_hash is None and manifest_json is not None:
                    manifest_hash = f"sha256:{hashlib.sha256(manifest_json.encode('utf-8')).hexdigest()}"
                progress = _progress_payload(changes.get("progress")) if "progress" in changes else _from_json(row["progress_json"], default={})
                completed_at = _optional_text(changes.get("completed_at")) if "completed_at" in changes else row["completed_at"]
                if completed_at is None and state.casefold() == "completed":
                    completed_at = now
                connection.execute(
                    """
                    UPDATE buyer_exports SET state = ?, progress_json = ?, object_ref = ?, filename = ?,
                        content_type = ?, bytes = ?, manifest_json = ?, manifest_hash = ?, sha256 = ?,
                        error = ?, completed_at = ?, updated_at = ?
                    WHERE export_id = ?
                    """,
                    (
                        state,
                        _json(progress, default={}),
                        _optional_text(changes.get("object_ref") or changes.get("path")) if {"object_ref", "path"} & set(changes) else row["object_ref"],
                        _optional_text(changes.get("filename")) if "filename" in changes else row["filename"],
                        _optional_text(changes.get("content_type")) if "content_type" in changes else row["content_type"],
                        _non_negative_integer_or_none(changes.get("bytes")) if "bytes" in changes else row["bytes"],
                        manifest_json,
                        manifest_hash,
                        _optional_text(changes.get("sha256")) if "sha256" in changes else row["sha256"],
                        _optional_text(changes.get("error")) if "error" in changes else row["error"],
                        completed_at,
                        now,
                        export_id,
                    ),
                )
                updated = connection.execute("SELECT * FROM buyer_exports WHERE export_id = ?", (export_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._export_record(updated)

    def _store_raw_artifact_sync(self, payload: JsonDict) -> JsonDict:
        source = _text(payload.get("source"))
        if not source:
            raise BuyerRepositoryValidationError("artifact source is required")
        artifact_id = _text(payload.get("artifact_id")) or _new_id("buyer_artifact")
        body = _redact(payload.get("body"))
        body_json = _json(body, default=None) if body is not None else None
        digest = hashlib.sha256((body_json or "").encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO buyer_raw_artifacts (
                    artifact_id, source, endpoint, request_fingerprint, account_registration_id, transport_id,
                    egress_ip, route_generation, status_code, headers_json, body_json, content_type,
                    sha256, parser_version, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, source, _optional_text(payload.get("endpoint")), _optional_text(payload.get("request_fingerprint")),
                 _optional_text(payload.get("account_registration_id")), _optional_text(payload.get("transport_id")),
                 _optional_text(payload.get("egress_ip")), _integer(payload.get("route_generation"), default=0) or None,
                 _integer(payload.get("status_code"), default=0) or None, _json(_safe_headers(payload.get("headers")), default={}),
                 body_json, _optional_text(payload.get("content_type")), digest, _optional_text(payload.get("parser_version")),
                 _text(payload.get("observed_at")) or _now()),
            )
            row = connection.execute("SELECT * FROM buyer_raw_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return self._artifact_record(row)

    def _get_raw_artifact_sync(self, artifact_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM buyer_raw_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise BuyerSearchRepositoryError(f"artifact not found: {artifact_id}")
        return self._artifact_record(row)

    def _append_event_sync(self, run_id: str, event_type: str, payload: JsonDict) -> JsonDict:
        if not event_type:
            raise BuyerRepositoryValidationError("event_type is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_event_in_transaction(connection, run_id, event_type, payload)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return event

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        """Append an event while the caller already owns a repository transaction."""

        if not event_type:
            raise BuyerRepositoryValidationError("event_type is required")
        self._require_run(connection, run_id)
        next_seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM buyer_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()["value"]
        )
        created_at = _now()
        event_payload = dict(payload)
        connection.execute(
            "INSERT INTO buyer_events (run_id, sequence, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, next_seq, event_type, _json(event_payload, default={}), created_at),
        )
        return {
            "run_id": run_id,
            "sequence": next_seq,
            "event_type": event_type,
            "payload": event_payload,
            "created_at": created_at,
        }

    def _replay_events_sync(self, run_id: str, after_seq: int, limit: int) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM buyer_events WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?", (run_id, after_seq, limit)).fetchall()
        return [
            {"run_id": row["run_id"], "sequence": int(row["sequence"]), "event_type": row["event_type"], "payload": _from_json(row["payload_json"], default={}), "created_at": row["created_at"]}
            for row in rows
        ]

    def _upsert_project(self, connection: sqlite3.Connection, card: JsonDict, observed_at: str) -> tuple[str, bool]:
        platform = _text(card.get("platform"), default="kwork")
        remote_id = _text(card.get("remote_project_id") or card.get("id"))
        if not remote_id:
            raise BuyerRepositoryValidationError("project remote_project_id/id is required")
        row = connection.execute("SELECT buyer_project_id FROM buyer_projects WHERE platform = ? AND remote_project_id = ?", (platform, remote_id)).fetchone()
        title = _optional_text(card.get("title"))
        description = _optional_text(card.get("description"))
        if row is None:
            project_id = _new_id("buyer_project")
            connection.execute(
                """
                INSERT INTO buyer_projects (
                    buyer_project_id, platform, remote_project_id, canonical_url, latest_title, latest_description,
                    latest_status, latest_category_id, buyer_remote_user_id, buyer_username,
                    first_seen_at, last_seen_at, latest_remote_updated_at, canonical_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, platform, remote_id, _optional_text(card.get("canonical_url") or card.get("url")), title, description,
                 _optional_text(card.get("status") or card.get("remote_status")), _optional_text(card.get("category_id")),
                 _optional_text(card.get("buyer_remote_user_id") or card.get("user_id")), _optional_text(card.get("buyer_username") or card.get("username")),
                 observed_at, observed_at, _optional_text(card.get("remote_updated_at")),
                 hashlib.sha256(_json({"title": title, "description": description}, default={}).encode("utf-8")).hexdigest()),
            )
            return project_id, True
        project_id = str(row["buyer_project_id"])
        connection.execute(
            """
            UPDATE buyer_projects SET canonical_url = COALESCE(?, canonical_url), latest_title = COALESCE(?, latest_title),
                latest_description = COALESCE(?, latest_description), latest_status = COALESCE(?, latest_status),
                latest_category_id = COALESCE(?, latest_category_id), buyer_remote_user_id = COALESCE(?, buyer_remote_user_id),
                buyer_username = COALESCE(?, buyer_username), last_seen_at = ?, latest_remote_updated_at = COALESCE(?, latest_remote_updated_at),
                canonical_hash = ? WHERE buyer_project_id = ?
            """,
            (_optional_text(card.get("canonical_url") or card.get("url")), title, description,
             _optional_text(card.get("status") or card.get("remote_status")), _optional_text(card.get("category_id")),
             _optional_text(card.get("buyer_remote_user_id") or card.get("user_id")), _optional_text(card.get("buyer_username") or card.get("username")),
             observed_at, _optional_text(card.get("remote_updated_at")),
             hashlib.sha256(_json({"title": title, "description": description}, default={}).encode("utf-8")).hexdigest(), project_id),
        )
        return project_id, False

    def _insert_observation(self, connection: sqlite3.Connection, task: sqlite3.Row, project_id: str, card: JsonDict, source: str, position: int, raw_artifact_id: str | None, observed_at: str) -> str:
        observation_id = _new_id("buyer_observation")
        budget_min = _number(card.get("budget_min"))
        budget_max = _number(card.get("budget_max"))
        price = _number(card.get("price"))
        if budget_min is None:
            budget_min = price
        if budget_max is None:
            budget_max = _number(card.get("possible_price_limit")) or price
        normalized_hash = hashlib.sha256(_json(_redact(card), default={}).encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO buyer_project_observations (
                observation_id, project_id, run_id, query_id, task_id, attempt_id, worker_id,
                account_registration_id, transport_id, egress_ip, route_generation, source, page,
                response_position, observed_at, title, description, budget_min, budget_max, offers,
                views, orders, remote_status, category_id, parent_category_id, buyer_hired_percent,
                buyer_projects_count, buyer_active_projects_count, expires_at, raw_artifact_id, normalized_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (observation_id, project_id, task["run_id"], task["query_id"], task["task_id"], task["attempt_id"], task["lease_owner"],
             task["account_registration_id"], task["transport_id"], task["egress_ip"], task["route_generation"], source, task["page"], position,
             observed_at, _optional_text(card.get("title")), _optional_text(card.get("description")), budget_min, budget_max,
             _integer(card.get("offers"), default=0) if card.get("offers") is not None else None,
             _integer(card.get("views"), default=0) if card.get("views") is not None else None,
             _integer(card.get("orders"), default=0) if card.get("orders") is not None else None,
             _optional_text(card.get("status") or card.get("remote_status")), _optional_text(card.get("category_id")),
             _optional_text(card.get("parent_category_id")), _number(card.get("buyer_hired_percent") or card.get("user_hired_percent")),
             _integer(card.get("buyer_projects_count") or card.get("user_projects_count"), default=0) if (card.get("buyer_projects_count") is not None or card.get("user_projects_count") is not None) else None,
             _integer(card.get("buyer_active_projects_count") or card.get("user_active_projects_count"), default=0) if (card.get("buyer_active_projects_count") is not None or card.get("user_active_projects_count") is not None) else None,
             _optional_text(card.get("expires_at")), raw_artifact_id, normalized_hash),
        )
        return observation_id

    def _upsert_match(self, connection: sqlite3.Connection, run_id: str, project_id: str, query_id: str, observation_id: str, observed_at: str) -> bool:
        existing = connection.execute("SELECT 1 FROM buyer_project_matches WHERE run_id = ? AND project_id = ? AND query_id = ?", (run_id, project_id, query_id)).fetchone()
        if existing is None:
            connection.execute("INSERT INTO buyer_project_matches (run_id, project_id, query_id, first_observation_id, last_observation_id, match_count, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)", (run_id, project_id, query_id, observation_id, observation_id, observed_at, observed_at))
            return True
        connection.execute("UPDATE buyer_project_matches SET last_observation_id = ?, match_count = match_count + 1, last_seen_at = ? WHERE run_id = ? AND project_id = ? AND query_id = ?", (observation_id, observed_at, run_id, project_id, query_id))
        return False

    def _upsert_run_project(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        project_id: str,
        observation_id: str,
        card: JsonDict,
        observed_at: str,
    ) -> bool:
        was_new = connection.execute(
            "SELECT 1 FROM buyer_run_projects WHERE run_id = ? AND project_id = ?", (run_id, project_id)
        ).fetchone() is None
        count = int(connection.execute("SELECT COUNT(*) AS count FROM buyer_project_matches WHERE run_id = ? AND project_id = ?", (run_id, project_id)).fetchone()["count"])
        budget_min = _number(card.get("budget_min")) or _number(card.get("price"))
        budget_max = _number(card.get("budget_max")) or _number(card.get("possible_price_limit")) or budget_min
        attachment_count, attachment_parse_state = self._attachment_projection(connection, project_id)
        connection.execute(
            """
            INSERT INTO buyer_run_projects (
                run_id, project_id, latest_observation_id, title, description_excerpt, budget_min, budget_max,
                offers, views, age_seconds, category_id, category_path, buyer_hired_percent,
                attachment_count, attachment_parse_state, matched_query_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, project_id) DO UPDATE SET
                latest_observation_id = excluded.latest_observation_id, title = COALESCE(excluded.title, buyer_run_projects.title),
                description_excerpt = COALESCE(excluded.description_excerpt, buyer_run_projects.description_excerpt),
                budget_min = COALESCE(excluded.budget_min, buyer_run_projects.budget_min),
                budget_max = COALESCE(excluded.budget_max, buyer_run_projects.budget_max),
                offers = COALESCE(excluded.offers, buyer_run_projects.offers), views = COALESCE(excluded.views, buyer_run_projects.views),
                category_id = COALESCE(excluded.category_id, buyer_run_projects.category_id),
                category_path = COALESCE(excluded.category_path, buyer_run_projects.category_path),
                buyer_hired_percent = COALESCE(excluded.buyer_hired_percent, buyer_run_projects.buyer_hired_percent),
                attachment_count = excluded.attachment_count,
                attachment_parse_state = excluded.attachment_parse_state,
                matched_query_count = excluded.matched_query_count, updated_at = excluded.updated_at
            """,
            (run_id, project_id, observation_id, _optional_text(card.get("title")), _optional_text(card.get("description"))[:500] if _optional_text(card.get("description")) else None,
             budget_min, budget_max, _integer(card.get("offers"), default=0) if card.get("offers") is not None else None,
             _integer(card.get("views"), default=0) if card.get("views") is not None else None,
             _integer(card.get("age_seconds"), default=0) if card.get("age_seconds") is not None else None,
             _optional_text(card.get("category_id")), _optional_text(card.get("category_path")), _number(card.get("buyer_hired_percent") or card.get("user_hired_percent")),
             attachment_count, attachment_parse_state, count, observed_at),
        )
        return was_new

    @staticmethod
    def _attachment_projection(connection: sqlite3.Connection, project_id: str) -> tuple[int, str | None]:
        attachments = connection.execute(
            "SELECT attachment_id, state FROM buyer_attachments WHERE project_id = ?", (project_id,)
        ).fetchall()
        attachment_count = len(attachments)
        if attachment_count == 0:
            return 0, None
        attachment_ids = [str(row["attachment_id"]) for row in attachments]
        placeholders = ", ".join("?" for _ in attachment_ids)
        derivative_rows = connection.execute(
            f"SELECT attachment_id, state FROM buyer_attachment_derivatives WHERE attachment_id IN ({placeholders})",
            attachment_ids,
        ).fetchall()
        parsed_attachment_ids = {
            str(row["attachment_id"])
            for row in attachments
            if _text(row["state"]).casefold() in {"parsed", "completed"}
        }
        derivative_states: list[str] = []
        for derivative in derivative_rows:
            state = _text(derivative["state"]).casefold()
            derivative_states.append(state)
            if state in {"parsed", "completed"}:
                parsed_attachment_ids.add(str(derivative["attachment_id"]))
        attachment_states = [_text(row["state"]).casefold() for row in attachments]
        if len(parsed_attachment_ids) == attachment_count:
            return attachment_count, "parsed"
        if parsed_attachment_ids:
            return attachment_count, "partial"
        states = set((*attachment_states, *derivative_states))
        if states.intersection({"failed", "error", "corrupt", "unsupported", "too_large"}):
            return attachment_count, "failed"
        if states.intersection({"parsing", "queued", "running", "downloading"}):
            return attachment_count, "processing"
        if states.intersection({"downloaded", "pending"}):
            return attachment_count, "pending"
        return attachment_count, "discovered"

    def _refresh_attachment_projections(self, connection: sqlite3.Connection, project_id: str, updated_at: str) -> None:
        attachment_count, attachment_parse_state = self._attachment_projection(connection, project_id)
        connection.execute(
            """
            UPDATE buyer_run_projects
            SET attachment_count = ?, attachment_parse_state = ?, updated_at = ?
            WHERE project_id = ?
            """,
            (attachment_count, attachment_parse_state, updated_at, project_id),
        )

    @staticmethod
    def _refresh_score_projection(connection: sqlite3.Connection, run_id: str, project_id: str, updated_at: str) -> None:
        preliminary = connection.execute(
            """
            SELECT total_score FROM buyer_scores
            WHERE run_id = ? AND project_id = ? AND score_kind = 'preliminary'
            ORDER BY created_at DESC, score_id DESC LIMIT 1
            """,
            (run_id, project_id),
        ).fetchone()
        final = connection.execute(
            """
            SELECT total_score FROM buyer_scores
            WHERE run_id = ? AND project_id = ? AND score_kind = 'final'
            ORDER BY created_at DESC, score_id DESC LIMIT 1
            """,
            (run_id, project_id),
        ).fetchone()
        connection.execute(
            """
            UPDATE buyer_run_projects
            SET preliminary_score = ?, final_score = ?, updated_at = ?
            WHERE run_id = ? AND project_id = ?
            """,
            (
                preliminary["total_score"] if preliminary is not None else None,
                final["total_score"] if final is not None else None,
                updated_at,
                run_id,
                project_id,
            ),
        )

    @staticmethod
    def _refresh_shortlist_projection(connection: sqlite3.Connection, run_id: str, project_id: str, updated_at: str) -> None:
        shortlist = connection.execute(
            "SELECT state FROM buyer_shortlist_items WHERE run_id = ? AND project_id = ?", (run_id, project_id)
        ).fetchone()
        tags = [
            str(row["tag"])
            for row in connection.execute(
                "SELECT tag FROM buyer_shortlist_tags WHERE run_id = ? AND project_id = ? ORDER BY tag",
                (run_id, project_id),
            ).fetchall()
        ]
        state = str(shortlist["state"]) if shortlist is not None else None
        if state == "removed":
            state = None
        connection.execute(
            """
            UPDATE buyer_run_projects
            SET shortlist_state = ?, tags_json = ?, updated_at = ?
            WHERE run_id = ? AND project_id = ?
            """,
            (state, _json(tags, default=[]), updated_at, run_id, project_id),
        )

    @staticmethod
    def _require_run_project(connection: sqlite3.Connection, run_id: str, project_id: str) -> sqlite3.Row:
        BuyerSearchRepository._require_run(connection, run_id)
        row = connection.execute(
            "SELECT * FROM buyer_run_projects WHERE run_id = ? AND project_id = ?", (run_id, project_id)
        ).fetchone()
        if row is None:
            raise BuyerSearchRepositoryError(f"project is not in run: {project_id}")
        return row

    def _shortlist_tags_sync(self, run_id: str, project_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tag FROM buyer_shortlist_tags WHERE run_id = ? AND project_id = ? ORDER BY tag",
                (run_id, project_id),
            ).fetchall()
        return [str(row["tag"]) for row in rows]

    def _shortlist_notes_sync(self, run_id: str, project_id: str) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM buyer_shortlist_notes WHERE run_id = ? AND project_id = ?
                ORDER BY created_at, note_id
                """,
                (run_id, project_id),
            ).fetchall()
        return [self._shortlist_note_record(row) for row in rows]

    @staticmethod
    def _project_filters(run_id: str, filters: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
        where = ["rp.run_id = ?"]
        parameters: list[Any] = [run_id]
        text = _optional_text(filters.get("text"))
        if text:
            where.append("(COALESCE(rp.title, '') LIKE ? OR COALESCE(rp.description_excerpt, '') LIKE ?)")
            parameters.extend([f"%{text}%", f"%{text}%"])
        category_id = _optional_text(filters.get("category_id"))
        if category_id:
            where.append("rp.category_id = ?")
            parameters.append(category_id)
        if filters.get("min_budget") is not None:
            where.append("COALESCE(rp.budget_max, rp.budget_min) >= ?")
            parameters.append(_filter_number(filters.get("min_budget"), "min_budget"))
        if filters.get("max_budget") is not None:
            where.append("COALESCE(rp.budget_max, rp.budget_min) <= ?")
            parameters.append(_filter_number(filters.get("max_budget"), "max_budget"))
        if filters.get("min_offers") is not None:
            where.append("COALESCE(rp.offers, 0) >= ?")
            parameters.append(_filter_number(filters.get("min_offers"), "min_offers"))
        if filters.get("max_offers") is not None:
            where.append("COALESCE(rp.offers, 0) <= ?")
            parameters.append(_filter_number(filters.get("max_offers"), "max_offers"))
        if filters.get("min_views") is not None:
            where.append("COALESCE(rp.views, 0) >= ?")
            parameters.append(_filter_number(filters.get("min_views"), "min_views"))
        if filters.get("max_views") is not None:
            where.append("COALESCE(rp.views, 0) <= ?")
            parameters.append(_filter_number(filters.get("max_views"), "max_views"))
        if filters.get("min_buyer_hired_percent") is not None:
            where.append("COALESCE(rp.buyer_hired_percent, 0) >= ?")
            parameters.append(
                _filter_number(filters.get("min_buyer_hired_percent"), "min_buyer_hired_percent")
            )
        min_age = filters.get("min_age_seconds", filters.get("min_age"))
        max_age = filters.get("max_age_seconds", filters.get("max_age"))
        if min_age is not None:
            where.append("COALESCE(rp.age_seconds, 0) >= ?")
            parameters.append(_filter_number(min_age, "min_age_seconds"))
        if max_age is not None:
            where.append("COALESCE(rp.age_seconds, 0) <= ?")
            parameters.append(_filter_number(max_age, "max_age_seconds"))
        effective_score = "COALESCE(rp.final_score, rp.preliminary_score)"
        min_score = filters.get("min_score", filters.get("score_gte"))
        max_score = filters.get("max_score", filters.get("score_lte"))
        if min_score is not None:
            where.append(f"{effective_score} >= ?")
            parameters.append(_filter_number(min_score, "min_score"))
        if max_score is not None:
            where.append(f"{effective_score} <= ?")
            parameters.append(_filter_number(max_score, "max_score"))
        if filters.get("min_preliminary_score") is not None:
            where.append("rp.preliminary_score >= ?")
            parameters.append(_filter_number(filters.get("min_preliminary_score"), "min_preliminary_score"))
        if filters.get("max_preliminary_score") is not None:
            where.append("rp.preliminary_score <= ?")
            parameters.append(_filter_number(filters.get("max_preliminary_score"), "max_preliminary_score"))
        if filters.get("min_final_score") is not None:
            where.append("rp.final_score >= ?")
            parameters.append(_filter_number(filters.get("min_final_score"), "min_final_score"))
        if filters.get("max_final_score") is not None:
            where.append("rp.final_score <= ?")
            parameters.append(_filter_number(filters.get("max_final_score"), "max_final_score"))
        if filters.get("shortlisted") is True:
            where.append("COALESCE(rp.shortlist_state, '') != ''")
        if filters.get("shortlisted") is False:
            where.append("COALESCE(rp.shortlist_state, '') = ''")
        shortlist_states = _filter_text_values(filters.get("shortlist_state"), "shortlist_state")
        if shortlist_states:
            placeholders = ", ".join("?" for _ in shortlist_states)
            where.append(f"COALESCE(rp.shortlist_state, '') IN ({placeholders})")
            parameters.extend(shortlist_states)
        if filters.get("has_attachments") is True:
            where.append("rp.attachment_count > 0")
        if filters.get("has_attachments") is False:
            where.append("rp.attachment_count = 0")
        attachment_states = _filter_text_values(filters.get("attachment_parse_state"), "attachment_parse_state")
        if attachment_states:
            placeholders = ", ".join("?" for _ in attachment_states)
            where.append(f"COALESCE(rp.attachment_parse_state, '') IN ({placeholders})")
            parameters.extend(attachment_states)
        attachment_types = _filter_text_values(filters.get("attachment_type"), "attachment_type")
        if attachment_types:
            placeholders = ", ".join("?" for _ in attachment_types)
            where.append(
                f"EXISTS (SELECT 1 FROM buyer_attachments attachment WHERE attachment.project_id = rp.project_id AND COALESCE(attachment.detected_type, '') IN ({placeholders}))"
            )
            parameters.extend(attachment_types)
        tags = _filter_text_values(filters.get("tag", filters.get("tags")), "tag")
        if tags:
            placeholders = ", ".join("?" for _ in tags)
            where.append(
                f"EXISTS (SELECT 1 FROM buyer_shortlist_tags shortlist_tag WHERE shortlist_tag.run_id = rp.run_id AND shortlist_tag.project_id = rp.project_id AND shortlist_tag.tag IN ({placeholders}))"
            )
            parameters.extend(tags)
        proposal_states = _filter_text_values(filters.get("proposal_state"), "proposal_state")
        if proposal_states:
            placeholders = ", ".join("?" for _ in proposal_states)
            where.append(f"COALESCE(rp.proposal_state, '') IN ({placeholders})")
            parameters.extend(proposal_states)
        conversation_states = _filter_text_values(filters.get("conversation_state"), "conversation_state")
        if conversation_states:
            placeholders = ", ".join("?" for _ in conversation_states)
            where.append(f"COALESCE(rp.conversation_state, '') IN ({placeholders})")
            parameters.extend(conversation_states)
        if filters.get("unseen") is True:
            where.append("rp.unseen = 1")
        if filters.get("unseen") is False:
            where.append("rp.unseen = 0")
        return where, parameters

    @staticmethod
    def _require_task_lease(
        task: sqlite3.Row | None,
        worker_id: str,
        attempt_id: str,
        lease_fence: int,
        now: str,
    ) -> sqlite3.Row:
        if task is None:
            raise BuyerQueryTaskLeaseLostError("task not found")
        if (
            task["state"] != "leased"
            or task["lease_owner"] != worker_id
            or task["attempt_id"] != attempt_id
            or int(task["lease_fence"]) != lease_fence
            or not task["lease_deadline"]
            or str(task["lease_deadline"]) <= now
        ):
            raise BuyerQueryTaskLeaseLostError(str(task["task_id"]))
        return task

    @staticmethod
    def _increment_run_counters(
        connection: sqlite3.Connection,
        run_id: str,
        increments: Mapping[str, int],
        *,
        updated_at: str | None = None,
    ) -> JsonDict:
        row = BuyerSearchRepository._require_run(connection, run_id)
        counters = _from_json(row["counters_json"], default={})
        if not isinstance(counters, Mapping):
            counters = {}
        next_counters: JsonDict = dict(counters)
        non_negative = {"queued_tasks", "active_tasks", "retry_tasks"}
        for key, amount in increments.items():
            if not key:
                continue
            value = _integer(next_counters.get(key), default=0) + int(amount)
            next_counters[key] = max(0, value) if key in non_negative else value
        connection.execute(
            "UPDATE buyer_runs SET counters_json = ?, updated_at = ? WHERE run_id = ?",
            (_json(next_counters, default={}), updated_at or _now(), run_id),
        )
        return next_counters

    @staticmethod
    def _refresh_query_terminal_state(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        query_id: str,
        updated_at: str,
    ) -> str:
        """Keep query-plan state truthful after page tasks finish or fail."""

        rows = connection.execute(
            """
            SELECT state, COUNT(*) AS value
            FROM buyer_query_tasks
            WHERE run_id = ? AND query_id = ?
            GROUP BY state
            """,
            (run_id, query_id),
        ).fetchall()
        counts = {str(row["state"]): int(row["value"]) for row in rows}
        active = sum(counts.get(state, 0) for state in ("queued", "leased", "retry_wait"))
        if active:
            state = "running" if counts.get("leased", 0) else "assigned"
        elif counts.get("completed", 0):
            state = "exhausted"
        elif counts.get("failed", 0):
            state = "failed"
        else:
            state = "assigned"
        connection.execute(
            "UPDATE buyer_queries SET state = ?, updated_at = ? WHERE run_id = ? AND query_id = ?",
            (state, updated_at, run_id, query_id),
        )
        return state

    @staticmethod
    def _require_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM buyer_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise BuyerRunNotFoundError(run_id)
        return row

    @staticmethod
    def _run_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["category_scope"] = _from_json(result.pop("category_scope_json"), default=[])
        result["filters"] = _from_json(result.pop("filter_json"), default={})
        result["config"] = _from_json(result.pop("config_json"), default={})
        result["counters"] = _from_json(result.pop("counters_json"), default={})
        result["enrichment_policy"] = _from_json(result["enrichment_policy"], default={})
        return result

    @staticmethod
    def _query_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["filters"] = _from_json(result.pop("filter_json"), default={})
        result["category_id"] = result["category_id"] or None
        result["approved"] = bool(result["approved"])
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _task_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["cursor"] = _from_json(result.pop("cursor_json"), default=None)
        result["commit_result"] = _from_json(result.pop("commit_result_json"), default=None)
        return result

    @staticmethod
    def _quarantine_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["active"] = result.get("state") == "active"
        return result

    @staticmethod
    def _project_record(row: sqlite3.Row) -> JsonDict:
        return dict(row)

    @staticmethod
    def _run_project_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result.pop("_sort_value", None)
        result["tags"] = _from_json(result.pop("tags_json"), default=[])
        result["unseen"] = bool(result["unseen"])
        return result

    @staticmethod
    def _observation_record(row: sqlite3.Row) -> JsonDict:
        return dict(row)

    @staticmethod
    def _match_record(row: sqlite3.Row) -> JsonDict:
        return dict(row)

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["headers"] = _from_json(result.pop("headers_json"), default={})
        result["body"] = _from_json(result.pop("body_json"), default=None)
        return result

    @staticmethod
    def _enrichment_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["normalized"] = _from_json(result.pop("normalized_json"), default={})
        return result

    @staticmethod
    def _attachment_record(row: sqlite3.Row) -> JsonDict:
        return dict(row)

    @staticmethod
    def _derivative_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["metadata"] = _from_json(result.pop("metadata_json"), default={})
        return result

    @staticmethod
    def _score_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["breakdown"] = _from_json(result.pop("breakdown_json"), default={})
        return result

    @staticmethod
    def _shortlist_note_record(row: sqlite3.Row) -> JsonDict:
        return dict(row)

    @staticmethod
    def _shortlist_record(
        row: sqlite3.Row,
        *,
        tags: Sequence[str],
        notes: Sequence[Mapping[str, Any]],
    ) -> JsonDict:
        result = dict(row)
        result["tags"] = list(tags)
        result["notes"] = [dict(note) for note in notes]
        return result

    @staticmethod
    def _export_record(row: sqlite3.Row) -> JsonDict:
        result = dict(row)
        result["selection"] = _from_json(result.pop("selection_json"), default={})
        result["progress"] = _from_json(result.pop("progress_json"), default={})
        result["manifest"] = _from_json(result.pop("manifest_json"), default=None)
        result["include_attachments"] = bool(result["include_attachments"])
        result["include_raw"] = bool(result["include_raw"])
        return result


SqliteBuyerSearchRepository = BuyerSearchRepository


__all__ = [
    "BuyerQueryTaskLeaseLostError",
    "BuyerRepositoryValidationError",
    "BuyerRunNotFoundError",
    "BuyerSearchRepository",
    "BuyerSearchRepositoryError",
    "SqliteBuyerSearchRepository",
]
