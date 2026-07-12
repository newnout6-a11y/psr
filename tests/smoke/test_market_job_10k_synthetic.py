"""Network-free scale smoke coverage for the durable 10,000-card job path."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import (
    MarketJobCreate,
    MarketScope,
    Operation,
    OperationKind,
    OperationState,
    ShardSpec,
)
from src.platforms.kwork_supply.rate_control import RateControlPolicy, TokenBucketPolicy
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.supervisor import MarketWorkerSupervisor


TARGET_UNIQUE_CARDS = 10_000
BATCH_SIZE = 500
BATCH_COUNT = TARGET_UNIQUE_CARDS // BATCH_SIZE


class PartitionedCatalogClient:
    """Synthetic catalog with one capped root stream and nine classifications."""

    _CARDS_PER_PARTITION = 1_000
    _BATCH_SIZE = 100

    async def get_web_catalog_filters(self, alias: str, **kwargs: Any) -> Mapping[str, Any]:
        filters = dict(kwargs.get("filters") or {})
        partition = next(
            (
                int(value)
                for key, value in filters.items()
                if str(key).startswith("attribute[") and str(value).isdigit()
            ),
            0,
        )
        partition_offset = partition * self._CARDS_PER_PARTITION
        excluded = {
            int(value)
            for value in str(filters.get("excludeIds") or "").split(",")
            if value.strip().isdigit()
        }
        cards = [
            {"id": partition_offset + identifier, "title": f"Synthetic {partition_offset + identifier}"}
            for identifier in range(1, self._CARDS_PER_PARTITION + 1)
            if partition_offset + identifier not in excluded
        ][: self._BATCH_SIZE]
        return {
            "endpoint": f"/catalog_kworks_filters/{alias}",
            "url": f"https://kwork.test/catalog_kworks_filters/{alias}",
            "status_code": 200,
            "content_type": "application/json",
            "bytes": 1,
            "request_params": {"page": 1, "pageSize": self._BATCH_SIZE, **filters},
            "protection_status": "ok",
            "raw": {
                "success": True,
                "data": {
                    "stateData": {
                        "viewData": {
                            "filters": {"activeCategoryId": 38, "kworksCount": TARGET_UNIQUE_CARDS},
                            "kworks": {
                                "total": self._CARDS_PER_PARTITION,
                                "total_found": TARGET_UNIQUE_CARDS,
                                "posts": {"data": cards},
                            },
                        }
                    }
                },
            },
        }

    async def get_catalog_filters(self, category_id: int) -> Mapping[str, Any]:
        return {
            "category_id": category_id,
            "filters": {"kworks_count": TARGET_UNIQUE_CARDS},
            "raw": {"success": True, "response": {"kworks_count": TARGET_UNIQUE_CARDS}},
        }

    async def get_category_attributes(self, category_id: int) -> Mapping[str, Any]:
        return {
            "category_id": category_id,
            "flat": [
                {"id": 208, "path_ids": [208], "raw": {"id": 208, "is_classification": True}},
                *[
                    {
                        "id": partition,
                        "path_ids": [208, partition],
                        "kworks_count": self._CARDS_PER_PARTITION,
                        "raw": {"id": partition},
                    }
                    for partition in range(1, 10)
                ],
            ],
            "raw": {"success": True, "response": [{"id": 208}]},
        }

    async def close(self) -> None:
        return None


async def _wait_for_collection_target(
    repository: MarketJobRepository,
    job_id: str,
    *,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        job = await repository.get_job(job_id)
        if job is not None:
            counters = job.get("counters") or {}
            if int(counters.get("unique_cards", 0) or 0) >= TARGET_UNIQUE_CARDS:
                return job
        await asyncio.sleep(0.01)
    raise AssertionError("synthetic 10k actor collection did not reach target")


@pytest.mark.asyncio
async def test_10k_synthetic_batches_stop_at_target_without_extra_continuation(tmp_path):
    """Commit paged cards through the repository without using a network source."""

    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    job_id = "job_synthetic_10k"
    shard_id = "shard_synthetic_10k"
    worker_id = "worker_synthetic_10k"

    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=TARGET_UNIQUE_CARDS,
            desired_workers=1,
        ),
        job_id=job_id,
    )
    await repository.create_shard(
        ShardSpec(
            shard_id=shard_id,
            job_id=job_id,
            source="web_catalog",
            alias="website-repair",
        )
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op-page-0",
            job_id=job_id,
            shard_id=shard_id,
            kind=OperationKind.FETCH_BATCH,
            idempotency_key="synthetic-page:0",
        )
    )

    for page_index in range(BATCH_COUNT):
        operation_id = f"op-page-{page_index}"
        leased = await repository.lease_operation(worker_id, job_id=job_id)
        assert leased is not None
        assert leased["operation_id"] == operation_id

        first_id = page_index * BATCH_SIZE + 1
        last_id = first_id + BATCH_SIZE - 1
        listings = [{"id": listing_id, "title": f"Synthetic {listing_id}"} for listing_id in range(first_id, last_id + 1)]
        if page_index:
            # A card from the preceding page verifies cross-page deduplication.
            listings.append({"id": first_id - 1, "title": f"Synthetic {first_id - 1}"})

        requested_cursor = {"page": page_index + 1, "previous_last_id": first_id - 1 if page_index else None}
        next_cursor = {"page": page_index + 2, "last_id": last_id}
        committed = await repository.commit_accepted_batch(
            job_id=job_id,
            shard_id=shard_id,
            operation_id=operation_id,
            attempt_id=leased["attempt_id"],
            idempotency_key=f"synthetic-page:{page_index}",
            source="web_catalog",
            listings=listings,
            requested_cursor=requested_cursor,
            reported_cursor={"page": page_index + 1, "count": len(listings)},
            next_cursor=next_cursor,
            raw_response_ref=f"synthetic://page/{page_index + 1}",
            fingerprint=f"synthetic-page-{page_index}",
            next_operation={
                "operation_id": f"op-page-{page_index + 1}",
                "kind": OperationKind.FETCH_BATCH,
                "idempotency_key": f"synthetic-page:{page_index + 1}",
                "payload": {"requested_cursor": next_cursor},
            },
        )

        if page_index < BATCH_COUNT - 1:
            assert committed["target_reached"] is False
            assert committed["next_operation_id"] == f"op-page-{page_index + 1}"
        else:
            assert committed["target_reached"] is True
            assert committed["next_operation_id"] is None
            assert committed["new_listings"] == BATCH_SIZE
            assert committed["observations"] == BATCH_SIZE + 1

    job = await repository.get_job(job_id)
    shard = await repository.get_shard(shard_id)
    succeeded_operations = await repository.list_operations(
        job_id,
        state=OperationState.SUCCEEDED,
        limit=BATCH_COUNT + 1,
    )
    queued_operations = await repository.list_operations(job_id, state=OperationState.QUEUED)
    events = await repository.replay_events(job_id, limit=BATCH_COUNT + 1)

    assert job is not None
    assert shard is not None
    assert job["counters"]["unique_cards"] == TARGET_UNIQUE_CARDS
    assert job["counters"]["unique_listings"] == TARGET_UNIQUE_CARDS
    assert job["counters"]["listing_observations"] == TARGET_UNIQUE_CARDS + BATCH_COUNT - 1
    assert job["counters"]["accepted_batches"] == BATCH_COUNT
    assert shard["cursor"] == {"page": BATCH_COUNT + 1, "last_id": TARGET_UNIQUE_CARDS}
    assert len(succeeded_operations) == BATCH_COUNT
    assert queued_operations == []
    assert await repository.get_operation(f"op-page-{BATCH_COUNT}") is None
    assert len(events) == BATCH_COUNT
    assert events[-1]["payload"]["next_operation_id"] is None


@pytest.mark.asyncio
async def test_10k_actor_path_expands_partitions_across_mapping_waves(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    permissive_rate_policy = RateControlPolicy(
        global_policy=TokenBucketPolicy(capacity=10_000, refill_per_second=10_000),
        default_source_policy=TokenBucketPolicy(capacity=10_000, refill_per_second=10_000),
        source_policies={"web_catalog": TokenBucketPolicy(capacity=10_000, refill_per_second=10_000)},
        fallback_retry_seconds=1,
        max_retry_after_seconds=10,
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=lambda _proxy_url: PartitionedCatalogClient(),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        rate_control_policy=permissive_rate_policy,
    )
    supervisor = MarketWorkerSupervisor(
        coordinator,
        handlers=executor.handlers,
        reconcile_interval_seconds=0.005,
        worker_poll_interval_seconds=0.001,
    )
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=TARGET_UNIQUE_CARDS,
            desired_workers=1,
        ),
        job_id="job_actor_10k",
    )
    await supervisor.start()
    try:
        collected = await _wait_for_collection_target(repository, "job_actor_10k")
    finally:
        await supervisor.close()

    shards = await repository.list_shards("job_actor_10k", limit=100)
    operations = await repository.list_operations("job_actor_10k", limit=1_000)
    assert collected["counters"]["unique_cards"] >= TARGET_UNIQUE_CARDS
    assert collected["phase"] in {"collect", "enrich"}
    assert len(shards) == 10
    assert len([operation for operation in operations if operation["payload"].get("partition_mapping") is True]) >= 1
    collection_operations = [
        operation
        for operation in operations
        if operation["kind"] in {
            OperationKind.MAP_SCOPE.value,
            OperationKind.RESOLVE_ALIAS.value,
            OperationKind.FETCH_BATCH.value,
        }
    ]
    assert collection_operations
    assert any(operation["state"] == OperationState.SUCCEEDED.value for operation in collection_operations)
    if collected["phase"] == "enrich":
        assert await repository.find_active_operation(
            "job_actor_10k",
            kinds=(OperationKind.ENRICH_LISTING,),
        ) is not None
