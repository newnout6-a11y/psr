"""Durable transition from a market recommendation to a non-published draft."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from src.platforms.kwork_autopublish import KworkAutopublishService
from src.platforms.kwork_form_contract import normalize_attribute_selection

from .repository import MarketJobRepository, MarketJobRepositoryError


ManifestLoader = Callable[[int, int | None, dict[str, Any], str], Awaitable[dict[str, Any]]]

_TERRA_REQUIRED_KEYS = {
    "verdict",
    "confidence",
    "reasons",
    "evidence_ids",
    "recommended_offer",
    "rejected_cases",
}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def validate_terra_recommendation(result: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the bounded JSON object accepted from the final Terra step."""

    if not isinstance(result, Mapping):
        raise TypeError("Terra recommendation must be a mapping")
    missing = sorted(key for key in _TERRA_REQUIRED_KEYS if key not in result)
    if missing:
        raise ValueError(f"Terra recommendation is missing: {', '.join(missing)}")
    verdict = _as_text(result.get("verdict"))
    if verdict not in {"recommend", "reject", "needs_review"}:
        raise ValueError("Terra verdict must be recommend, reject, or needs_review")
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Terra confidence must be numeric") from exc
    if not 0 <= confidence <= 1:
        raise ValueError("Terra confidence must be between 0 and 1")
    reasons = result.get("reasons")
    evidence_ids = result.get("evidence_ids")
    rejected_cases = result.get("rejected_cases")
    offer = result.get("recommended_offer")
    if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes)):
        raise ValueError("Terra reasons must be an array")
    if not isinstance(evidence_ids, Sequence) or isinstance(evidence_ids, (str, bytes)):
        raise ValueError("Terra evidence_ids must be an array")
    if not isinstance(rejected_cases, Sequence) or isinstance(rejected_cases, (str, bytes)):
        raise ValueError("Terra rejected_cases must be an array")
    if not isinstance(offer, Mapping):
        raise ValueError("Terra recommended_offer must be an object")
    clean_reasons = [_as_text(item) for item in reasons if _as_text(item)]
    clean_evidence = list(dict.fromkeys(_as_text(item) for item in evidence_ids if _as_text(item)))
    if verdict == "recommend" and not clean_evidence:
        raise ValueError("a recommended offer requires evidence_ids")
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": clean_reasons,
        "evidence_ids": clean_evidence,
        "recommended_offer": dict(offer),
        "rejected_cases": [item for item in rejected_cases if isinstance(item, (str, int, float, bool, dict, list))],
    }


class MarketRecommendationHandoffService:
    """Own recommendation confirmation, canonical field mapping, and draft generation."""

    def __init__(
        self,
        repository: MarketJobRepository,
        manifest_loader: ManifestLoader,
        *,
        autopublish_service: KworkAutopublishService | None = None,
    ) -> None:
        self.repository = repository
        self.manifest_loader = manifest_loader
        self.autopublish_service = autopublish_service or KworkAutopublishService()

    async def propose(
        self,
        job_id: str,
        *,
        category_id: int,
        service_summary: str,
        price: int,
        work_time: int,
        terra_result: Mapping[str, Any],
        classifier_id: int | None = None,
        source_cluster_id: str | None = None,
    ) -> dict[str, Any]:
        terra = validate_terra_recommendation(terra_result)
        if terra["verdict"] != "recommend":
            raise ValueError("only a Terra recommend verdict can create a draft recommendation")
        summary = _as_text(service_summary)
        if not summary:
            raise ValueError("service_summary cannot be blank")
        clean_price = _as_positive_int(price, "price")
        clean_work_time = _as_positive_int(work_time, "work_time")
        await self._validate_evidence(job_id, terra["evidence_ids"], source_cluster_id)
        return await self.repository.create_recommendation(
            job_id,
            category_id=_as_positive_int(category_id, "category_id"),
            classifier_id=classifier_id,
            service_summary=summary,
            price=clean_price,
            work_time=clean_work_time,
            source_cluster_id=_as_text(source_cluster_id) or None,
            evidence_ids=terra["evidence_ids"],
            terra_result=terra,
        )

    async def confirm_recommendation(
        self,
        recommendation_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self.repository.transition_recommendation(
            recommendation_id,
            "recommendation_confirmed",
            expected_revision=expected_revision,
        )

    async def reject_recommendation(
        self,
        recommendation_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self.repository.transition_recommendation(
            recommendation_id,
            "rejected",
            expected_revision=expected_revision,
        )

    async def refresh_manifest(self, handoff_id: str, *, lang: str = "ru") -> dict[str, Any]:
        handoff = await self._require_handoff(handoff_id)
        manifest = await self.manifest_loader(
            int(handoff["category_id"]),
            handoff.get("classifier_id"),
            dict(handoff.get("attribute_selection") or {}),
            lang or "ru",
        )
        if not manifest.get("success", True):
            detail = _as_text(manifest.get("detail")) or _as_text(manifest.get("code")) or "Kwork form manifest failed"
            raise MarketJobRepositoryError(detail)
        normalized = normalize_attribute_selection(manifest, handoff.get("attribute_selection") or {})
        return await self._save_normalized_fields(
            handoff,
            normalized,
            fields_confirmed=False,
            expected_manifest_hash=handoff.get("attribute_manifest_hash"),
        )

    async def update_selection(
        self,
        handoff_id: str,
        *,
        selection: Mapping[str, Any],
        expected_manifest_hash: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        handoff = await self._require_handoff(handoff_id)
        current_hash = _as_text(handoff.get("attribute_manifest_hash"))
        if not current_hash:
            raise ValueError("load the current Kwork manifest before selecting fields")
        if current_hash != _as_text(expected_manifest_hash):
            raise MarketJobRepositoryError("Kwork form manifest changed; reload it before confirming fields")
        manifest = handoff.get("attribute_manifest")
        normalized = normalize_attribute_selection(manifest if isinstance(manifest, Mapping) else {}, selection)
        return await self._save_normalized_fields(
            handoff,
            normalized,
            fields_confirmed=bool(confirm and normalized["valid"]),
            expected_manifest_hash=current_hash,
        )

    async def generate_draft(
        self,
        handoff_id: str,
        *,
        generation_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        handoff = await self._require_handoff(handoff_id)
        if handoff["state"] != "fields_confirmed":
            raise MarketJobRepositoryError("confirm Kwork fields before generating a draft")
        options = dict(generation_options) if isinstance(generation_options, Mapping) else {}
        allowed_options = {
            key: options[key]
            for key in (
                "brief",
                "audience",
                "portfolio_context",
                "market_context",
                "lang",
                "use_llm",
                "generate_image",
                "image_context",
                "cover_text",
                "cover_subtitle",
                "cover_text_overlay",
            )
            if key in options
        }
        additional_market_context = allowed_options.pop("market_context", {})
        if not isinstance(additional_market_context, Mapping):
            additional_market_context = {}
        request = {
            "category_id": handoff["category_id"],
            "classifier_id": handoff.get("classifier_id"),
            "service_summary": handoff["service_summary"],
            "price": handoff["price"],
            "work_time": handoff["work_time"],
            "attribute_manifest": handoff["attribute_manifest"],
            "attribute_manifest_hash": handoff["attribute_manifest_hash"],
            "attribute_selection": handoff["attribute_selection"],
            "market_context": {
                "recommendation_id": handoff["recommendation_id"],
                "handoff_id": handoff_id,
                **additional_market_context,
            },
            **allowed_options,
        }
        generated = await self.autopublish_service.generate_draft(request)
        draft = generated.get("draft") if isinstance(generated, Mapping) else None
        if not isinstance(draft, Mapping):
            raise MarketJobRepositoryError("draft generator returned no draft")
        durable_draft = dict(draft)
        durable_draft.update(
            {
                "recommendation_id": handoff["recommendation_id"],
                "handoff_id": handoff_id,
                "attribute_manifest": handoff["attribute_manifest"],
                "attribute_manifest_hash": handoff["attribute_manifest_hash"],
                "attribute_selection": handoff["attribute_selection"],
                "selection_hash": handoff["selection_hash"],
            }
        )
        stored = await self.repository.store_draft_handoff_draft(
            handoff_id,
            draft=durable_draft,
            draft_hash=self.autopublish_service.draft_hash(durable_draft),
        )
        return {"handoff": stored, "draft": stored["draft"], "image": generated.get("image")}

    async def _validate_evidence(
        self,
        job_id: str,
        evidence_ids: Sequence[str],
        source_cluster_id: str | None,
    ) -> None:
        semantic = await self.repository.get_semantic_analysis(job_id)
        allowed: set[str] = set()
        cluster_ids: set[str] = set()
        for cluster in semantic.get("clusters") or []:
            if not isinstance(cluster, Mapping):
                continue
            cluster_id = _as_text(cluster.get("cluster_id"))
            if cluster_id:
                cluster_ids.add(cluster_id)
            dossier = cluster.get("dossier") if isinstance(cluster.get("dossier"), Mapping) else {}
            for evidence_id in dossier.get("evidence_ids") or []:
                text = _as_text(evidence_id)
                if text:
                    allowed.add(text)
            for representative in dossier.get("representatives") or []:
                if isinstance(representative, Mapping):
                    text = _as_text(representative.get("evidence_id"))
                    if text:
                        allowed.add(text)
        if source_cluster_id and source_cluster_id not in cluster_ids:
            raise ValueError("source_cluster_id does not belong to this market job")
        invalid = sorted(set(evidence_ids).difference(allowed))
        if invalid:
            raise ValueError(f"Terra evidence_ids are not in the durable dossier: {', '.join(invalid)}")

    async def _require_handoff(self, handoff_id: str) -> dict[str, Any]:
        handoff = await self.repository.get_draft_handoff(handoff_id)
        if handoff is None:
            raise MarketJobRepositoryError(f"market draft handoff {handoff_id!r} was not found")
        return handoff

    async def _save_normalized_fields(
        self,
        handoff: Mapping[str, Any],
        normalized: Mapping[str, Any],
        *,
        fields_confirmed: bool,
        expected_manifest_hash: str | None,
    ) -> dict[str, Any]:
        validation = {
            "valid": bool(normalized.get("valid")),
            "clean": bool(normalized.get("clean")),
            "issues": normalized.get("issues") or {},
            "unresolved_required": normalized.get("unresolved_required") or [],
            "active_controls": normalized.get("active_controls") or [],
        }
        return await self.repository.save_draft_handoff_fields(
            str(handoff["handoff_id"]),
            manifest=normalized["manifest"],
            manifest_hash=str(normalized["manifest_hash"]),
            selection=normalized["selection"],
            selection_hash=str(normalized["selection_hash"]),
            validation=validation,
            fields_confirmed=fields_confirmed,
            expected_manifest_hash=expected_manifest_hash,
        )
