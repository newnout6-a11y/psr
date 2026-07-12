"""Durable server-side results analysis for completed collection frontiers."""

from __future__ import annotations

from typing import Any

from .ai_evidence import build_ai_evidence_packet
from .enrichment import EnrichmentPolicy, select_enrichment_candidates
from .metrics import market_metrics_to_wire, project_market_metrics
from .repository import MarketJobNotFoundError, MarketJobRepository


class MarketResultsAnalyzer:
    """Project deterministic observed-listing metrics into a durable checkpoint."""

    def __init__(self, repository: MarketJobRepository) -> None:
        self.repository = repository

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
                        "checkpoint": checkpoint,
                    }
        listings = await self.repository.list_listing_metrics_inputs(job_id)
        aggregate_total = (job.get("counters") or {}).get("aggregate_scope_total")
        metrics = market_metrics_to_wire(
            project_market_metrics(listings, aggregate_scope_total=aggregate_total)
        )
        selection = select_enrichment_candidates(
            listings,
            policy=EnrichmentPolicy(max_items=self._enrichment_limit(str(job.get("profile") or ""))),
        )
        ai_evidence = (
            build_ai_evidence_packet(listings, metrics, selection)
            if bool(job.get("include_ai"))
            else {
                "schema_version": 1,
                "enabled": False,
                "sample_based": True,
                "coverage_label": "AI evidence disabled; observed-card metrics remain deterministic.",
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
            },
        )
        return {
            "job_id": job_id,
            "metrics": metrics,
            "enrichment_selection": selection,
            "ai_evidence": ai_evidence,
            "checkpoint": checkpoint,
        }

    @staticmethod
    def _enrichment_limit(profile: str) -> int:
        normalized = profile.strip().lower()
        if normalized in {"full", "custom", "extended", "expanded"}:
            return 250
        if normalized in {"deep", "detailed"}:
            return 100
        return 40
