from __future__ import annotations

import pytest

from src.platforms.kwork_supply.analyzer import MarketResultsAnalyzer
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope
from src.platforms.kwork_supply.repository import MarketJobRepository


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
    assert result["ai_evidence"]["sample_based"] is True


@pytest.mark.asyncio
async def test_analyzer_persists_a_bounded_enrichment_and_ai_evidence_projection(tmp_path):
    repository = MarketJobRepository(tmp_path / "market-jobs.sqlite3")
    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=38, canonical_alias="website-repair"),
            include_ai=False,
        ),
        job_id="job_analysis_without_ai",
    )

    result = await MarketResultsAnalyzer(repository).analyze("job_analysis_without_ai", operation_id="op_analysis")

    assert result["enrichment_selection"]["selection_policy"]["max_items"] == 40
    assert result["ai_evidence"] == {
        "schema_version": 1,
        "enabled": False,
        "sample_based": True,
        "coverage_label": "AI evidence disabled; observed-card metrics remain deterministic.",
    }
    replayed = await MarketResultsAnalyzer(repository).analyze("job_analysis_without_ai", operation_id="op_analysis")
    assert replayed["checkpoint"]["checkpoint_id"] == result["checkpoint"]["checkpoint_id"]
