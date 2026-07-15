"""Deterministic initial shard planning for validated market scopes."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Iterable

from .mapper import ScopeMap, ScopePartition
from .models import MarketJob, ShardSpec
from .repository import MarketJobRepository


class ShardPlanningError(ValueError):
    """Raised when a scope has not passed the required planning gates."""


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ShardPlanningError("partition filters must be JSON serializable") from exc


@dataclass(frozen=True, slots=True)
class PlannedShard:
    """One initial durable shard plus its bounded collection budget."""

    shard: ShardSpec
    request_budget: int


@dataclass(frozen=True, slots=True)
class InitialShardPlan:
    """Deterministic collection plan, without any coverage claim."""

    job_id: str
    shards: tuple[PlannedShard, ...]
    request_budget: int
    estimated_requests_for_target: int | None
    aggregate_scope_total: int | None
    observed_first_batch_count: int


class ShardPlanner:
    """Plan only validated web-catalog shards with explicit finite budgets."""

    def plan_initial(self, job: MarketJob, scope_map: ScopeMap) -> InitialShardPlan:
        if not isinstance(job, MarketJob):
            raise TypeError("job must be a MarketJob")
        if not isinstance(scope_map, ScopeMap):
            raise TypeError("scope_map must be a ScopeMap")
        if not scope_map.ready_for_planning:
            raise ShardPlanningError("scope map requires a validated web alias and source")
        if job.scope.category_id != scope_map.scope.category_id:
            raise ShardPlanningError("job and scope map category IDs differ")
        if job.scope.canonical_alias and job.scope.canonical_alias != scope_map.alias_validation.canonical_alias:
            raise ShardPlanningError("job and scope map canonical aliases differ")

        partitions = self._ordered_unique_partitions(scope_map.partitions)
        if not partitions:
            raise ShardPlanningError("scope map has no candidate partitions")
        observed = scope_map.observed_first_batch_count
        estimated = math.ceil(job.target_unique_cards / observed) if observed > 0 else None
        if job.request_budget is not None:
            request_budget = job.request_budget
            if request_budget <= 0:
                raise ShardPlanningError("request budget must be positive")
            initial_width = min(len(partitions), request_budget, max(job.desired_workers, 1))
            selected = partitions[:initial_width]
            budgets = self._allocate_budgets(request_budget, len(selected))
        else:
            # Keep the unfiltered root stream alive long enough to reach the
            # target even when parallel candidate filters are empty or ignored.
            root_signature = _stable_json(scope_map.scope.filters)
            root = next((item for item in partitions if _stable_json(item.filters) == root_signature), None)
            if root is not None:
                partitions = (root, *(item for item in partitions if item is not root))
            initial_width = min(len(partitions), max(job.desired_workers, 1))
            selected = partitions[:initial_width]
            primary_budget = max(estimated or 1, 1)
            budgets = (primary_budget, *(1 for _item in selected[1:]))
            request_budget = sum(budgets)

        planned_shards: list[PlannedShard] = []
        for partition, shard_budget in zip(selected, budgets, strict=True):
            if partition.source != scope_map.source or partition.alias != scope_map.alias_validation.canonical_alias:
                raise ShardPlanningError("partition is not bound to the validated alias/source")
            shard = ShardSpec(
                shard_id=self._shard_id(job.job_id, partition),
                job_id=job.job_id,
                source=partition.source,
                alias=partition.alias,
                filters=partition.filters,
                expected_count=partition.expected_count,
                priority=partition.priority,
            )
            planned_shards.append(PlannedShard(shard=shard, request_budget=shard_budget))
        return InitialShardPlan(
            job_id=job.job_id,
            shards=tuple(planned_shards),
            request_budget=request_budget,
            estimated_requests_for_target=estimated,
            aggregate_scope_total=scope_map.aggregate_scope_total,
            observed_first_batch_count=observed,
        )

    async def persist_initial_shards(
        self,
        repository: MarketJobRepository,
        plan: InitialShardPlan,
    ) -> list[dict[str, Any]]:
        """Persist the already deterministic shard specs in plan order."""

        records: list[dict[str, Any]] = []
        for planned in plan.shards:
            records.append(await repository.create_shard(planned.shard))
        return records

    @staticmethod
    def _ordered_unique_partitions(partitions: Iterable[ScopePartition]) -> tuple[ScopePartition, ...]:
        ordered = sorted(
            partitions,
            key=lambda item: (-item.priority, item.source or "", item.alias or "", _stable_json(item.filters), item.key),
        )
        result: list[ScopePartition] = []
        seen: set[tuple[str | None, str | None, str]] = set()
        for partition in ordered:
            identity = (partition.source, partition.alias, _stable_json(partition.filters))
            if identity in seen:
                continue
            seen.add(identity)
            result.append(partition)
        return tuple(result)

    @staticmethod
    def _allocate_budgets(request_budget: int, shard_count: int) -> tuple[int, ...]:
        if shard_count <= 0:
            raise ShardPlanningError("shard_count must be positive")
        base, remainder = divmod(request_budget, shard_count)
        return tuple(base + (1 if index < remainder else 0) for index in range(shard_count))

    @staticmethod
    def _shard_id(job_id: str, partition: ScopePartition) -> str:
        payload = "|".join(
            (
                job_id,
                partition.source or "",
                partition.alias or "",
                partition.key,
                _stable_json(partition.filters),
            )
        )
        return f"shard_{sha256(payload.encode('utf-8')).hexdigest()[:24]}"
