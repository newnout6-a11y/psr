"""Durable server-side results analysis for completed collection frontiers."""

from __future__ import annotations

from typing import Any

from .metrics import market_metrics_to_wire, project_market_metrics
from .repository import MarketJobNotFoundError, MarketJobRepository
from .semantic import LocalSemanticAnalyzer


class MarketResultsAnalyzer:
    """Project deterministic observed-listing metrics into a durable checkpoint."""

    def __init__(
        self,
        repository: MarketJobRepository,
        *,
        semantic_analyzer: LocalSemanticAnalyzer | None = None,
    ) -> None:
        self.repository = repository
        self.semantic_analyzer = semantic_analyzer or LocalSemanticAnalyzer()

    async def analyze(self, job_id: str, *, operation_id: str | None = None) -> dict[str, Any]:
        job = await self.repository.get_job(job_id)
        if job is None:
            raise MarketJobNotFoundError(f"market job {job_id!r} was not found")
        if operation_id is not None:
            checkpoints = await self.repository.list_checkpoints(job_id, limit=100)
            for checkpoint in checkpoints:
                frontier = checkpoint.get("frontier") or {}
                metrics = checkpoint.get("metrics") or {}
                if (
                    frontier.get("phase") == "analyze"
                    and frontier.get("operation_id") == operation_id
                    and isinstance(metrics.get("market_metrics"), dict)
                ):
                    return {
                        "job_id": job_id,
                        "metrics": metrics["market_metrics"],
                        "enrichment_selection": metrics.get("enrichment_selection"),
                        "ai_evidence": metrics.get("ai_evidence"),
                        "semantic_analysis": metrics.get("semantic_analysis"),
                        "checkpoint": checkpoint,
                    }
        listings = await self.repository.list_listing_metrics_inputs(job_id)
        aggregate_total = (job.get("counters") or {}).get("aggregate_scope_total")
        metrics = market_metrics_to_wire(
            project_market_metrics(listings, aggregate_scope_total=aggregate_total)
        )
        local_inputs = await self.repository.list_local_analysis_inputs(job_id)
        embedding_cache = await self.repository.get_embedding_cache(
            job_id,
            self.semantic_analyzer.model_name,
        )
        semantic_result = self.semantic_analyzer.analyze(
            local_inputs,
            cached_embeddings=embedding_cache,
        )
        semantic_analysis = await self.repository.replace_semantic_analysis(job_id, semantic_result)
        price_rejected = max(len(listings) - len(local_inputs), 0)
        selection = {
            "schema_version": 2,
            "strategy": "full durable enrichment before local semantic clustering",
            "raw_listing_count": len(listings),
            "eligible_listing_count": len(local_inputs),
            "price_or_data_rejected_count": price_rejected,
            "selected_count": len(local_inputs),
            "omitted_listing_count": 0,
            "selection_is_bounded": False,
        }
        ai_evidence = (
            {
                "schema_version": 2,
                "enabled": True,
                "sample_based": False,
                "coverage_label": "Local semantic groups with bounded representatives; raw listing rows are excluded from Terra input.",
                "terra_dossier": semantic_analysis["dossier"],
            }
            if bool(job.get("include_ai"))
            else {
                "schema_version": 2,
                "enabled": False,
                "sample_based": False,
                "coverage_label": "AI evidence disabled; local semantic analysis remains durable.",
            }
        )
        checkpoint = await self.repository.create_checkpoint(
            job_id,
            frontier={
                "phase": "analyze",
                "listing_count": len(listings),
                "operation_id": operation_id,
            },
            metrics={
                "market_metrics": metrics,
                "enrichment_selection": selection,
                "ai_evidence": ai_evidence,
                "semantic_analysis": semantic_analysis,
            },
        )
        return {
            "job_id": job_id,
            "metrics": metrics,
            "enrichment_selection": selection,
            "ai_evidence": ai_evidence,
            "semantic_analysis": semantic_analysis,
            "checkpoint": checkpoint,
        }
