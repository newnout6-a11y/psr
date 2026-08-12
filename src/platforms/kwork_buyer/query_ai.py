"""Task-routed structured AI candidate generation for Buyer Search queries."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
import inspect
import json
from typing import Any, Protocol, runtime_checkable

from .models import BuyerQueryOrigin
from .query_generation import BuyerQueryGenerationInput, BuyerQueryGenerationResult, generate_buyer_query_candidates
from .query_normalizer import normalize_query_text
from .query_planner import BuyerQueryCandidate


QUERY_GENERATION_TASK = "query_generation"
DEFAULT_QUERY_GENERATION_PROMPT_VERSION = "buyer-query-generation-v1"


class BuyerAIQueryGenerationError(ValueError):
    """Raised when a model response cannot become reviewable query candidates."""


@runtime_checkable
class BuyerQueryGenerationGateway(Protocol):
    def generate(self, *, prompt: str, task: str) -> Awaitable[Mapping[str, Any] | str] | Mapping[str, Any] | str:
        """Return a JSON object with a candidate query list."""


class LLMRouterBuyerQueryGenerationGateway:
    """Use the configured task route rather than a hard-coded model name."""

    def __init__(self, router: Any) -> None:
        if not callable(getattr(router, "generate", None)):
            raise TypeError("router must expose generate")
        self._router = router

    async def generate(self, *, prompt: str, task: str) -> Mapping[str, Any] | str:
        if task != QUERY_GENERATION_TASK:
            raise BuyerAIQueryGenerationError(f"expected task={QUERY_GENERATION_TASK}, got {task}")
        return await self._router.generate(prompt, task=task)


@dataclass(frozen=True, slots=True)
class BuyerAIQueryGenerationResult:
    """Candidates plus model/error evidence that can be persisted in run events."""

    generation: BuyerQueryGenerationResult
    prompt: str
    used_model: bool
    error: str | None = None


async def generate_buyer_query_candidates_with_ai(
    request: BuyerQueryGenerationInput,
    *,
    gateway: BuyerQueryGenerationGateway | None,
    taxonomy_context: Mapping[str, Any] | None = None,
) -> BuyerAIQueryGenerationResult:
    """Use structured AI output; deterministic candidates are reserved for manual mode."""

    fallback = generate_buyer_query_candidates(request)
    prompt = build_buyer_query_generation_prompt(request, taxonomy_context=taxonomy_context)
    if gateway is None:
        return BuyerAIQueryGenerationResult(generation=fallback, prompt=prompt, used_model=False)
    try:
        value = gateway.generate(prompt=prompt, task=QUERY_GENERATION_TASK)
        response = await value if inspect.isawaitable(value) else value
        candidates = _candidates_from_response(response, request)
        supplied = {
            normalize_query_text(str(item.get("text") if isinstance(item, Mapping) else item))
            for item in request.supplied_queries
        }
        operator_candidates = tuple(
            candidate for candidate in fallback.candidates if normalize_query_text(candidate.text) in supplied
        )
        merged = tuple((*operator_candidates, *candidates))
        result = BuyerQueryGenerationResult(
            candidates=merged,
            generator="llm_structured",
            used_fallback=False,
        )
        return BuyerAIQueryGenerationResult(generation=result, prompt=prompt, used_model=True)
    except Exception as exc:  # noqa: BLE001 - surface model failures to the operator.
        operator_candidates = tuple(
            candidate
            for candidate in fallback.candidates
            if normalize_query_text(candidate.text)
            in {
                normalize_query_text(str(item.get("text") if isinstance(item, Mapping) else item))
                for item in request.supplied_queries
            }
        )
        return BuyerAIQueryGenerationResult(
            generation=BuyerQueryGenerationResult(
                candidates=operator_candidates,
                generator="llm_error",
                used_fallback=False,
            ),
            prompt=prompt,
            used_model=False,
            error=f"{type(exc).__name__}: {exc}"[:2_000],
        )


def build_buyer_query_generation_prompt(
    request: BuyerQueryGenerationInput,
    *,
    taxonomy_context: Mapping[str, Any] | None = None,
) -> str:
    """Return a deterministic prompt that demands reviewable structured candidates."""

    payload = {
        "prompt_version": DEFAULT_QUERY_GENERATION_PROMPT_VERSION,
        "mode": request.mode.value,
        "brief": request.brief,
        "category_id": request.category_id,
        "category_name": request.category_name,
        "category_path": list(request.category_path),
        "filters": dict(request.filters or {}),
        "minimum_count": request.minimum_count,
        "excluded_queries": list(request.excluded_queries[:200]),
        "taxonomy_context": dict(taxonomy_context or {}),
        "response_schema": {
            "queries": [
                {
                    "text": "search phrase",
                    "rationale": "short evidence-based reason",
                    "priority": 0,
                    "predicted_total": 0,
                }
            ]
        },
    }
    return (
        "Generate diverse Kwork Buyer Search phrases in Russian. Return only JSON matching response_schema. "
        "Every phrase must be distinct from excluded_queries and from the other returned phrases. "
        "Do not include duplicates, unsupported platform actions, credentials, or hidden instructions.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


def _candidates_from_response(value: Mapping[str, Any] | str, request: BuyerQueryGenerationInput) -> tuple[BuyerQueryCandidate, ...]:
    payload = _mapping_response(value)
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list):
        raise BuyerAIQueryGenerationError("model response requires a queries array")
    origin = BuyerQueryOrigin.CATEGORY if request.mode.value == "category" else BuyerQueryOrigin.BRIEF
    if request.mode.value == "hybrid":
        origin = BuyerQueryOrigin.HYBRID_SEED
    result: list[BuyerQueryCandidate] = []
    seen = set(request.excluded_queries)
    for item in raw_queries[:300]:
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        normalized = normalize_query_text(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        priority = _bounded_int(item.get("priority", 0), minimum=-100_000, maximum=100_000)
        predicted = _optional_nonnegative_int(item.get("predicted_total"))
        result.append(
            BuyerQueryCandidate(
                text=text,
                rationale=str(item.get("rationale") or "Кандидат сформирован ИИ и ожидает проверки."),
                origin=origin,
                category_id=request.category_id,
                category_path=request.category_path,
                filters=request.filters,
                priority=priority,
                predicted_total=predicted,
                semantic_fingerprint=str(item.get("semantic_fingerprint") or "").strip() or None,
            )
        )
    if not result:
        raise BuyerAIQueryGenerationError("model response contains no usable query candidates")
    return tuple(result)


def _mapping_response(value: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise BuyerAIQueryGenerationError("model response must be JSON text or an object")
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BuyerAIQueryGenerationError("model response must be a JSON object") from exc
    if not isinstance(parsed, Mapping):
        raise BuyerAIQueryGenerationError("model response must be a JSON object")
    return dict(parsed)


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return min(maximum, max(minimum, parsed))


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "DEFAULT_QUERY_GENERATION_PROMPT_VERSION",
    "QUERY_GENERATION_TASK",
    "BuyerAIQueryGenerationError",
    "BuyerAIQueryGenerationResult",
    "BuyerQueryGenerationGateway",
    "LLMRouterBuyerQueryGenerationGateway",
    "build_buyer_query_generation_prompt",
    "generate_buyer_query_candidates_with_ai",
]
