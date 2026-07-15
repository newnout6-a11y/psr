"""Durable server-side results analysis for completed collection frontiers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
from typing import Any

from loguru import logger

from .metrics import market_metrics_to_wire, project_market_metrics
from .repository import MarketJobNotFoundError, MarketJobRepository
from .semantic import LocalSemanticAnalyzer


AiVerdictProvider = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _string_list(value: object, *, limit: int = 30) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def _bounded_int(value: object, *, minimum: int = 0, maximum: int = 100) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return max(minimum, min(maximum, parsed))


def _normalize_ai_verdict(value: Mapping[str, Any], dossier: Mapping[str, Any]) -> dict[str, Any]:
    allowed_verdicts = {"promising_for_entry", "popular_crowded", "do_not_take", "mixed"}
    verdict = str(value.get("market_verdict") or value.get("verdict") or "mixed").strip()
    if verdict not in allowed_verdicts:
        verdict = "mixed"
    confidence = _bounded_int(value.get("confidence"))
    allowed_evidence = {
        str(evidence_id)
        for group in dossier.get("groups", [])
        if isinstance(group, Mapping)
        for evidence_id in group.get("evidence_ids", [])
    }
    opportunities: list[dict[str, Any]] = []
    raw_opportunities = value.get("opportunities")
    if isinstance(raw_opportunities, list):
        for raw in raw_opportunities[:30]:
            if not isinstance(raw, Mapping):
                continue
            evidence_ids = [
                evidence_id
                for evidence_id in _string_list(raw.get("evidence_ids"), limit=30)
                if evidence_id in allowed_evidence
            ]
            opportunities.append(
                {
                    "cluster_id": str(raw.get("cluster_id") or "").strip() or None,
                    "label": str(raw.get("label") or raw.get("name") or "").strip(),
                    "verdict": str(raw.get("verdict") or "mixed").strip(),
                    "confidence": _bounded_int(raw.get("confidence")),
                    "why": str(raw.get("why") or raw.get("reason") or "").strip(),
                    "evidence_ids": evidence_ids,
                    "recommended_offer": raw.get("recommended_offer") if isinstance(raw.get("recommended_offer"), Mapping) else None,
                }
            )
    rejected_cases = value.get("rejected_cases")
    return {
        "schema_version": 1,
        "status": "ok",
        "market_verdict": verdict,
        "confidence": confidence,
        "summary": str(value.get("summary") or "").strip(),
        "reasons": _string_list(value.get("reasons")),
        "opportunities": opportunities,
        "risks": _string_list(value.get("risks")),
        "rejected_cases": rejected_cases[:30] if isinstance(rejected_cases, list) else [],
        "evidence_group_count": len(dossier.get("groups", [])) if isinstance(dossier.get("groups"), list) else 0,
    }


async def generate_market_ai_verdict(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    """Ask the configured market-analysis model for one bounded final verdict."""

    from src.brain.llm_router import get_llm_router

    system_prompt = (
        "Ты аналитик рынка услуг Kwork. Используй только переданный доказательный dossier и агрегатные метрики. "
        "Не выдумывай спрос, технологии или факты. Верни только JSON с полями market_verdict, confidence, "
        "summary, reasons, opportunities, risks, rejected_cases. market_verdict: promising_for_entry, "
        "popular_crowded, do_not_take или mixed. В opportunities используй cluster_id, label, verdict, "
        "confidence, why, evidence_ids, recommended_offer. Все evidence_ids должны существовать во входе."
    )
    prompt = "Сформируй итоговый проверяемый вердикт по завершённому сбору.\n\n" + json.dumps(
        packet,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = await get_llm_router().generate(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=0.15,
        max_tokens=5000,
        task="market_analysis",
    )
    parsed = json.loads(_strip_json_fence(response))
    if not isinstance(parsed, Mapping):
        raise ValueError("market analysis response is not a JSON object")
    return parsed


class MarketResultsAnalyzer:
    """Project deterministic observed-listing metrics into a durable checkpoint."""

    def __init__(
        self,
        repository: MarketJobRepository,
        *,
        semantic_analyzer: LocalSemanticAnalyzer | None = None,
        ai_verdict_provider: AiVerdictProvider | None = None,
    ) -> None:
        self.repository = repository
        self.semantic_analyzer = semantic_analyzer or LocalSemanticAnalyzer()
        self.ai_verdict_provider = ai_verdict_provider
        self._semantic_lock = asyncio.Lock()

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
                        "ai_verdict": metrics.get("ai_verdict"),
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
        async with self._semantic_lock:
            semantic_result = await asyncio.to_thread(
                self.semantic_analyzer.analyze,
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
        dossier = semantic_analysis.get("dossier") if isinstance(semantic_analysis, Mapping) else None
        if not bool(job.get("include_ai")):
            ai_verdict: dict[str, Any] = {"schema_version": 1, "status": "disabled"}
        elif not isinstance(dossier, Mapping) or not dossier.get("groups"):
            ai_verdict = {
                "schema_version": 1,
                "status": "insufficient_data",
                "summary": "Недостаточно обогащённых доказательств для итогового AI-вердикта.",
            }
        elif self.ai_verdict_provider is None:
            ai_verdict = {"schema_version": 1, "status": "not_configured"}
        else:
            try:
                raw_verdict = await self.ai_verdict_provider(
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "scope": job.get("scope") or {},
                        "metrics": metrics,
                        "dossier": dossier,
                    }
                )
                ai_verdict = _normalize_ai_verdict(raw_verdict, dossier)
            except Exception as exc:
                logger.warning(f"Kwork final AI verdict unavailable: {type(exc).__name__}: {exc}")
                ai_verdict = {
                    "schema_version": 1,
                    "status": "unavailable",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
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
                "ai_verdict": ai_verdict,
            },
        )
        return {
            "job_id": job_id,
            "metrics": metrics,
            "enrichment_selection": selection,
            "ai_evidence": ai_evidence,
            "semantic_analysis": semantic_analysis,
            "ai_verdict": ai_verdict,
            "checkpoint": checkpoint,
        }
