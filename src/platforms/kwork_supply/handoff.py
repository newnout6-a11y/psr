"""Durable transition from a market recommendation to a non-published draft."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import re
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

_EVIDENCE_LISTING_RE = re.compile(r"(?:^|_)listing_(\d+)$", re.I)
_KWORK_URL_ID_RE = re.compile(r"(?:https?://(?:www\.)?kwork\.ru)?/[^/?#]+/(\d+)(?:[/?#]|$)", re.I)
_KWORK_CDN = "https://cdn-edge.kwork.ru"
_VARIANT_DIRECTIONS = (
    "Artifact-first: show one complete finished deliverable edge to edge as the single dominant subject.",
    "Process-proof: show the real specialist, tools, materials, or interaction that produces this exact service.",
    "Outcome-in-use: show a real client or user naturally using the finished result in its intended context.",
)


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


def _variant_count(value: Any) -> int:
    try:
        return max(1, min(3, int(value or 1)))
    except (TypeError, ValueError):
        return 1


def _draft_variants(draft: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(draft, Mapping):
        return []
    variants = draft.get("variants")
    if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
        result = [dict(item) for item in variants if isinstance(item, Mapping)]
        if result:
            return result
    return [dict(draft)]


def _listing_id_from_evidence(value: Any) -> int | None:
    match = _EVIDENCE_LISTING_RE.search(_as_text(value))
    return int(match.group(1)) if match else None


def _cover_url(value: Any) -> str:
    raw = _as_text(value)
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    return f"{_KWORK_CDN}/pics/t3/{raw.lstrip('/')}"


def _portfolio_urls(value: Any, *, limit: int = 4) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    for item in value:
        raw = _as_text(item)
        if not raw:
            continue
        url = raw if raw.startswith(("http://", "https://")) else f"{_KWORK_CDN}/files/portfolio/t3/{raw.lstrip('/')}"
        if url not in result:
            result.append(url)
        if len(result) >= limit:
            break
    return result


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
        if handoff["state"] not in {"fields_confirmed", "draft_generated"}:
            raise MarketJobRepositoryError("confirm Kwork fields before generating a draft")
        options = dict(generation_options) if isinstance(generation_options, Mapping) else {}
        variant_count = _variant_count(options.pop("variant_count", 1))
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
                "use_competitor_image_analysis",
                "use_cover_prompt_llm",
                "cover_prompt_provider",
                "cover_prompt_model",
                "cover_vision_provider",
                "cover_vision_model",
            )
            if key in options
        }
        additional_market_context = allowed_options.pop("market_context", {})
        if not isinstance(additional_market_context, Mapping):
            additional_market_context = {}
        evidence_context, scope = await self._evidence_market_context(handoff)
        market_context = {
            "recommendation_id": handoff["recommendation_id"],
            "handoff_id": handoff_id,
            "source_job_id": handoff["job_id"],
            **evidence_context,
        }
        for key, value in additional_market_context.items():
            if key in {"competitors", "practice_context"} and not value:
                continue
            market_context[key] = value
        request = {
            "category_id": handoff["category_id"],
            "category_name": scope.get("category_name") or scope.get("canonical_alias") or "",
            "classifier_id": handoff.get("classifier_id"),
            "classifier_name": scope.get("classifier_name") or "",
            "service_summary": handoff["service_summary"],
            "price": handoff["price"],
            "work_time": handoff["work_time"],
            "attribute_manifest": handoff["attribute_manifest"],
            "attribute_manifest_hash": handoff["attribute_manifest_hash"],
            "attribute_selection": handoff["attribute_selection"],
            "market_context": market_context,
            **allowed_options,
        }
        ensure_portfolio_assets = getattr(self.autopublish_service, "ensure_portfolio_assets", None)
        variants: list[dict[str, Any]] = []
        images: list[dict[str, Any] | None] = []
        shared_cover_analysis: dict[str, Any] = {}
        for variant_index in range(variant_count):
            variant_request = {
                **request,
                "variant_index": variant_index,
                "variant_count": variant_count,
                "creative_direction": _VARIANT_DIRECTIONS[variant_index],
                **shared_cover_analysis,
            }
            generated = await self.autopublish_service.generate_draft(variant_request)
            draft = generated.get("draft") if isinstance(generated, Mapping) else None
            if not isinstance(draft, Mapping):
                raise MarketJobRepositoryError(f"draft generator returned no draft for variant {variant_index + 1}")
            durable_variant = dict(draft)
            if callable(ensure_portfolio_assets) and not durable_variant.get("portfolio_assets"):
                durable_variant["portfolio_assets"] = ensure_portfolio_assets(durable_variant)
            durable_variant.update(
                {
                    "variant_index": variant_index,
                    "variant_count": variant_count,
                    "creative_direction": _VARIANT_DIRECTIONS[variant_index],
                    "recommendation_id": handoff["recommendation_id"],
                    "handoff_id": handoff_id,
                    "attribute_manifest": handoff["attribute_manifest"],
                    "attribute_manifest_hash": handoff["attribute_manifest_hash"],
                    "attribute_selection": handoff["attribute_selection"],
                    "selection_hash": handoff["selection_hash"],
                }
            )
            variants.append(durable_variant)
            images.append(generated.get("image") if isinstance(generated, Mapping) else None)
            shared_cover_analysis = {
                key: variant_request[key]
                for key in (
                    "_cover_visual_analysis",
                    "_cover_competitor_image_data_urls",
                    "_cover_competitor_image_meta",
                )
                if key in variant_request
            }

        durable_draft = dict(variants[0])
        durable_draft["generation_options"] = {
            "variant_count": variant_count,
            "use_competitor_image_analysis": allowed_options.get("use_competitor_image_analysis", True) is not False,
        }
        if variant_count > 1:
            durable_draft.update(
                {
                    "active_variant_index": 0,
                    "variant_count": variant_count,
                    "variants": variants,
                }
            )
        stored = await self.repository.store_draft_handoff_draft(
            handoff_id,
            draft=durable_draft,
            draft_hash=self.autopublish_service.draft_hash(durable_draft),
        )
        return {
            "handoff": stored,
            "draft": stored["draft"],
            "image": images[0],
            "images": images,
            "variants": variants,
        }

    async def _evidence_market_context(self, handoff: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        recommendation, job = await asyncio.gather(
            self.repository.get_recommendation(str(handoff["recommendation_id"])),
            self.repository.get_job(str(handoff["job_id"])),
        )
        evidence_ids = recommendation.get("evidence_ids") if isinstance(recommendation, Mapping) else []
        listing_ids = [
            identifier for identifier in (_listing_id_from_evidence(item) for item in evidence_ids or []) if identifier
        ]
        listings = await asyncio.gather(
            *(self.repository.get_listing(str(handoff["job_id"]), listing_id) for listing_id in listing_ids[:8])
        )
        competitors: list[dict[str, Any]] = []
        for listing in listings:
            if not isinstance(listing, Mapping):
                continue
            canonical = listing.get("canonical") if isinstance(listing.get("canonical"), Mapping) else {}
            image_url = _cover_url(canonical.get("photo") or canonical.get("image_url"))
            portfolio_images = _portfolio_urls(canonical.get("portfolios"))
            if image_url and image_url not in portfolio_images:
                portfolio_images.insert(0, image_url)
            competitors.append(
                {
                    "listing_id": listing.get("listing_id"),
                    "title": listing.get("title") or canonical.get("title") or canonical.get("gtitle"),
                    "price": listing.get("price") or canonical.get("price"),
                    "image_url": image_url,
                    "portfolio_images": portfolio_images[:4],
                    "service_size": canonical.get("baseVolumeShortName") or canonical.get("service_size") or "",
                    "url": canonical.get("url") or "",
                }
            )
        scope = job.get("scope") if isinstance(job, Mapping) and isinstance(job.get("scope"), Mapping) else {}
        return {
            "evidence_ids": list(evidence_ids or []),
            "competitors": competitors,
            "practice_context": competitors,
        }, dict(scope)

    async def publish_draft(
        self,
        handoff_id: str,
        *,
        dry_run: bool = True,
        confirm_token: str = "",
        confirmation: str = "",
        variant_index: int = 0,
    ) -> dict[str, Any]:
        """Publish only an explicit, freshly revalidated durable draft."""

        handoff = await self._require_handoff(handoff_id)
        if handoff["state"] != "draft_generated":
            raise MarketJobRepositoryError("generate a durable draft before publication")
        manifest = await self.manifest_loader(
            int(handoff["category_id"]),
            handoff.get("classifier_id"),
            dict(handoff.get("attribute_selection") or {}),
            "ru",
        )
        if not manifest.get("success", True):
            detail = _as_text(manifest.get("detail")) or _as_text(manifest.get("code")) or "Kwork form manifest failed"
            raise MarketJobRepositoryError(detail)
        normalized = normalize_attribute_selection(manifest, handoff.get("attribute_selection") or {})
        if (
            normalized["manifest_hash"] != handoff.get("attribute_manifest_hash")
            or normalized["selection"] != handoff.get("attribute_selection")
            or not normalized["valid"]
        ):
            raise MarketJobRepositoryError(
                "Kwork form manifest changed; refresh and reconfirm fields before publishing"
            )
        variants = _draft_variants(handoff.get("draft") if isinstance(handoff.get("draft"), Mapping) else {})
        if variant_index < 0 or variant_index >= len(variants):
            raise MarketJobRepositoryError(
                f"draft variant {variant_index + 1} is unavailable; generated variants: {len(variants)}"
            )
        selected_draft = variants[variant_index]
        selected_draft_hash = self.autopublish_service.draft_hash(selected_draft)
        result = await self.autopublish_service.publish_draft(
            selected_draft,
            dry_run=dry_run,
            confirm_token=confirm_token,
            confirmation=confirmation,
        )
        result["variant_index"] = variant_index
        result["variant_count"] = len(variants)
        result["variant_draft_hash"] = selected_draft_hash
        if dry_run or not result.get("ok"):
            return {"handoff": handoff, "publish": result, "published_listing": None}
        published = await self.repository.record_published_listing(
            handoff_id,
            publish_result=result,
            kwork_id=self._published_kwork_id(result),
            draft_hash=selected_draft_hash,
        )
        return {"handoff": handoff, "publish": result, "published_listing": published}

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

    @staticmethod
    def _published_kwork_id(result: Mapping[str, Any]) -> str | None:
        def id_from_url(value: Any) -> str | None:
            match = _KWORK_URL_ID_RE.search(_as_text(value))
            return match.group(1) if match else None

        for key in ("kwork_id", "listing_id"):
            value = _as_text(result.get(key))
            if value:
                return value
        for nested_key in ("verify_result", "save_result"):
            nested = result.get(nested_key)
            if not isinstance(nested, Mapping):
                continue
            for key in ("kwork_id", "listing_id", "id"):
                value = _as_text(nested.get(key))
                if value:
                    return value
            for key in ("url", "redirect_url", "final_url", "redirectUrl"):
                value = id_from_url(nested.get(key))
                if value:
                    return value
            raw = nested.get("raw")
            if isinstance(raw, Mapping):
                for key in ("url", "redirect_url", "final_url", "redirectUrl"):
                    value = id_from_url(raw.get(key))
                    if value:
                        return value
            for checked in nested.get("checked") or []:
                if not isinstance(checked, Mapping):
                    continue
                for key in ("url", "final_url"):
                    value = id_from_url(checked.get(key))
                    if value:
                        return value
        return None

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
