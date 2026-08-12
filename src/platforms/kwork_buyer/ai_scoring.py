"""Versioned, task-routed final scoring for Buyer Search projects.

The deterministic score remains the fast first pass.  This module adds an
explicit, auditable model pass without making a model response part of the
project's canonical data or allowing it to influence a remote action.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import inspect
import json
import math
from typing import Any, Protocol, runtime_checkable


BUYER_FINAL_SCORING_TASK = "scoring"
DEFAULT_BUYER_FINAL_SCORING_PROFILE_ID = "buyer-ai-final"
DEFAULT_BUYER_FINAL_SCORING_PROFILE_VERSION = "1"


class BuyerFinalScoringError(ValueError):
    """Raised when model scoring cannot be represented as durable evidence."""


@dataclass(frozen=True, slots=True)
class BuyerFinalScore:
    """One normalized final score plus the exact model route used to produce it."""

    total: float
    breakdown: Mapping[str, float]
    rationale: str
    provider: str
    model: str
    profile_id: str = DEFAULT_BUYER_FINAL_SCORING_PROFILE_ID
    profile_version: str = DEFAULT_BUYER_FINAL_SCORING_PROFILE_VERSION
    prompt_version: str = "buyer-final-scoring-v1"

    def __post_init__(self) -> None:
        total = _score_value(self.total, "total")
        breakdown = {str(key): _score_value(value, f"breakdown.{key}") for key, value in self.breakdown.items()}
        if not breakdown:
            raise BuyerFinalScoringError("breakdown cannot be empty")
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "breakdown", breakdown)
        for name in ("rationale", "provider", "model", "profile_id", "profile_version", "prompt_version"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))

    def to_payload(self, *, input_context_hash: str, prompt_hash: str) -> dict[str, Any]:
        return {
            "score_kind": "final",
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_context_hash": input_context_hash,
            "prompt_hash": prompt_hash,
            "total": self.total,
            "breakdown": dict(self.breakdown),
            "rationale": self.rationale,
        }


@runtime_checkable
class BuyerFinalScoringGateway(Protocol):
    """A narrow task-routed model gateway with resolvable provider metadata."""

    def generate(self, *, prompt: str, task: str) -> Awaitable[BuyerFinalScore | Mapping[str, Any] | str] | BuyerFinalScore | Mapping[str, Any] | str:
        """Return structured JSON or an already-normalized final score."""


class LLMRouterBuyerFinalScoringGateway:
    """Adapt the application's LLM router without hardcoding a concrete model."""

    def __init__(self, router: Any) -> None:
        if not callable(getattr(router, "generate", None)):
            raise TypeError("router must expose generate")
        self._router = router

    async def generate(self, *, prompt: str, task: str) -> BuyerFinalScore:
        if task != BUYER_FINAL_SCORING_TASK:
            raise BuyerFinalScoringError(f"expected task={BUYER_FINAL_SCORING_TASK}, got {task}")
        response = await self._router.generate(prompt, task=task)
        route_getter = getattr(self._router, "get_last_route", None)
        route = route_getter() if callable(route_getter) else None
        if not isinstance(route, Mapping):
            raise BuyerFinalScoringError("LLM router did not expose resolved route metadata")
        payload = _response_mapping(response)
        payload.setdefault("provider", route.get("provider") or route.get("resolved_provider"))
        payload.setdefault("model", route.get("model") or route.get("resolved_model"))
        return _final_score_from_mapping(payload)


@dataclass(frozen=True, slots=True)
class BuyerFinalScoringResult:
    """Persistable score evidence and reproducible prompt/context hashes."""

    score: BuyerFinalScore
    input_context_hash: str
    prompt_hash: str
    prompt: str

    def to_payload(self) -> dict[str, Any]:
        return self.score.to_payload(input_context_hash=self.input_context_hash, prompt_hash=self.prompt_hash)


async def score_buyer_project_final(
    project: Mapping[str, Any],
    *,
    gateway: BuyerFinalScoringGateway,
    profile_id: str = DEFAULT_BUYER_FINAL_SCORING_PROFILE_ID,
    profile_version: str = DEFAULT_BUYER_FINAL_SCORING_PROFILE_VERSION,
    prompt_version: str = "buyer-final-scoring-v1",
) -> BuyerFinalScoringResult:
    """Score a bounded project snapshot through the configured model task."""

    if not isinstance(project, Mapping):
        raise TypeError("project must be a mapping")
    if not isinstance(gateway, BuyerFinalScoringGateway):
        raise TypeError("gateway must expose generate")
    context = _bounded_project_context(project)
    input_context_hash = _hash_json(context)
    prompt = build_buyer_final_scoring_prompt(
        context,
        profile_id=_required_text(profile_id, "profile_id"),
        profile_version=_required_text(profile_version, "profile_version"),
        prompt_version=_required_text(prompt_version, "prompt_version"),
    )
    value = gateway.generate(prompt=prompt, task=BUYER_FINAL_SCORING_TASK)
    response = await value if inspect.isawaitable(value) else value
    score = response if isinstance(response, BuyerFinalScore) else _final_score_from_mapping(_response_mapping(response))
    if score.profile_id != profile_id or score.profile_version != profile_version or score.prompt_version != prompt_version:
        score = BuyerFinalScore(
            total=score.total,
            breakdown=score.breakdown,
            rationale=score.rationale,
            provider=score.provider,
            model=score.model,
            profile_id=profile_id,
            profile_version=profile_version,
            prompt_version=prompt_version,
        )
    return BuyerFinalScoringResult(
        score=score,
        input_context_hash=input_context_hash,
        prompt_hash=_hash_text(prompt),
        prompt=prompt,
    )


def build_buyer_final_scoring_prompt(
    context: Mapping[str, Any],
    *,
    profile_id: str,
    profile_version: str,
    prompt_version: str,
) -> str:
    """Produce a deterministic structured-output prompt with a bounded context."""

    payload = {
        "prompt_version": prompt_version,
        "profile": {"profile_id": profile_id, "profile_version": profile_version},
        "project": context,
        "response_schema": {
            "total": "number from 0 to 100",
            "breakdown": {"fit": "0..100", "risk": "0..100", "value": "0..100"},
            "rationale": "concise evidence-based explanation",
        },
    }
    return (
        "Evaluate this Buyer Search project for a service provider. Return only a JSON object matching response_schema. "
        "Do not invent remote facts or reveal secrets.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def _bounded_project_context(project: Mapping[str, Any]) -> dict[str, Any]:
    attachments = project.get("attachments")
    attachment_manifest = [
        {
            "filename": item.get("filename"),
            "detected_type": item.get("detected_type"),
            "state": item.get("state"),
            "size_bytes": item.get("size_bytes"),
        }
        for item in attachments or ()
        if isinstance(item, Mapping)
    ][:25]
    return {
        "project_id": project.get("project_id"),
        "remote_project_id": project.get("remote_project_id"),
        "title": str(project.get("title") or "")[:2_000],
        "description": str(project.get("description") or project.get("description_excerpt") or "")[:12_000],
        "budget_min": project.get("budget_min"),
        "budget_max": project.get("budget_max"),
        "offers": project.get("offers"),
        "views": project.get("views"),
        "age_seconds": project.get("age_seconds"),
        "category_id": project.get("category_id"),
        "buyer_hired_percent": project.get("buyer_hired_percent"),
        "matched_queries": [str(value)[:500] for value in project.get("matched_queries") or ()][:50],
        "attachments": attachment_manifest,
    }


def _response_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise BuyerFinalScoringError("model response must be JSON text or a mapping")
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuyerFinalScoringError("model response must be a JSON object") from exc
    if not isinstance(parsed, Mapping):
        raise BuyerFinalScoringError("model response must be a JSON object")
    return dict(parsed)


def _final_score_from_mapping(payload: Mapping[str, Any]) -> BuyerFinalScore:
    breakdown_value = payload.get("breakdown")
    if not isinstance(breakdown_value, Mapping):
        raise BuyerFinalScoringError("model response requires a breakdown object")
    return BuyerFinalScore(
        total=payload.get("total"),
        breakdown=breakdown_value,
        rationale=payload.get("rationale"),
        provider=payload.get("provider"),
        model=payload.get("model"),
        profile_id=str(payload.get("profile_id") or DEFAULT_BUYER_FINAL_SCORING_PROFILE_ID),
        profile_version=str(payload.get("profile_version") or DEFAULT_BUYER_FINAL_SCORING_PROFILE_VERSION),
        prompt_version=str(payload.get("prompt_version") or "buyer-final-scoring-v1"),
    )


def _score_value(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise BuyerFinalScoringError(f"{name} must be a number from 0 to 100")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BuyerFinalScoringError(f"{name} must be a number from 0 to 100") from exc
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise BuyerFinalScoringError(f"{name} must be a number from 0 to 100")
    return round(result, 2)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuyerFinalScoringError(f"{name} cannot be blank")
    return value.strip()


def _hash_text(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def _hash_json(value: Mapping[str, Any]) -> str:
    return _hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


__all__ = [
    "BUYER_FINAL_SCORING_TASK",
    "BuyerFinalScore",
    "BuyerFinalScoringError",
    "BuyerFinalScoringGateway",
    "BuyerFinalScoringResult",
    "LLMRouterBuyerFinalScoringGateway",
    "build_buyer_final_scoring_prompt",
    "score_buyer_project_final",
]
