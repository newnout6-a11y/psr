from __future__ import annotations

import pytest

from src.platforms.kwork_supply.contracts import BatchResult, ContractState, ContractVerdict, ProtectionStatus
from src.platforms.kwork_supply.mapper import AliasValidationStatus, ScopeMapper
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope
from src.platforms.kwork_supply.repository import MarketJobRepository


@pytest.fixture
def scope() -> MarketScope:
    return MarketScope(
        category_id=38,
        category_name="Website work",
        canonical_alias="website-repair",
        filters={"price_to": 5000},
    )


@pytest.fixture
def accepted_web_batch() -> BatchResult:
    return BatchResult(
        source="web_catalog",
        requested_cursor=None,
        reported_cursor=None,
        cards=({"id": 101}, {"id": 102}),
        actual_item_count=2,
        source_total=1000,
        source_total_found=13000,
        protection_status=ProtectionStatus.OK,
        metadata={"shape_valid": True, "active_category_id": 38},
    )


@pytest.fixture
def accepted_verdict() -> ContractVerdict:
    return ContractVerdict(state=ContractState.ACCEPTED)


def test_scope_mapper_keeps_aggregate_and_observed_counts_separate(
    scope: MarketScope,
    accepted_web_batch: BatchResult,
    accepted_verdict: ContractVerdict,
):
    mapped = ScopeMapper().map_scope(
        scope,
        accepted_web_batch,
        accepted_verdict,
        source_url="https://kwork.ru/catalog_kworks_filters/website-repair",
    )

    assert mapped.alias_validation.status is AliasValidationStatus.ACCEPTED
    assert mapped.ready_for_planning is True
    assert mapped.aggregate_scope_total == 13000
    assert mapped.reported_stream_total == 1000
    assert mapped.observed_first_batch_count == 2
    assert mapped.aggregate_scope_total != mapped.observed_first_batch_count
    assert mapped.partitions[0].filters == {"price_to": 5000}


def test_scope_mapper_rejects_unvalidated_alias_or_non_web_source(
    scope: MarketScope,
    accepted_web_batch: BatchResult,
    accepted_verdict: ContractVerdict,
):
    wrong_category = BatchResult(
        source="web_catalog",
        requested_cursor=None,
        reported_cursor=None,
        cards=({"id": 101},),
        protection_status=ProtectionStatus.OK,
        metadata={"shape_valid": True, "active_category_id": 39},
    )
    mobile_batch = BatchResult(
        source="mobile_kworks",
        requested_cursor=None,
        reported_cursor=None,
        cards=({"id": 101},),
        protection_status=ProtectionStatus.OK,
        metadata={"shape_valid": True, "active_category_id": 38},
    )
    mapper = ScopeMapper()

    wrong_category_map = mapper.map_scope(scope, wrong_category, accepted_verdict)
    mobile_map = mapper.map_scope(scope, mobile_batch, accepted_verdict)

    assert wrong_category_map.alias_validation.status is AliasValidationStatus.REJECTED
    assert "active_category_mismatch" in wrong_category_map.alias_validation.reason_codes
    assert wrong_category_map.ready_for_planning is False
    assert mobile_map.alias_validation.status is AliasValidationStatus.REJECTED
    assert "primary_source_not_web_catalog" in mobile_map.alias_validation.reason_codes


def test_scope_mapper_builds_only_classification_filter_candidates(scope: MarketScope):
    attributes = {
        "flat": [
            {
                "id": 208,
                "path_ids": [208],
                "raw": {"id": 208, "is_classification": True},
            },
            {
                "id": 3587,
                "path_ids": [208, 3587],
                "kworks_count": 18261,
                "raw": {"id": 3587},
            },
            {
                "id": 999,
                "path_ids": [999],
                "raw": {"id": 999, "is_classification": False},
            },
            {
                "id": 1000,
                "path_ids": [999, 1000],
                "kworks_count": 500,
                "raw": {"id": 1000},
            },
        ]
    }

    candidates = ScopeMapper.classification_partition_candidates(scope, attributes)

    assert len(candidates) == 1
    assert candidates[0].key == "attribute:208:3587"
    assert candidates[0].filters == {"price_to": 5000, "attribute[208]": "3587"}
    assert candidates[0].expected_count == 18261


def test_scope_mapper_builds_disjoint_price_lanes_for_parallel_workers(scope: MarketScope):
    candidates = ScopeMapper.parallel_partition_candidates(
        scope,
        attributes=None,
        catalog_filters={"filters": {"priceLimits": {"min": 500, "max": 50_000}}},
        max_candidates=4,
    )

    assert len(candidates) == 4
    assert candidates[0].filters == {"price_to": 1625, "price_from": 500}
    assert candidates[-1].filters == {"price_to": 5000, "price_from": 3878}
    assert all(
        int(left.filters["price_to"]) + 1 == int(right.filters["price_from"])
        for left, right in zip(candidates, candidates[1:], strict=False)
    )


def test_scope_mapper_combines_independent_classifications_to_fill_worker_front(scope: MarketScope):
    flat = []
    for parent_id in (100, 200, 300):
        flat.append({"id": parent_id, "path_ids": [parent_id], "raw": {"is_classification": True}})
        for offset in range(5):
            value_id = parent_id + offset + 1
            flat.append(
                {
                    "id": value_id,
                    "path_ids": [parent_id, value_id],
                    "kworks_count": 100 - offset,
                    "raw": {},
                }
            )

    candidates = ScopeMapper.parallel_partition_candidates(
        scope,
        attributes={"flat": flat},
        catalog_filters=None,
        max_candidates=50,
    )

    assert len(candidates) == 50
    assert sum(candidate.key.startswith("combo:") for candidate in candidates) == 35
    assert all(
        len([key for key in candidate.filters if str(key).startswith("attribute[")]) == 2
        for candidate in candidates[15:]
    )


@pytest.mark.asyncio
async def test_scope_mapper_persists_the_alias_validation(
    tmp_path,
    scope: MarketScope,
    accepted_web_batch: BatchResult,
    accepted_verdict: ContractVerdict,
):
    repository = MarketJobRepository(tmp_path / "mapper.sqlite3")
    await repository.create_job(MarketJobCreate(scope=scope), job_id="job_mapper")
    mapped = ScopeMapper().map_scope(scope, accepted_web_batch, accepted_verdict)

    record = await ScopeMapper().persist_alias_validation(repository, mapped)

    assert record["category_id"] == 38
    assert record["canonical_alias"] == "website-repair"
    assert record["validation_status"] == AliasValidationStatus.ACCEPTED.value
    assert record["active_category_id"] == 38
