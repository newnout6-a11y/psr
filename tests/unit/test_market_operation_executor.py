from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from src.platforms.kwork_supply.artifacts import LocalArtifactStore
from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, OperationKind, OperationState, SourcePolicy
from src.platforms.kwork_supply.rate_control import RateControlPolicy, TokenBucketPolicy
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.worker import RetryableOperationError


def raw_catalog(cards: list[Mapping[str, object]], *, category_id: int = 38) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "stateData": {
                "viewData": {
                    "filters": {"activeCategoryId": category_id, "kworksCount": 100},
                    "kworks": {"total": 20, "total_found": 100, "posts": {"data": cards}},
                }
            }
        },
    }


class FakeWebClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.proxy_url: str | None = None
        self.closed = False
        self.web_calls: list[dict[str, Any]] = []

    async def get_web_catalog_filters(self, alias: str, **kwargs: Any) -> Mapping[str, Any]:
        self.web_calls.append({"alias": alias, "cookies": dict(kwargs.get("cookies") or {})})
        if "status_code" in self.response and "raw" in self.response:
            return self.response
        return {
            "endpoint": f"/catalog_kworks_filters/{alias}",
            "url": f"https://kwork.ru/catalog_kworks_filters/{alias}",
            "status_code": 200,
            "content_type": "application/json",
            "bytes": 123,
            "request_params": {"page": 1, "pageSize": 10, **dict(kwargs.get("filters") or {})},
            "protection_status": "ok",
            "raw": self.response,
        }

    async def close(self) -> None:
        self.closed = True


class ClientFactory:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.created: list[FakeWebClient] = []

    def __call__(self, proxy_url: str | None) -> FakeWebClient:
        client = FakeWebClient(self.responses.pop(0))
        client.proxy_url = proxy_url
        self.created.append(client)
        return client


class DiscoveryWebClient(FakeWebClient):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        super().__init__({})
        self.responses = responses

    async def get_web_catalog_filters(self, alias: str, **kwargs: Any) -> Mapping[str, Any]:
        if not self.responses:
            raise AssertionError("missing discovery web response")
        self.response = self.responses.pop(0)
        return await super().get_web_catalog_filters(alias, **kwargs)

    async def get_catalog_filters(self, category_id: int) -> Mapping[str, Any]:
        return {
            "filters": {"kworks_count": 100, "groups": [{"id": 1}]},
            "raw": {"success": True, "response": {"kworks_count": 100}},
            "category_id": category_id,
        }

    async def get_category_attributes(self, category_id: int) -> Mapping[str, Any]:
        return {
            "category_id": category_id,
            "flat": [
                {
                    "id": 208,
                    "path_ids": [208],
                    "raw": {"id": 208, "is_classification": True},
                },
                {
                    "id": 3587,
                    "path_ids": [208, 3587],
                    "kworks_count": 30,
                    "raw": {"id": 3587},
                },
            ],
            "raw": {"success": True, "response": [{"id": 208}]},
        }


class DiscoveryClientFactory:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.created: list[DiscoveryWebClient] = []

    def __call__(self, proxy_url: str | None) -> DiscoveryWebClient:
        client = DiscoveryWebClient(self.responses)
        client.proxy_url = proxy_url
        self.created.append(client)
        return client


class PriceDiscoveryWebClient(DiscoveryWebClient):
    async def get_catalog_filters(self, category_id: int) -> Mapping[str, Any]:
        return {
            "category_id": category_id,
            "filters": {"kworksCount": 5_000, "priceLimits": {"min": 500, "max": 50_000}},
            "raw": {"success": True},
        }

    async def get_category_attributes(self, category_id: int) -> Mapping[str, Any]:
        return {"category_id": category_id, "flat": [], "raw": {"success": True}}


class PriceDiscoveryClientFactory(DiscoveryClientFactory):
    def __call__(self, proxy_url: str | None) -> PriceDiscoveryWebClient:
        client = PriceDiscoveryWebClient(self.responses)
        client.proxy_url = proxy_url
        self.created.append(client)
        return client


class FakeMobileOnlyClient:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.mobile_calls: list[dict[str, int | None]] = []
        self.web_calls = 0
        self.closed = False

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
    ) -> Mapping[str, Any]:
        self.mobile_calls.append(
            {"category_id": category_id, "classifier_id": classifier_id, "page": page}
        )
        return self.response

    async def get_web_catalog_filters(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        self.web_calls += 1
        raise AssertionError("mobile_first_page_only must not call the web catalog client")

    async def close(self) -> None:
        self.closed = True


class MobileOnlyClientFactory:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.created: list[FakeMobileOnlyClient] = []

    def __call__(self, _proxy_url: str | None) -> FakeMobileOnlyClient:
        client = FakeMobileOnlyClient(self.response)
        self.created.append(client)
        return client


class FakeWorker:
    job_id = "job_executor"
    worker_id = "worker_executor"
    transport_proxy_url = "http://127.0.0.1:17990"

    def __init__(self) -> None:
        self.quarantine_calls: list[dict[str, str | None]] = []

    async def quarantine_current_transport(self, *, reason: str, until: str | None = None) -> bool:
        self.quarantine_calls.append({"reason": reason, "until": until})
        return True


@pytest.mark.asyncio
async def test_executor_waits_locally_and_isolates_rate_limits_by_transport(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    policy = RateControlPolicy(
        global_policy=TokenBucketPolicy(capacity=1, refill_per_second=1),
        default_source_policy=TokenBucketPolicy(capacity=1, refill_per_second=1),
    )
    executor = MarketOperationExecutor(
        coordinator,
        rate_control_policy=policy,
        rate_clock=clock,
        rate_sleeper=sleeper,
    )
    first_route = FakeWorker()
    first_route.transport_id = "vpnte-slot-1"
    second_route = FakeWorker()
    second_route.transport_id = "vpnte-slot-2"

    await executor._acquire_source_permit(first_route, "mobile_kworks")
    await executor._acquire_source_permit(first_route, "mobile_kworks")
    assert sleeps == [pytest.approx(1.0)]

    await executor._acquire_source_permit(second_route, "mobile_kworks")
    assert sleeps == [pytest.approx(1.0)]
    assert set(executor._rate_control_states) == {"vpnte-slot-1", "vpnte-slot-2"}


@pytest.mark.asyncio
async def test_executor_maps_validated_scope_then_commits_web_batch_with_raw_artifact(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = ClientFactory(
        [
            raw_catalog([{"id": 1, "title": "Mapping evidence"}, {"id": 2, "title": "Second"}]),
            raw_catalog([{"id": 1, "title": "First fetch"}, {"id": 2, "title": "Second fetch"}]),
        ]
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=4,
            desired_workers=1,
            request_budget=2,
        ),
        job_id=worker.job_id,
    )

    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None
    await executor.handle_map_scope(worker, mapping)
    await repository.complete_operation(mapping["operation_id"], worker.worker_id)

    aliases = await repository.get_category_alias(38, "website-repair")
    shards = await repository.list_shards(worker.job_id)
    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    assert aliases is not None and aliases["validation_status"] == "accepted"
    assert len(shards) == 1
    assert len(queued) == 1

    fetch = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert fetch is not None
    await executor.handle_fetch_batch(worker, fetch)

    listings = await repository.list_listings(worker.job_id)
    accepted = await repository.get_operation(fetch["operation_id"])
    job = await repository.get_job(worker.job_id)
    next_operations = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    raw_files = list((tmp_path / "artifacts" / worker.job_id / "raw").glob("*.json.gz"))
    assert [listing["listing_key"] for listing in listings] == ["1", "2"]
    assert accepted is not None and accepted["state"] == OperationState.SUCCEEDED.value
    assert job is not None and job["phase"] == "collect"
    assert [operation["kind"] for operation in next_operations].count(OperationKind.FETCH_BATCH.value) == 1
    assert [operation["kind"] for operation in next_operations].count(OperationKind.ENRICH_LISTING.value) == 0
    assert raw_files
    assert all(client.closed for client in factory.created)


@pytest.mark.asyncio
async def test_executor_passes_injected_session_cookies_to_the_web_catalog_adapter(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = ClientFactory([raw_catalog([{"id": 1, "title": "Mapping evidence"}])])

    async def fresh_cookies() -> Mapping[str, str]:
        return {"session": "fresh-cookie"}

    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        web_cookie_provider=fresh_cookies,
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=2,
            desired_workers=1,
            request_budget=1,
        ),
        job_id=worker.job_id,
    )
    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None

    await executor.handle_map_scope(worker, mapping)

    assert factory.created[0].web_calls == [{"alias": "website-repair", "cookies": {"session": "fresh-cookie"}}]


@pytest.mark.asyncio
async def test_export_enqueue_uses_a_new_operation_for_a_new_checkpoint_after_resume(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=ClientFactory([]),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=2,
            desired_workers=1,
        ),
        job_id="job_export_resume",
    )
    mapping = await repository.lease_operation("worker_export", job_id="job_export_resume")
    assert mapping is not None
    await repository.complete_operation(mapping["operation_id"], "worker_export")

    first = await executor._enqueue_export_operation(
        "job_export_resume",
        checkpoint_id="checkpoint_before_resume",
        revision=1,
    )
    leased = await repository.lease_operation("worker_export", job_id="job_export_resume")
    assert leased is not None and leased["operation_id"] == first["operation_id"]
    await repository.complete_operation(first["operation_id"], "worker_export")

    second = await executor._enqueue_export_operation(
        "job_export_resume",
        checkpoint_id="checkpoint_after_resume",
        revision=1,
    )

    assert second["operation_id"] != first["operation_id"]
    assert second["state"] == OperationState.QUEUED.value
    assert second["idempotency_key"] == "export:job_export_resume:checkpoint_after_resume"


@pytest.mark.asyncio
async def test_executor_schedules_classification_partition_for_a_second_worker(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = DiscoveryClientFactory(
        [
            raw_catalog([{"id": 1}, {"id": 2}]),
            raw_catalog([{"id": 3}, {"id": 4}]),
        ]
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=40,
            desired_workers=2,
            request_budget=2,
        ),
        job_id=worker.job_id,
    )
    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None

    await executor.handle_map_scope(worker, mapping)

    shards = await repository.list_shards(worker.job_id)
    checkpoints = await repository.list_checkpoints(worker.job_id, limit=5)
    classification_shards = [shard for shard in shards if shard["filters"].get("attribute[208]") == "3587"]
    assert len(shards) == 2
    assert len(classification_shards) == 1
    assert checkpoints[0]["metrics"]["mapping"]["category_attribute_count"] == 2
    assert checkpoints[0]["metrics"]["mapping"]["partition_probes"][0]["accepted"] is None
    assert checkpoints[0]["metrics"]["mapping"]["partition_probes"][0]["validation"] == "first worker fetch"
    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    assert len(queued) == 2
    assert sum(operation["payload"].get("mapping_candidate") is True for operation in queued) == 1


@pytest.mark.asyncio
async def test_executor_queues_one_initial_fetch_per_configured_worker(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = PriceDiscoveryClientFactory([raw_catalog([{"id": 1}, {"id": 2}])])
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=96,
            desired_workers=4,
            request_budget=4,
        ),
        job_id=worker.job_id,
    )
    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None

    await executor.handle_map_scope(worker, mapping)

    shards = await repository.list_shards(worker.job_id)
    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    fetches = [operation for operation in queued if operation["kind"] == OperationKind.FETCH_BATCH.value]
    assert len(shards) == 4
    assert len(fetches) == 4
    assert sum(operation["payload"].get("mapping_candidate") is True for operation in fetches) == 3
    assert {operation["payload"].get("parallel_wave_size") for operation in fetches} == {4}
    assert len({operation["payload"].get("parallel_wave_id") for operation in fetches}) == 1


@pytest.mark.asyncio
async def test_request_events_record_the_real_parallel_peak(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    executor = MarketOperationExecutor(coordinator, client_factory=ClientFactory([]))
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            desired_workers=4,
        ),
        job_id="job_parallel_peak",
    )
    gate = asyncio.Event()
    entered = 0
    entered_lock = asyncio.Lock()

    async def fetch() -> dict[str, bool]:
        nonlocal entered
        async with entered_lock:
            entered += 1
            if entered == 4:
                gate.set()
        await gate.wait()
        await asyncio.sleep(0)
        return {"ok": True}

    workers = []
    for index in range(4):
        worker = FakeWorker()
        worker.job_id = "job_parallel_peak"
        worker.worker_id = f"worker_parallel_{index}"
        worker.transport_id = f"vpnte-slot-{index + 1}"
        worker.account_registration_id = f"account_{index + 1}"
        worker.current_operation_id = None
        workers.append(worker)

    await asyncio.gather(*(executor._tracked_source_fetch(worker, "web_catalog", fetch) for worker in workers))

    events = await repository.list_recent_events("job_parallel_peak", limit=20)
    starts = [event for event in events if event["event_type"] == "request.started"]
    assert len(starts) == 4
    assert max(int(event["payload"]["peak_requests"]) for event in starts) == 4


@pytest.mark.asyncio
async def test_initial_fetch_wave_waits_until_every_worker_is_ready(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=ClientFactory([]),
        fetch_wave_timeout_seconds=1,
    )
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            desired_workers=4,
        ),
        job_id="job_parallel_wave",
    )
    workers = []
    for index in range(4):
        worker = FakeWorker()
        worker.job_id = "job_parallel_wave"
        worker.worker_id = f"worker_wave_{index}"
        workers.append(worker)
    payload = {"parallel_wave_id": "wave-1", "parallel_wave_size": 4}
    tasks = [
        asyncio.create_task(
            executor._await_parallel_fetch_wave(
                worker,
                {},
                payload,
            )
        )
        for index, worker in enumerate(workers[:3])
    ]
    await asyncio.sleep(0.05)
    assert not any(task.done() for task in tasks)

    tasks.append(
        asyncio.create_task(
            executor._await_parallel_fetch_wave(
                workers[3],
                {},
                payload,
            )
        )
    )
    await asyncio.gather(*tasks)

    events = await repository.list_recent_events("job_parallel_wave", limit=20)
    released = [event for event in events if event["event_type"] == "request.wave_released"]
    assert len(released) == 1
    assert released[0]["payload"]["ready_workers"] == 4
    assert released[0]["payload"]["reason"] == "all_workers_ready"


@pytest.mark.asyncio
async def test_deep_remap_queues_a_new_partition_after_the_root_stream_is_exhausted(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = DiscoveryClientFactory(
        [
            raw_catalog([{"id": 1}]),
            raw_catalog([]),
            raw_catalog([{"id": 1}]),
            raw_catalog([{"id": 3}]),
        ]
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=1,
            desired_workers=1,
            request_budget=1,
        ),
        job_id=worker.job_id,
    )
    initial_map = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert initial_map is not None
    await executor.handle_map_scope(worker, initial_map)
    await repository.complete_operation(initial_map["operation_id"], worker.worker_id)
    initial_fetch = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert initial_fetch is not None
    await executor.handle_fetch_batch(worker, initial_fetch)

    analyzing = await repository.get_job(worker.job_id)
    assert analyzing is not None
    completed = await repository.update_job_state(
        worker.job_id,
        "completed",
        phase="export",
        expected_revision=analyzing["revision"],
    )
    reconfigured = await coordinator.update_job(
        worker.job_id,
        target_unique_cards=40,
        request_budget=2,
        expected_revision=completed["revision"],
    )
    remapping = await coordinator.resume_job(worker.job_id, expected_revision=reconfigured["revision"])
    assert remapping["state"] == "mapping"

    remap_operation = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert remap_operation is not None and remap_operation["payload"]["resume_from_completed"] is True
    await executor.handle_map_scope(worker, remap_operation)

    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    partition_fetches = [
        operation
        for operation in queued
        if operation["kind"] == OperationKind.FETCH_BATCH.value
        and operation["shard_id"] is not None
    ]
    assert len(partition_fetches) == 1
    shard = await repository.get_shard(partition_fetches[0]["shard_id"])
    assert shard is not None and shard["filters"]["attribute[208]"] == "3587"


@pytest.mark.asyncio
async def test_executor_uses_retry_after_without_blocking_a_rate_limited_job(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = ClientFactory(
        [
            raw_catalog([{"id": 1, "title": "Mapping evidence"}]),
            {"status_code": 429, "raw": {}, "retry_after": "2", "bytes": 0},
        ]
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=2,
            desired_workers=1,
            request_budget=1,
        ),
        job_id=worker.job_id,
    )
    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None
    await executor.handle_map_scope(worker, mapping)
    await repository.complete_operation(mapping["operation_id"], worker.worker_id)
    fetch = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert fetch is not None

    with pytest.raises(RetryableOperationError, match="HTTP 429") as error:
        await executor.handle_fetch_batch(worker, fetch)

    assert error.value.failure_kind == "rate_limited"
    assert error.value.retry_at
    job = await repository.get_job(worker.job_id)
    assert job is not None and job["state"] == "running"
    assert worker.quarantine_calls == []


@pytest.mark.asyncio
async def test_executor_collects_one_mobile_page_and_persists_bounded_evidence(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = MobileOnlyClientFactory(
        {
            "kworks_count": 250,
            "paging": {"page": 1, "total": 250},
            "kworks": [{"id": 201, "title": "First mobile offer"}, {"id": 202, "title": "Second mobile offer"}],
            "_request_params": {"categoryId": 38, "classifierId": 1271, "page": 1},
        }
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, classifier_id=1271),
            source_policy=SourcePolicy.MOBILE_FIRST_PAGE_ONLY,
            target_unique_cards=10,
            desired_workers=1,
            request_budget=50,
        ),
        job_id=worker.job_id,
    )

    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None
    await executor.handle_map_scope(worker, mapping)
    await repository.complete_operation(mapping["operation_id"], worker.worker_id)

    shards = await repository.list_shards(worker.job_id)
    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    assert factory.created == []
    assert len(shards) == 1
    assert shards[0]["source"] == "mobile_kworks"
    assert shards[0]["cursor"]["page"] == 1
    assert len(queued) == 1
    assert queued[0]["kind"] == OperationKind.FETCH_BATCH.value
    assert queued[0]["payload"]["cursor"]["page"] == 1
    assert queued[0]["payload"]["remaining_requests"] == 1

    fetch = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert fetch is not None
    await executor.handle_fetch_batch(worker, fetch)

    attempts = await repository.list_operation_attempts(fetch["operation_id"])
    snapshot = await repository.load_export_snapshot(worker.job_id)
    queued = await repository.list_operations(worker.job_id, state=OperationState.QUEUED)
    raw_files = list((tmp_path / "artifacts" / worker.job_id / "raw").glob("*.json.gz"))
    assert len(factory.created) == 1
    assert factory.created[0].mobile_calls == [{"category_id": 38, "classifier_id": 1271, "page": 1}]
    assert factory.created[0].web_calls == 0
    assert factory.created[0].closed is True
    assert len(attempts) == 1
    assert attempts[0]["requested_cursor"]["page"] == 1
    assert attempts[0]["reported_cursor"]["page"] == 1
    assert attempts[0]["raw_response_ref"]
    assert [observation["source"] for observation in snapshot["observations"]] == ["mobile_kworks", "mobile_kworks"]
    assert all(observation["requested_cursor"]["page"] == 1 for observation in snapshot["observations"])
    assert all(observation["reported_cursor"]["page"] == 1 for observation in snapshot["observations"])
    assert raw_files
    assert [operation["kind"] for operation in queued] == [
        OperationKind.ENRICH_LISTING.value,
        OperationKind.ENRICH_LISTING.value,
    ]
    assert {operation["payload"]["listing_id"] for operation in queued} == {1, 2}


@pytest.mark.asyncio
async def test_executor_quarantines_the_current_route_on_a_403_protection_signal(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    factory = ClientFactory(
        [
            raw_catalog([{"id": 1, "title": "Mapping evidence"}]),
            {"status_code": 403, "raw": {}, "retry_after": "30", "bytes": 0},
        ]
    )
    executor = MarketOperationExecutor(
        coordinator,
        client_factory=factory,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    worker = FakeWorker()
    await coordinator.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=2,
            desired_workers=1,
            request_budget=1,
        ),
        job_id=worker.job_id,
    )
    mapping = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert mapping is not None
    await executor.handle_map_scope(worker, mapping)
    await repository.complete_operation(mapping["operation_id"], worker.worker_id)
    fetch = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
    assert fetch is not None

    with pytest.raises(RetryableOperationError, match="HTTP 403") as error:
        await executor.handle_fetch_batch(worker, fetch)

    assert error.value.failure_kind == "protection"
    assert error.value.retry_at
    job = await repository.get_job(worker.job_id)
    assert job is not None and job["state"] == "blocked"
    assert worker.quarantine_calls == [{"reason": "http_403", "until": error.value.retry_at}]
