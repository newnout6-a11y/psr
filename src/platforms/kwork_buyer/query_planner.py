"""Deterministic, durable-ready planning for Buyer Search queries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Any, TypeAlias
from uuid import NAMESPACE_URL, uuid5

from .models import BuyerQuery, BuyerQueryOrigin, BuyerQueryState, BuyerRunMode
from .query_normalizer import QueryKey, stable_query_key


SemanticScorer: TypeAlias = Callable[[BuyerQuery, BuyerQuery], float]


class BuyerQueryPlanningError(ValueError):
    """Raised when a query plan cannot satisfy its durable invariants."""


class InsufficientBuyerQueriesError(BuyerQueryPlanningError):
    """Raised when exact deduplication leaves too few queries to distribute."""

    def __init__(self, *, required: int, available: int) -> None:
        self.required = required
        self.available = available
        super().__init__(f"query plan requires {required} unique queries, but only {available} are available")


class BuyerQueryCollisionKind(StrEnum):
    EXACT = "exact"
    SEMANTIC = "semantic"


@dataclass(frozen=True, slots=True)
class BuyerQueryCandidate:
    """A planner input, optionally enriched by an AI or taxonomy source."""

    text: str
    rationale: str = ""
    origin: BuyerQueryOrigin | None = None
    category_id: int | None = None
    category_path: tuple[int, ...] | None = None
    filters: Mapping[str, Any] | None = None
    parent_query_id: str | None = None
    priority: int = 0
    semantic_fingerprint: str | None = None
    predicted_total: int | None = None


@dataclass(frozen=True, slots=True)
class BuyerQueryPlanRequest:
    """Inputs necessary to produce a query plan before work is distributed."""

    run_id: str
    mode: BuyerRunMode
    worker_count: int
    queries_per_worker: int
    candidates: tuple[BuyerQueryCandidate, ...]
    category_id: int | None = None
    category_path: tuple[int, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    auto_approve: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id cannot be blank")
        try:
            mode = BuyerRunMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be a BuyerRunMode") from exc
        if isinstance(self.worker_count, bool) or not isinstance(self.worker_count, int):
            raise ValueError("worker_count must be an integer")
        if not 1 <= self.worker_count <= 30:
            raise ValueError("worker_count must be between 1 and 30")
        if isinstance(self.queries_per_worker, bool) or not isinstance(self.queries_per_worker, int):
            raise ValueError("queries_per_worker must be an integer")
        if self.queries_per_worker <= 0:
            raise ValueError("queries_per_worker must be positive")
        if not isinstance(self.auto_approve, bool):
            raise ValueError("auto_approve must be a boolean")
        candidates = tuple(self.candidates)
        if not candidates:
            raise BuyerQueryPlanningError("query plan requires at least one candidate")
        if any(not isinstance(candidate, BuyerQueryCandidate) for candidate in candidates):
            raise TypeError("candidates must contain BuyerQueryCandidate values")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "category_path", tuple(self.category_path))
        object.__setattr__(self, "filters", dict(self.filters))

    @property
    def minimum_query_count(self) -> int:
        return self.worker_count * self.queries_per_worker


@dataclass(frozen=True, slots=True)
class BuyerQueryCollision:
    """A collision emitted for operator review without silently hiding it."""

    kind: BuyerQueryCollisionKind
    first_candidate_index: int
    second_candidate_index: int
    first_query_id: str
    second_query_id: str | None
    first_text: str
    second_text: str
    key: QueryKey | None = None
    score: float | None = None


@dataclass(frozen=True, slots=True)
class BuyerQueryAssignment:
    """A deterministic initial ownership hint; workers may later steal work."""

    query_id: str
    worker_index: int
    assignment_order: int

    def to_payload(self, *, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "query_id": self.query_id,
            "worker_index": self.worker_index,
            "assignment_order": self.assignment_order,
        }


@dataclass(frozen=True, slots=True)
class BuyerQueryPlan:
    """Result of central query planning, ready for repository persistence."""

    request: BuyerQueryPlanRequest
    queries: tuple[BuyerQuery, ...]
    collisions: tuple[BuyerQueryCollision, ...]
    assignments: tuple[BuyerQueryAssignment, ...]

    def query_payloads(self) -> tuple[dict[str, Any], ...]:
        """Return JSON-serializable rows for the ``buyer_queries`` table."""

        return tuple(
            {
                "query_id": query.query_id,
                "run_id": query.run_id,
                "text": query.text,
                "normalized_text": query.normalized_text,
                "origin": query.origin.value,
                "parent_query_id": query.parent_query_id,
                "rationale": query.rationale,
                "priority": query.priority,
                "category_id": query.category_id,
                "category_path": list(query.category_path),
                "filters": dict(query.filters),
                "filter_json": query.filter_json,
                "filter_hash": query.filter_hash,
                "semantic_fingerprint": query.semantic_fingerprint,
                "predicted_total": query.predicted_total,
                "approved": query.approved,
                "enabled": query.enabled,
                "state": query.state.value,
                "stable_hash": query.stable_hash,
            }
            for query in self.queries
        )

    def assignment_payloads(self) -> tuple[dict[str, Any], ...]:
        """Return JSON-serializable initial assignment rows."""

        return tuple(assignment.to_payload(run_id=self.request.run_id) for assignment in self.assignments)

    def to_durable_payload(self) -> dict[str, Any]:
        """Return the complete plan snapshot for a durable event or artifact."""

        return {
            "run_id": self.request.run_id,
            "mode": self.request.mode.value,
            "minimum_query_count": self.request.minimum_query_count,
            "queries": list(self.query_payloads()),
            "assignments": list(self.assignment_payloads()),
            "collisions": [
                {
                    "kind": collision.kind.value,
                    "first_candidate_index": collision.first_candidate_index,
                    "second_candidate_index": collision.second_candidate_index,
                    "first_query_id": collision.first_query_id,
                    "second_query_id": collision.second_query_id,
                    "first_text": collision.first_text,
                    "second_text": collision.second_text,
                    "key": list(collision.key) if collision.key is not None else None,
                    "score": collision.score,
                }
                for collision in self.collisions
            ],
        }


class BuyerQueryPlanner:
    """Create deterministic, centrally deduplicated query plans."""

    def __init__(self, *, semantic_scorer: SemanticScorer | None = None, semantic_threshold: float = 0.9) -> None:
        if not 0.0 <= semantic_threshold <= 1.0:
            raise ValueError("semantic_threshold must be between 0 and 1")
        self._semantic_scorer = semantic_scorer
        self._semantic_threshold = semantic_threshold

    def plan(self, request: BuyerQueryPlanRequest) -> BuyerQueryPlan:
        """Build a complete plan without remote I/O or non-deterministic state."""

        if not isinstance(request, BuyerQueryPlanRequest):
            raise TypeError("request must be a BuyerQueryPlanRequest")

        queries: list[BuyerQuery] = []
        collisions: list[BuyerQueryCollision] = []
        seen: dict[QueryKey, tuple[int, BuyerQuery]] = {}
        for index, candidate in enumerate(request.candidates):
            query = self._build_query(request, candidate)
            existing = seen.get(query.stable_key)
            if existing is not None:
                first_index, first_query = existing
                collisions.append(
                    BuyerQueryCollision(
                        kind=BuyerQueryCollisionKind.EXACT,
                        first_candidate_index=first_index,
                        second_candidate_index=index,
                        first_query_id=first_query.query_id,
                        second_query_id=None,
                        first_text=first_query.text,
                        second_text=query.text,
                        key=query.stable_key,
                    )
                )
                continue
            seen[query.stable_key] = (index, query)
            queries.append(query)

        if len(queries) < request.minimum_query_count:
            raise InsufficientBuyerQueriesError(required=request.minimum_query_count, available=len(queries))

        collisions.extend(self._semantic_collisions(queries, seen))
        assignments = self._round_robin_assignments(queries, request.worker_count)
        return BuyerQueryPlan(
            request=request,
            queries=tuple(queries),
            collisions=tuple(collisions),
            assignments=assignments,
        )

    def _build_query(self, request: BuyerQueryPlanRequest, candidate: BuyerQueryCandidate) -> BuyerQuery:
        category_id = request.category_id if candidate.category_id is None else candidate.category_id
        category_path = request.category_path if candidate.category_path is None else candidate.category_path
        filters = request.filters if candidate.filters is None else candidate.filters
        origin = _candidate_origin(request.mode, candidate)
        rationale = candidate.rationale.strip() if isinstance(candidate.rationale, str) else ""
        if not rationale:
            rationale = f"{origin.value.replace('_', ' ')} candidate"
        key = stable_query_key(candidate.text, category_id=category_id, filters=filters)
        query_id = _query_id(request.run_id, key)
        approved = request.auto_approve
        return BuyerQuery(
            query_id=query_id,
            run_id=request.run_id,
            text=candidate.text,
            origin=origin,
            rationale=rationale,
            category_id=category_id,
            category_path=category_path,
            filters=filters,
            parent_query_id=candidate.parent_query_id,
            priority=candidate.priority,
            semantic_fingerprint=candidate.semantic_fingerprint,
            predicted_total=candidate.predicted_total,
            approved=approved,
            enabled=True,
            state=BuyerQueryState.APPROVED if approved else BuyerQueryState.GENERATED,
        )

    def _semantic_collisions(
        self,
        queries: Sequence[BuyerQuery],
        seen: Mapping[QueryKey, tuple[int, BuyerQuery]],
    ) -> tuple[BuyerQueryCollision, ...]:
        if self._semantic_scorer is None:
            return ()
        candidate_index_by_query_id = {query.query_id: index for index, query in seen.values()}
        collisions: list[BuyerQueryCollision] = []
        for first_index, first_query in enumerate(queries):
            for second_query in queries[first_index + 1 :]:
                score = _semantic_score(self._semantic_scorer(first_query, second_query))
                if score < self._semantic_threshold:
                    continue
                collisions.append(
                    BuyerQueryCollision(
                        kind=BuyerQueryCollisionKind.SEMANTIC,
                        first_candidate_index=candidate_index_by_query_id[first_query.query_id],
                        second_candidate_index=candidate_index_by_query_id[second_query.query_id],
                        first_query_id=first_query.query_id,
                        second_query_id=second_query.query_id,
                        first_text=first_query.text,
                        second_text=second_query.text,
                        score=score,
                    )
                )
        return tuple(collisions)

    @staticmethod
    def _round_robin_assignments(
        queries: Sequence[BuyerQuery], worker_count: int
    ) -> tuple[BuyerQueryAssignment, ...]:
        assignment_counts = [0] * worker_count
        assignments: list[BuyerQueryAssignment] = []
        for query_index, query in enumerate(queries):
            worker_index = (query_index % worker_count) + 1
            assignment_counts[worker_index - 1] += 1
            assignments.append(
                BuyerQueryAssignment(
                    query_id=query.query_id,
                    worker_index=worker_index,
                    assignment_order=assignment_counts[worker_index - 1],
                )
            )
        return tuple(assignments)


def plan_buyer_queries(
    request: BuyerQueryPlanRequest,
    *,
    semantic_scorer: SemanticScorer | None = None,
    semantic_threshold: float = 0.9,
) -> BuyerQueryPlan:
    """Convenience wrapper for a one-off deterministic planning pass."""

    return BuyerQueryPlanner(semantic_scorer=semantic_scorer, semantic_threshold=semantic_threshold).plan(request)


def _candidate_origin(mode: BuyerRunMode, candidate: BuyerQueryCandidate) -> BuyerQueryOrigin:
    if candidate.origin is not None:
        try:
            return BuyerQueryOrigin(candidate.origin)
        except (TypeError, ValueError) as exc:
            raise BuyerQueryPlanningError("candidate origin must be a BuyerQueryOrigin") from exc
    if mode is BuyerRunMode.MANUAL:
        return BuyerQueryOrigin.MANUAL
    if mode is BuyerRunMode.CATEGORY:
        return BuyerQueryOrigin.CATEGORY
    if mode is BuyerRunMode.BRIEF:
        return BuyerQueryOrigin.BRIEF
    return BuyerQueryOrigin.AI_EXPANSION if candidate.parent_query_id else BuyerQueryOrigin.HYBRID_SEED


def _query_id(run_id: str, key: QueryKey) -> str:
    category_id, normalized_text, normalized_filter_hash = key
    name = f"psr:kwork-buyer:{run_id}:{category_id}:{normalized_text}:{normalized_filter_hash}"
    return f"buyer-query-{uuid5(NAMESPACE_URL, name).hex}"


def _semantic_score(value: float) -> float:
    if isinstance(value, bool):
        raise BuyerQueryPlanningError("semantic scorer must return a number between 0 and 1")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise BuyerQueryPlanningError("semantic scorer must return a number between 0 and 1") from exc
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise BuyerQueryPlanningError("semantic scorer must return a number between 0 and 1")
    return score


__all__ = [
    "BuyerQueryAssignment",
    "BuyerQueryCandidate",
    "BuyerQueryCollision",
    "BuyerQueryCollisionKind",
    "BuyerQueryPlan",
    "BuyerQueryPlanRequest",
    "BuyerQueryPlanner",
    "BuyerQueryPlanningError",
    "InsufficientBuyerQueriesError",
    "SemanticScorer",
    "plan_buyer_queries",
]
