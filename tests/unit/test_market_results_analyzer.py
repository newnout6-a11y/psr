from __future__ import annotations

import pytest

from src.platforms.kwork_supply.analyzer import MarketResultsAnalyzer
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, Operation, OperationKind, ShardSpec
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.semantic import LocalSemanticAnalyzer


class _AnalyzerEmbedder:
    model_name = "test-analyzer-embedder"

    def encode(self, texts):
        return [[1.0, 0.0] for _text in texts]


@pytest.mark.asyncio
async def test_analyzer_writes_lossless_metrics_to_a_durable_checkpoint(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    await repository.create_job(
        MarketJobCreate(scope=MarketScope(category_id=38, canonical_alias="website-repair")),
        job_id="job_analysis_result",
    )

    result = await MarketResultsAnalyzer(repository).analyze("job_analysis_result")

    assert result["metrics"]["observed_cards"]["observed_listing_count"] == 0
    assert result["metrics"]["price_distribution"]["p50"] is None
    assert result["checkpoint"]["metrics"]["market_metrics"] == result["metrics"]
    assert result["enrichment_selection"]["selected_count"] == 0
    assert result["enrichment_selection"]["selection_is_bounded"] is False
    assert result["ai_evidence"]["sample_based"] is False
    assert result["semantic_analysis"]["cluster_count"] == 0


@pytest.mark.asyncio
async def test_analyzer_persists_full_durable_enrichment_and_semantic_projection(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            include_ai=False,
        ),
        job_id="job_analysis_without_ai",
    )

    result = await MarketResultsAnalyzer(repository).analyze("job_analysis_without_ai", operation_id="op_analysis")

    assert result["enrichment_selection"] == {
        "schema_version": 2,
        "strategy": "full durable enrichment before local semantic clustering",
        "raw_listing_count": 0,
        "eligible_listing_count": 0,
        "price_or_data_rejected_count": 0,
        "selected_count": 0,
        "omitted_listing_count": 0,
        "selection_is_bounded": False,
    }
    assert result["ai_evidence"] == {
        "schema_version": 2,
        "enabled": False,
        "sample_based": False,
        "coverage_label": "AI evidence disabled; local semantic analysis remains durable.",
    }
    replayed = await MarketResultsAnalyzer(repository).analyze("job_analysis_without_ai", operation_id="op_analysis")
    assert replayed["checkpoint"]["checkpoint_id"] == result["checkpoint"]["checkpoint_id"]


@pytest.mark.asyncio
async def test_analyzer_uses_durable_enrichment_for_clusters_and_terra_dossier(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    await repository.create_job(
        MarketJobCreate(scope=MarketScope(category_id=38, canonical_alias="website-repair")),
        job_id="job_local_semantic",
    )
    await repository.create_shard(
        ShardSpec(
            shard_id="shard_local_semantic",
            job_id="job_local_semantic",
            source="web_catalog",
            alias="website-repair",
        )
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="op_local_semantic_fetch",
            job_id="job_local_semantic",
            shard_id="shard_local_semantic",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation("worker_semantic", job_id="job_local_semantic")
    assert leased is not None
    await repository.commit_accepted_batch(
        job_id="job_local_semantic",
        shard_id="shard_local_semantic",
        operation_id="op_local_semantic_fetch",
        attempt_id=leased["attempt_id"],
        listings=[
            {"id": 1, "gtitle": "Telegram bot for leads", "userName": "alice", "price": 4900},
            {"id": 2, "gtitle": "Telegram bot for sales", "userName": "bob", "price": 5900},
        ],
        idempotency_key="local-semantic-batch",
    )
    for listing in await repository.list_listings("job_local_semantic"):
        await repository.persist_listing_enrichment(
            job_id="job_local_semantic",
            listing_id=listing["listing_id"],
            listing_features={
                "status": "ok",
                "description": "Automation for Telegram leads",
                "listing_reviews_count": 4,
                "queue_count": 1,
                "expires_at": "2026-07-13T10:00:00Z",
            },
            seller_features={
                "seller_key": listing["seller_key"],
                "status": "ok",
                "seller_rating_count": 10,
                "completed_orders_count": 20,
                "expires_at": "2026-07-13T10:00:00Z",
            },
            reviews=[
                {
                    "review_key": f"review_{listing['listing_id']}",
                    "time_added": "2026-07-01T12:00:00Z",
                    "is_good": True,
                    "is_bad": False,
                    "text": "Recent order",
                }
            ],
        )

    result = await MarketResultsAnalyzer(
        repository,
        semantic_analyzer=LocalSemanticAnalyzer(embedder=_AnalyzerEmbedder()),
        ai_verdict_provider=lambda packet: _fake_ai_verdict(packet),
    ).analyze("job_local_semantic")

    assert result["semantic_analysis"]["cluster_count"] == 1
    assert result["semantic_analysis"]["embedding_models"] == ["test-analyzer-embedder"]
    assert result["ai_evidence"]["sample_based"] is False
    assert len(result["ai_evidence"]["terra_dossier"]["groups"]) == 1
    assert result["ai_verdict"]["status"] == "ok"
    assert result["ai_verdict"]["market_verdict"] == "promising_for_entry"
    assert result["checkpoint"]["metrics"]["ai_verdict"] == result["ai_verdict"]


async def _fake_ai_verdict(packet):
    group = packet["dossier"]["groups"][0]
    return {
        "market_verdict": "promising_for_entry",
        "confidence": 78,
        "summary": "Есть подтверждённая группа с умеренным барьером входа.",
        "reasons": ["Есть свежие отзывы и несколько продавцов."],
        "opportunities": [
            {
                "cluster_id": group["cluster_id"],
                "label": group["label"],
                "verdict": "promising_for_entry",
                "confidence": 78,
                "why": "Спрос подтверждён локальными доказательствами.",
                "evidence_ids": group["evidence_ids"],
            }
        ],
        "risks": [],
        "rejected_cases": [],
    }
