"""Core Buyer Search domain types independent from the legacy market flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .query_normalizer import QueryKey, canonical_filter_json, filter_hash, normalize_query_text, stable_query_hash, stable_query_key


class BuyerRunMode(StrEnum):
    BRIEF = "brief"
    CATEGORY = "category"
    MANUAL = "manual"
    HYBRID = "hybrid"


class BuyerRunState(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETING = "completing"
    COMPLETED = "completed"
    STOPPING = "stopping"
    STOPPED = "stopped"
    BLOCKED = "blocked"
    FAILED = "failed"


class BuyerQueryOrigin(StrEnum):
    BRIEF = "brief"
    CATEGORY = "category"
    CATEGORY_BROWSE = "category_browse"
    MANUAL = "manual"
    HYBRID_SEED = "hybrid_seed"
    AI_EXPANSION = "ai_expansion"
    SUGGESTION = "suggestion"
    HISTORICAL = "historical"


class BuyerQueryState(StrEnum):
    GENERATED = "generated"
    APPROVED = "approved"
    ASSIGNED = "assigned"
    RUNNING = "running"
    EXHAUSTED = "exhausted"
    DISABLED = "disabled"
    REJECTED = "rejected"
    FAILED = "failed"


class BuyerOperationKind(StrEnum):
    PLAN_BUYER_QUERIES = "plan_buyer_queries"
    FETCH_BUYER_PROJECTS = "fetch_buyer_projects"
    FETCH_BUYER_PROJECT_DETAIL = "fetch_buyer_project_detail"
    FETCH_BUYER_PROFILE_HISTORY = "fetch_buyer_profile_history"
    FETCH_BUYER_PROJECT_WEB_STATE = "fetch_buyer_project_web_state"
    DOWNLOAD_BUYER_ATTACHMENT = "download_buyer_attachment"
    PARSE_BUYER_ATTACHMENT = "parse_buyer_attachment"
    SCORE_BUYER_PROJECT = "score_buyer_project"
    BUILD_BUYER_EXPORT = "build_buyer_export"
    RECONCILE_BUYER_PROJECT = "reconcile_buyer_project"
    SYNC_BUYER_CONVERSATION = "sync_buyer_conversation"


class BuyerOperationState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class BuyerQuery:
    """One auditable candidate in a run-wide Buyer Search query plan."""

    query_id: str
    run_id: str
    text: str
    origin: BuyerQueryOrigin
    rationale: str
    category_id: int | None
    category_path: tuple[int, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    parent_query_id: str | None = None
    priority: int = 0
    semantic_fingerprint: str | None = None
    predicted_total: int | None = None
    approved: bool = False
    enabled: bool = True
    state: BuyerQueryState = BuyerQueryState.GENERATED
    normalized_text: str = field(init=False)
    filter_json: str = field(init=False)
    filter_hash: str = field(init=False)
    stable_key: QueryKey = field(init=False)
    stable_hash: str = field(init=False)

    def __post_init__(self) -> None:
        query_id = _required_text(self.query_id, "query_id")
        run_id = _required_text(self.run_id, "run_id")
        text = _required_text(self.text, "text")
        rationale = self.rationale.strip() if isinstance(self.rationale, str) else _raise_type("rationale", "a string")
        if self.category_id is not None and (
            isinstance(self.category_id, bool) or not isinstance(self.category_id, int) or self.category_id <= 0
        ):
            raise ValueError("category_id must be a positive integer or None")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("priority must be an integer")
        if self.predicted_total is not None and (
            isinstance(self.predicted_total, bool) or not isinstance(self.predicted_total, int) or self.predicted_total < 0
        ):
            raise ValueError("predicted_total must be a non-negative integer or None")
        if not isinstance(self.approved, bool) or not isinstance(self.enabled, bool):
            raise ValueError("approved and enabled must be booleans")
        normalized_path = tuple(_positive_category_id(value) for value in self.category_path)
        if self.category_id is not None and normalized_path and normalized_path[-1] != self.category_id:
            raise ValueError("category_path must end with category_id")

        filter_json = canonical_filter_json(self.filters)
        normalized_text = normalize_query_text(text)
        if not normalized_text:
            raise ValueError("text must contain non-whitespace characters")

        object.__setattr__(self, "query_id", query_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "category_path", normalized_path)
        object.__setattr__(self, "filters", dict(self.filters))
        object.__setattr__(self, "normalized_text", normalized_text)
        object.__setattr__(self, "filter_json", filter_json)
        object.__setattr__(self, "filter_hash", filter_hash(self.filters))
        object.__setattr__(self, "stable_key", stable_query_key(text, category_id=self.category_id, filters=self.filters))
        object.__setattr__(self, "stable_hash", stable_query_hash(text, category_id=self.category_id, filters=self.filters))


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str):
        return _raise_type(name, "a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} cannot be blank")
    return result


def _positive_category_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("category_path must contain positive integers")
    return value


def _raise_type(name: str, expected: str) -> Any:
    raise TypeError(f"{name} must be {expected}")


__all__ = [
    "BuyerOperationKind",
    "BuyerOperationState",
    "BuyerQuery",
    "BuyerQueryOrigin",
    "BuyerQueryState",
    "BuyerRunMode",
    "BuyerRunState",
]
