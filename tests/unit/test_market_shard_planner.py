from __future__ import annotations

import pytest

from src.platforms.kwork_supply.contracts import BatchResult, ContractState, ContractVerdict, ProtectionStatus
from src.platforms.kwork_supply.mapper import ScopeMapper, ScopePartition
from src.platforms.kwork_supply.models import (
    MarketJob,
    MarketJobCreate,
    MarketScope,
    NetworkPolicy,
    SourcePolicy,
)
from src.platforms.kwork_supply.planner import ShardPlanner, ShardPlanningError
from src.platforms.kwork_supply.repository import MarketJobRepository


@pytest.fixture
def scope() -> MarketScope:
    return MarketScope(category_id=38, canonical_alias="website-repair", filters={"price_to": 5000})


@pytest.fixture
def valid_scope_map(scope: MarketScope):
    batch = BatchResult(
        source="web_catalog",
        requested_cursor=None,
        reported_cursor=None,
        cards=({"id": 101}, {"id": 102}),
        source_total=1000,
        source_total_found=13000,
        protection_status=ProtectionStatus.OK,
        metadata={"shape_valid": True, "active_category_id": 38},
    )
    partitions = (
        ScopePartition(key="language", filters={"attribute[10]": "python"}, expected_count=400, priority=20),
        ScopePartition(key="root", filters={"price_to": 5000}, expected_count=13000, priority=10),
        ScopePartition(key="price", filters={"price_from": 1000, "price_to": 5000}, expected_count=600, priority=20),
    )
    return ScopeMapper().map_scope(
        scope,
        batch,
        ContractVerdict(state=ContractState.ACCEPTED),
        partitions=partitions,
    )


@pytest.fixture
def job(scope: MarketScope) -> MarketJob:
    return MarketJob(
        job_id="job_plan",
        scope=scope,
        profile="working",
        target_unique_cards=6,
        desired_workers=2,
        network_policy=NetworkPolicy.PREFER_VPNTE,
        source_policy=SourcePolicy.VALIDATED_ONLY,
        include_ai=False,
        request_budget=5,
    )


def test_planner_is_deterministic_and_allocates_bounded_budgets(job: MarketJob, valid_scope_map):
    planner = ShardPlanner()

    first = planner.plan_initial(job, valid_scope_map)
    reversed_map = valid_scope_map.with_partitions(tuple(reversed(valid_scope_map.partitions)))
    second = planner.plan_initial(job, reversed_map)

    assert [(item.shard.shard_id, item.request_budget, item.shard.filters) for item in first.shards] == [
        (item.shard.shard_id, item.request_budget, item.shard.filters) for item in second.shards
    ]
    assert first.request_budget == 5
    assert sum(item.request_budget for item in first.shards) == 5
    assert [item.shard.priority for item in first.shards] == [20, 20]
    assert [item.request_budget for item in first.shards] == [3, 2]
    assert first.aggregate_scope_total == 13000
    assert first.observed_first_batch_count == 2


def test_default_budget_uses_observed_batch_not_aggregate_total(scope: MarketScope, valid_scope_map):
    job = MarketJob(
        job_id="job_default_budget",
        scope=scope,
        profile="working",
        target_unique_cards=5,
        desired_workers=1,
        network_policy=NetworkPolicy.PREFER_VPNTE,
        source_policy=SourcePolicy.VALIDATED_ONLY,
        include_ai=False,
    )

    plan = ShardPlanner().plan_initial(job, valid_scope_map.with_partitions((valid_scope_map.partitions[1],)))

    assert plan.estimated_requests_for_target == 3
    assert plan.request_budget == 3
    assert plan.observed_first_batch_count == 2
    assert plan.aggregate_scope_total == 13000


def test_default_parallel_budget_keeps_full_root_fallback(scope: MarketScope, valid_scope_map):
    job = MarketJob(
        job_id="job_parallel_fallback",
        scope=scope,
        profile="working",
        target_unique_cards=5,
        desired_workers=3,
        network_policy=NetworkPolicy.PREFER_VPNTE,
        source_policy=SourcePolicy.VALIDATED_ONLY,
        include_ai=False,
    )

    plan = ShardPlanner().plan_initial(job, valid_scope_map)

    assert len(plan.shards) == 3
    assert plan.shards[0].shard.filters == scope.filters
    assert [item.request_budget for item in plan.shards] == [3, 1, 1]
    assert plan.request_budget == 5


def test_planner_keeps_one_initial_batch_for_each_of_100_validated_partitions(scope: MarketScope, valid_scope_map):
    partitions = tuple(
        ScopePartition(
            key=f"attribute:10:{index}",
            filters={"attribute[10]": str(index)},
            expected_count=24,
            priority=100 - index,
            source="web_catalog",
            alias="website-repair",
        )
        for index in range(1, 101)
    )
    job = MarketJob(
        job_id="job_one_hundred_workers",
        scope=scope,
        profile="working",
        target_unique_cards=1,
        desired_workers=100,
        network_policy=NetworkPolicy.VPNTE_ONLY,
        source_policy=SourcePolicy.VALIDATED_ONLY,
        include_ai=False,
    )

    plan = ShardPlanner().plan_initial(job, valid_scope_map.with_partitions(partitions))

    assert len(plan.shards) == 100
    assert plan.request_budget == 100
    assert all(item.request_budget == 1 for item in plan.shards)


def test_planner_rejects_a_scope_map_without_validated_alias(scope: MarketScope):
    rejected_map = ScopeMapper().map_scope(
        scope,
        BatchResult(
            source="mobile_kworks",
            requested_cursor=None,
            reported_cursor=None,
            cards=({"id": 101},),
            metadata={"shape_valid": True, "active_category_id": 38},
        ),
        ContractVerdict(state=ContractState.ACCEPTED),
    )
    job = MarketJob(
        job_id="job_rejected",
        scope=scope,
        profile="working",
        target_unique_cards=5,
        desired_workers=1,
        network_policy=NetworkPolicy.PREFER_VPNTE,
        source_policy=SourcePolicy.VALIDATED_ONLY,
        include_ai=False,
    )

    with pytest.raises(ShardPlanningError):
        ShardPlanner().plan_initial(job, rejected_map)


@pytest.mark.asyncio
async def test_planner_persists_deterministic_initial_shards(tmp_path, scope: MarketScope, job: MarketJob, valid_scope_map):
    repository = MarketJobRepository(tmp_path / "planner.sqlite3")
    await repository.create_job(MarketJobCreate(scope=scope), job_id=job.job_id)
    plan = ShardPlanner().plan_initial(job, valid_scope_map)

    created = await ShardPlanner().persist_initial_shards(repository, plan)

    assert [row["shard_id"] for row in created] == [item.shard.shard_id for item in plan.shards]
    assert [row["expected_count"] for row in created] == [item.shard.expected_count for item in plan.shards]
