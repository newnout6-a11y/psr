from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from kwork.exceptions import KworkHTTPException

from src.platforms.kwork_supply.coordinator import MarketScanCoordinator
from src.platforms.kwork_supply.executor import MarketOperationExecutor
from src.platforms.kwork_supply.models import (
    MarketJobCreate,
    MarketScope,
    JobPhase,
    JobState,
    Operation,
    OperationKind,
    OperationState,
    ShardSpec,
)
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.semantic import LocalSemanticAnalyzer
from src.platforms.kwork_supply.worker import RetryableOperationError


async def _collected_job(
    repository: MarketJobRepository,
    *,
    job_id: str,
    listings: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            target_unique_cards=100,
            desired_workers=1,
        ),
        job_id=job_id,
    )
    await repository.create_shard(
        ShardSpec(shard_id=f"shard_{job_id}", job_id=job_id, source="web_catalog", alias="website-repair")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id=f"fetch_{job_id}",
            job_id=job_id,
            shard_id=f"shard_{job_id}",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation(f"worker_{job_id}", job_id=job_id)
    assert leased is not None
    await repository.commit_accepted_batch(
        job_id=job_id,
        shard_id=f"shard_{job_id}",
        operation_id=f"fetch_{job_id}",
        attempt_id=leased["attempt_id"],
        listings=listings,
        source="web_catalog",
        idempotency_key=f"batch_{job_id}",
    )
    return await repository.list_listings(job_id)


@pytest.mark.asyncio
async def test_prepare_enrichment_enforces_price_gate_and_reuses_completed_cache(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    listings = await _collected_job(
        repository,
        job_id="job_price_gate",
        listings=[
            {"id": 1, "gtitle": "Eligible", "userName": "alice", "price": 15_000},
            {"id": 2, "gtitle": "Rejected", "userName": "bob", "price": 15_000.01},
            {"id": 3, "gtitle": "Unknown price", "userName": "carol", "price": None},
        ],
    )
    listing_ids = {str(item["listing_key"]): int(item["listing_id"]) for item in listings}

    prepared = await repository.prepare_listing_enrichment(
        "job_price_gate",
        now="2026-07-12T10:00:00Z",
    )

    assert prepared["total_listings"] == 3
    assert prepared["eligible_listings"] == 2
    assert prepared["price_rejected"] == 1
    assert prepared["queued"] == 2
    rejected = await repository.get_listing_enrichment("job_price_gate", listing_ids["2"])
    assert rejected is not None and rejected["status"] == "reject_price"
    queued = await repository.list_operations("job_price_gate", state=OperationState.QUEUED)
    assert {operation["payload"]["listing_id"] for operation in queued} == {
        listing_ids["1"],
        listing_ids["3"],
    }

    eligible_operation = next(item for item in queued if item["payload"]["listing_id"] == listing_ids["1"])
    await repository.persist_listing_enrichment(
        job_id="job_price_gate",
        listing_id=listing_ids["1"],
        listing_features={
            "status": "ok",
            "generation": eligible_operation["payload"]["generation"],
            "detail": {"description": "Cached description"},
            "extra": {"reviews_count": 3},
            "listing_reviews_count": 3,
            "expires_at": "2026-07-13T10:00:00Z",
        },
        seller_features={
            "seller_key": "alice",
            "status": "ok",
            "profile": {"username": "alice"},
            "seller_rating": 5,
            "expires_at": "2026-07-13T10:00:00Z",
        },
        reviews=[
            {
                "review_key": "review_1",
                "time_added": "1710000000",
                "is_good": True,
                "is_bad": False,
                "text": "Good work",
                "writer": "buyer",
                "raw": {"id": "review_1"},
            }
        ],
        now="2026-07-12T10:05:00Z",
    )
    cached = await repository.prepare_listing_enrichment(
        "job_price_gate",
        now="2026-07-12T10:10:00Z",
    )

    assert cached["cached"] == 1
    persisted = await repository.get_listing_enrichment("job_price_gate", listing_ids["1"])
    seller = await repository.get_seller_enrichment("job_price_gate", "alice")
    assert persisted is not None
    assert persisted["reviews"][0]["text"] == "Good work"
    assert seller is not None and seller["seller_rating"] == 5

    with pytest.raises(ValueError, match="15000"):
        await repository.prepare_listing_enrichment("job_price_gate", price_limit=20_000)


class _EnrichmentWorker:
    job_id = "job_enrichment"
    worker_id = "worker_enrichment"
    transport_proxy_url = "http://127.0.0.1:17990"


class _EnrichmentClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def request(self, endpoint: str, **params: object) -> dict[str, object]:
        self.calls.append((endpoint, dict(params)))
        if endpoint == "getKworkDetails":
            return {
                "response": {
                    "kwork_description": "Build a useful integration",
                    "kwork_instructions": "Send API access",
                    "unit_and_quantity": "One integration",
                    "orders_in_queue_count": 2,
                    "term": 172800,
                    "short_user_info": {"username": "alice"},
                }
            }
        if endpoint == "getKworkDetailsExtra":
            return {
                "response": {
                    "reviews_count": 12,
                    "goodReviews": 11,
                    "badReviews": 1,
                    "last_reviews": [
                        {
                            "id": "review_recent",
                            "time_added": 1710000000,
                            "good": 1,
                            "bad": 0,
                            "text": "Fast result",
                            "writer": {"username": "buyer"},
                        }
                    ],
                }
            }
        if endpoint == "userByUsername":
            return {
                "response": {
                    "user": {
                        "id": 77,
                        "username": "alice",
                        "rating": 5,
                        "rating_count": 101,
                        "reviews_count": 99,
                        "addtime": 1600000000,
                    },
                    "stats": {"completed_orders_count": 120, "active_kworks_count": 4},
                }
            }
        raise AssertionError(f"unexpected endpoint {endpoint}")

    async def close(self) -> None:
        self.closed = True


class _EnrichmentClientFactory:
    def __init__(self) -> None:
        self.created: list[_EnrichmentClient] = []

    def __call__(self, _proxy_url: str | None) -> _EnrichmentClient:
        client = _EnrichmentClient()
        self.created.append(client)
        return client


class _ForbiddenEnrichmentClient(_EnrichmentClient):
    async def request(self, endpoint: str, **params: object) -> dict[str, object]:
        self.calls.append((endpoint, dict(params)))
        raise KworkHTTPException("HTTP 403 for POST /getKworkDetails", status=403, endpoint=endpoint)


class _ForbiddenEnrichmentClientFactory:
    def __init__(self) -> None:
        self.created: list[_ForbiddenEnrichmentClient] = []

    def __call__(self, _proxy_url: str | None) -> _ForbiddenEnrichmentClient:
        client = _ForbiddenEnrichmentClient()
        self.created.append(client)
        return client


class _ProtectionEnrichmentWorker(_EnrichmentWorker):
    def __init__(self) -> None:
        self.quarantine_calls: list[dict[str, str | None]] = []

    async def quarantine_current_transport(self, *, reason: str, until: str | None = None) -> bool:
        self.quarantine_calls.append({"reason": reason, "until": until})
        return True


@pytest.mark.asyncio
async def test_executor_persists_listing_reviews_and_fetches_one_seller_profile_per_job(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    await _collected_job(
        repository,
        job_id="job_enrichment",
        listings=[
            {"id": 10, "gtitle": "First", "userName": "alice", "price": 4900},
            {"id": 11, "gtitle": "Second", "userName": "alice", "price": 5900},
        ],
    )
    prepared = await repository.prepare_listing_enrichment("job_enrichment")
    assert prepared["queued"] == 2
    factory = _EnrichmentClientFactory()
    executor = MarketOperationExecutor(coordinator, client_factory=factory)
    worker = _EnrichmentWorker()

    for _ in range(2):
        operation = await repository.lease_operation(worker.worker_id, job_id=worker.job_id)
        assert operation is not None and operation["kind"] == OperationKind.ENRICH_LISTING.value
        await executor.handle_enrich_listing(worker, operation)
        await repository.complete_operation(operation["operation_id"], worker.worker_id)

    clients = factory.created
    endpoint_calls = [endpoint for client in clients for endpoint, _params in client.calls]
    assert endpoint_calls.count("getKworkDetails") == 2
    assert endpoint_calls.count("getKworkDetailsExtra") == 2
    assert endpoint_calls.count("userByUsername") == 1
    assert all(client.closed for client in clients)

    listings = await repository.list_listings(worker.job_id)
    enriched = await repository.get_listing_enrichment(worker.job_id, listings[0]["listing_id"])
    seller = await repository.get_seller_enrichment(worker.job_id, "alice")
    assert enriched is not None
    assert enriched["status"] == "ok"
    assert enriched["description"] == "Build a useful integration"
    assert enriched["listing_reviews_count"] == 12
    assert enriched["reviews"][0]["time_added"] == "1710000000"
    assert seller is not None
    assert seller["seller_rating_count"] == 101
    assert seller["seller_reviews_count"] == 99
    assert seller["completed_orders_count"] == 120


@pytest.mark.asyncio
async def test_executor_retries_kwork_403_and_quarantines_the_current_transport(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    await _collected_job(
        repository,
        job_id="job_enrichment_403",
        listings=[{"id": 15, "gtitle": "Protected", "userName": "alice", "price": 4900}],
    )
    prepared = await repository.prepare_listing_enrichment("job_enrichment_403")
    assert prepared["queued"] == 1
    operation = await repository.lease_operation("worker_enrichment", job_id="job_enrichment_403")
    assert operation is not None

    factory = _ForbiddenEnrichmentClientFactory()
    executor = MarketOperationExecutor(coordinator, client_factory=factory)
    worker = _ProtectionEnrichmentWorker()
    worker.job_id = "job_enrichment_403"

    with pytest.raises(RetryableOperationError) as exc_info:
        await executor.handle_enrich_listing(worker, operation)

    assert exc_info.value.failure_kind == "protection"
    assert exc_info.value.retry_at is not None
    assert worker.quarantine_calls == [{"reason": "http_403", "until": exc_info.value.retry_at}]
    assert factory.created[0].closed is True


@pytest.mark.asyncio
async def test_executor_rechecks_hard_price_cap_even_for_a_tampered_operation_payload(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    listings = await _collected_job(
        repository,
        job_id="job_tampered_price",
        listings=[{"id": 55, "gtitle": "Too expensive", "userName": "alice", "price": 15_000.01}],
    )
    listing_id = int(listings[0]["listing_id"])
    await repository.enqueue_operation(
        Operation(
            operation_id="enrich_tampered_price",
            job_id="job_tampered_price",
            shard_id=None,
            kind=OperationKind.ENRICH_LISTING,
            payload={"listing_id": listing_id, "generation": 1, "price_limit": 20_000},
        )
    )
    operation = await repository.lease_operation("worker_tampered_price", job_id="job_tampered_price")
    assert operation is not None
    factory = _EnrichmentClientFactory()
    executor = MarketOperationExecutor(coordinator, client_factory=factory)

    class Worker:
        job_id = "job_tampered_price"
        worker_id = "worker_tampered_price"
        transport_proxy_url = None

    await executor.handle_enrich_listing(Worker(), operation)

    assert factory.created == []
    feature = await repository.get_listing_enrichment("job_tampered_price", listing_id)
    assert feature is not None and feature["status"] == "reject_price"


@pytest.mark.asyncio
async def test_terminal_enrichment_failure_blocks_analysis_until_explicit_retry(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    coordinator = MarketScanCoordinator(repository)
    listings = await _collected_job(
        repository,
        job_id="job_terminal_enrichment",
        listings=[{"id": 61, "gtitle": "Eligible", "userName": "alice", "price": 4900}],
    )
    job = await repository.get_job("job_terminal_enrichment")
    assert job is not None
    await repository.update_job_state(
        "job_terminal_enrichment",
        JobState.ENRICHING,
        phase=JobPhase.ENRICH,
        expected_revision=job["revision"],
    )
    listing_id = int(listings[0]["listing_id"])
    await repository.enqueue_operation(
        Operation(
            operation_id="enrich_terminal",
            job_id="job_terminal_enrichment",
            shard_id=None,
            kind=OperationKind.ENRICH_LISTING,
            payload={"listing_id": listing_id, "generation": 1},
        )
    )
    leased = await repository.lease_operation("worker_terminal", job_id="job_terminal_enrichment")
    assert leased is not None
    await repository.fail_operation(
        leased["operation_id"],
        "worker_terminal",
        "detail endpoint rejected the request",
        failure_kind="contract_violation",
    )
    await repository.mark_listing_enrichment_failed(
        "job_terminal_enrichment",
        listing_id,
        generation=1,
        error="detail endpoint rejected the request",
    )

    assert await coordinator.ensure_analysis_operation("job_terminal_enrichment") is None
    blocked = await repository.get_job("job_terminal_enrichment")
    operations = await repository.list_operations("job_terminal_enrichment")
    assert blocked is not None and blocked["state"] == JobState.BLOCKED.value
    assert all(operation["kind"] != OperationKind.ANALYZE_SNAPSHOT.value for operation in operations)


class _SingleGroupEmbedder:
    model_name = "test-single-group"

    def encode(self, texts):
        return [[1.0, 0.0] for _text in texts]


@pytest.mark.asyncio
async def test_repository_projects_enriched_inputs_and_persists_semantic_clusters(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    listings = await _collected_job(
        repository,
        job_id="job_semantic_storage",
        listings=[
            {"id": 21, "gtitle": "Telegram automation", "userName": "alice", "price": 4900},
            {"id": 22, "gtitle": "Telegram integration", "userName": "bob", "price": 5900},
        ],
    )
    for listing in listings:
        listing_id = int(listing["listing_id"])
        seller_key = str(listing["seller_key"])
        await repository.persist_listing_enrichment(
            job_id="job_semantic_storage",
            listing_id=listing_id,
            listing_features={
                "status": "ok",
                "description": "Build a Telegram workflow",
                "listing_reviews_count": 4,
                "good_reviews": 4,
                "bad_reviews": 0,
                "expires_at": "2026-07-13T10:00:00Z",
            },
            seller_features={
                "seller_key": seller_key,
                "status": "ok",
                "seller_rating_count": 20,
                "seller_reviews_count": 20,
                "completed_orders_count": 30,
                "expires_at": "2026-07-13T10:00:00Z",
            },
            reviews=[
                {
                    "review_key": f"review_{listing_id}",
                    "time_added": "2026-07-01T12:00:00Z",
                    "is_good": True,
                    "is_bad": False,
                    "text": "Recent order",
                }
            ],
        )

    inputs = await repository.list_local_analysis_inputs("job_semantic_storage")
    analysis = LocalSemanticAnalyzer(embedder=_SingleGroupEmbedder()).analyze(inputs)
    stored = await repository.replace_semantic_analysis("job_semantic_storage", analysis)

    assert len(inputs) == 2
    assert inputs[0]["seller"]["completed_orders_count"] == 30
    assert len(analysis["clusters"]) == 1
    assert stored["embedding_models"] == ["test-single-group"]
    assert stored["clusters"][0]["member_listing_ids"] == [
        int(listings[0]["listing_id"]),
        int(listings[1]["listing_id"]),
    ]
    assert stored["dossier"]["coverage"]["eligible_listing_count"] == 2
    assert stored["dossier"]["required_response_schema"]["recommended_offer"] == "object or null"


@pytest.mark.asyncio
async def test_local_analysis_uses_resolved_seller_username_when_raw_listing_key_is_numeric(tmp_path: Path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    listings = await _collected_job(
        repository,
        job_id="job_resolved_seller",
        listings=[{"id": 88, "gtitle": "Bot", "seller_key": "77", "userName": "Alice", "price": 4900}],
    )
    listing_id = int(listings[0]["listing_id"])
    await repository.persist_listing_enrichment(
        job_id="job_resolved_seller",
        listing_id=listing_id,
        listing_features={
            "status": "ok",
            "resolved_seller_key": "alice",
            "expires_at": "2026-07-13T10:00:00Z",
        },
        seller_features={
            "seller_key": "alice",
            "status": "ok",
            "seller_rating_count": 42,
            "completed_orders_count": 31,
            "expires_at": "2026-07-13T10:00:00Z",
        },
    )

    inputs = await repository.list_local_analysis_inputs("job_resolved_seller")

    assert inputs[0]["seller_key"] == "alice"
    assert inputs[0]["seller"]["seller_rating_count"] == 42
    assert inputs[0]["seller"]["completed_orders_count"] == 31
