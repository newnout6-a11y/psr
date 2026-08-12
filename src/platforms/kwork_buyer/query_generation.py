"""Deterministic query candidate generation for the Buyer Search planning step.

The generator is deliberately local and side-effect free.  A stronger LLM-backed
generator can feed additional candidates into the same planner, but it must not
be a prerequisite for creating a durable, operator-reviewable run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import BuyerQueryOrigin, BuyerRunMode
from .query_normalizer import normalize_query_text
from .query_planner import BuyerQueryCandidate


@dataclass(frozen=True, slots=True)
class BuyerQueryGenerationInput:
    """All local inputs used to construct fallback query candidates."""

    mode: BuyerRunMode
    brief: str = ""
    category_id: int | None = None
    category_name: str | None = None
    category_path: tuple[int, ...] = ()
    filters: Mapping[str, Any] | None = None
    supplied_queries: Sequence[str | Mapping[str, Any]] = ()
    excluded_queries: Sequence[str] = ()
    minimum_count: int = 1

    def __post_init__(self) -> None:
        mode = BuyerRunMode(self.mode)
        if self.category_id is not None and (
            isinstance(self.category_id, bool) or not isinstance(self.category_id, int) or self.category_id <= 0
        ):
            raise ValueError("category_id must be a positive integer or None")
        if isinstance(self.minimum_count, bool) or not isinstance(self.minimum_count, int) or self.minimum_count < 1:
            raise ValueError("minimum_count must be a positive integer")
        if self.minimum_count > 120:
            raise ValueError("minimum_count cannot exceed 120")
        path = tuple(self.category_path)
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in path):
            raise ValueError("category_path must contain positive integers")
        if self.category_id is not None and path and path[-1] != self.category_id:
            raise ValueError("category_path must end with category_id")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "brief", str(self.brief or "").strip())
        object.__setattr__(self, "category_name", str(self.category_name or "").strip() or None)
        object.__setattr__(self, "category_path", path)
        object.__setattr__(self, "filters", dict(self.filters or {}))
        object.__setattr__(self, "supplied_queries", tuple(self.supplied_queries))
        raw_excluded = self.excluded_queries
        if not isinstance(raw_excluded, Sequence) or isinstance(raw_excluded, (str, bytes, bytearray)):
            raise TypeError("excluded_queries must be an array of strings")
        excluded = tuple(
            dict.fromkeys(
                normalized
                for item in raw_excluded
                for normalized in (normalize_query_text(str(item)),)
                if normalized
            )
        )
        object.__setattr__(self, "excluded_queries", excluded)


@dataclass(frozen=True, slots=True)
class BuyerQueryGenerationResult:
    """Source-aware candidates that can be passed straight to ``BuyerQueryPlanner``."""

    candidates: tuple[BuyerQueryCandidate, ...]
    generator: str
    used_fallback: bool


def generate_buyer_query_candidates(request: BuyerQueryGenerationInput) -> BuyerQueryGenerationResult:
    """Build reviewable candidates without network or model calls.

    Manual mode deliberately preserves the operator's query set, including
    exact duplicates, so the central planner can emit a collision report.
    Brief/category/hybrid modes make up the requested *unique* minimum with
    transparent template candidates.
    """

    if not isinstance(request, BuyerQueryGenerationInput):
        raise TypeError("request must be BuyerQueryGenerationInput")

    supplied = _supplied_candidates(request)
    if request.mode is BuyerRunMode.MANUAL:
        return BuyerQueryGenerationResult(candidates=supplied, generator="operator", used_fallback=False)

    excluded = set(request.excluded_queries)
    candidates = [
        candidate
        for candidate in supplied
        if normalize_query_text(candidate.text) not in excluded
    ]
    if _unique_candidate_count(candidates) >= request.minimum_count:
        return BuyerQueryGenerationResult(candidates=tuple(candidates), generator="operator", used_fallback=False)

    origin = BuyerQueryOrigin.CATEGORY if request.mode is BuyerRunMode.CATEGORY else BuyerQueryOrigin.BRIEF
    if request.mode is BuyerRunMode.HYBRID:
        origin = BuyerQueryOrigin.HYBRID_SEED
    seed = _seed_text(request)
    seen = {normalize_query_text(candidate.text) for candidate in candidates} | excluded
    for text in _fallback_variations(seed):
        normalized = normalize_query_text(text)
        if not normalized or normalized in seen:
            continue
        candidates.append(
            BuyerQueryCandidate(
                text=text,
                rationale="Резервный запрос сформирован по выбранной рубрике. При необходимости уточните его.",
                origin=origin,
                category_id=request.category_id,
                category_path=request.category_path,
                filters=request.filters,
            )
        )
        seen.add(normalized)
        if _unique_candidate_count(candidates) >= request.minimum_count:
            break
    return BuyerQueryGenerationResult(candidates=tuple(candidates), generator="deterministic_fallback", used_fallback=True)


def _supplied_candidates(request: BuyerQueryGenerationInput) -> tuple[BuyerQueryCandidate, ...]:
    result: list[BuyerQueryCandidate] = []
    default_origin = BuyerQueryOrigin.CATEGORY if request.mode is BuyerRunMode.CATEGORY else BuyerQueryOrigin.BRIEF
    if request.mode is BuyerRunMode.HYBRID:
        default_origin = BuyerQueryOrigin.HYBRID_SEED
    if request.mode is BuyerRunMode.MANUAL:
        default_origin = BuyerQueryOrigin.MANUAL

    for value in request.supplied_queries:
        payload = {"text": value} if isinstance(value, str) else dict(value) if isinstance(value, Mapping) else None
        if payload is None:
            raise TypeError("supplied_queries must contain strings or mappings")
        text = str(payload.get("text") or "").strip()
        normalized = normalize_query_text(text)
        if not normalized:
            continue
        origin_value = payload.get("origin", default_origin)
        try:
            origin = BuyerQueryOrigin(origin_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("query origin is invalid") from exc
        raw_path = payload.get("category_path", request.category_path)
        category_path = tuple(raw_path) if isinstance(raw_path, Sequence) and not isinstance(raw_path, (str, bytes)) else request.category_path
        filters = payload.get("filters", request.filters)
        result.append(
            BuyerQueryCandidate(
                text=text,
                rationale=str(payload.get("rationale") or "Operator supplied candidate"),
                origin=origin,
                category_id=payload.get("category_id", request.category_id),
                category_path=category_path,
                filters=filters if isinstance(filters, Mapping) else request.filters,
                parent_query_id=payload.get("parent_query_id"),
                priority=int(payload.get("priority", 0)),
                semantic_fingerprint=payload.get("semantic_fingerprint"),
                predicted_total=payload.get("predicted_total"),
            )
        )
    return tuple(result)


def _unique_candidate_count(candidates: Sequence[BuyerQueryCandidate]) -> int:
    """Count exact-normalized candidates without hiding collision evidence."""

    return len({normalize_query_text(candidate.text) for candidate in candidates if normalize_query_text(candidate.text)})


def _seed_text(request: BuyerQueryGenerationInput) -> str:
    if request.mode is BuyerRunMode.CATEGORY and request.category_name:
        return request.category_name
    normalized = normalize_query_text(request.brief)
    if normalized:
        return normalized
    if request.category_name:
        return request.category_name
    if request.category_id is not None:
        return f"рубрика {request.category_id}"
    return "заказ на фрилансе"


def _fallback_variations(seed: str) -> tuple[str, ...]:
    """Keep candidate wording explicit so the operator can review every expansion."""

    variants = (
        seed,
        f"заказ {seed}",
        f"услуги {seed}",
        f"{seed} разработка",
        f"{seed} дизайн",
        f"интеграция {seed}",
        f"автоматизация {seed}",
        f"поддержка {seed}",
        f"нужен {seed}",
        f"ищу {seed}",
        f"заказать {seed}",
        f"создать {seed}",
        f"помощь с {seed}",
        f"срочно {seed}",
        f"индивидуальный {seed}",
        f"{seed} с нуля",
        f"{seed} доработка",
        f"{seed} аудит",
        f"консультация по {seed}",
        f"внедрение {seed}",
        f"{seed} настройка",
        f"{seed} сопровождение",
        f"миграция {seed}",
        f"оптимизация {seed}",
        f"прототип {seed}",
        f"обновление {seed}",
        f"исправление {seed}",
        f"проверка {seed}",
        f"{seed} панель управления",
        f"{seed} приложение",
    )
    # The first group preserves historical query ordering.  The continuation
    # group gives automatic target runs several additional, non-repeating
    # waves even when an AI planner is temporarily unavailable.
    continuation_variants = (
        f"{seed} под ключ",
        f"заказать {seed} под ключ",
        f"проект {seed}",
        f"задача {seed}",
        f"срочный заказ {seed}",
        f"долгосрочный проект {seed}",
        f"ищем специалиста {seed}",
        f"требуется специалист {seed}",
        f"нужен исполнитель {seed}",
        f"исполнитель {seed}",
        f"фриланс {seed}",
        f"разработчик {seed}",
        f"дизайнер {seed}",
        f"эксперт {seed}",
        f"поиск исполнителя {seed}",
        f"нужен фрилансер {seed}",
        f"{seed} для бизнеса",
        f"{seed} для стартапа",
        f"{seed} для сайта",
        f"{seed} для интернет-магазина",
        f"{seed} для мобильного приложения",
        f"{seed} для личного кабинета",
        f"{seed} для CRM",
        f"{seed} для маркетплейса",
        f"{seed} для Telegram",
        f"{seed} для 1С",
        f"{seed} для SaaS",
        f"{seed} для B2B",
        f"{seed} по техническому заданию",
        f"{seed} с оплатой по этапам",
        f"{seed} с нуля под ключ",
        f"{seed} доработка проекта",
        f"{seed} редизайн",
        f"{seed} адаптация",
        f"{seed} интерфейс",
        f"{seed} UX",
        f"{seed} UI",
        f"{seed} прототипирование",
        f"{seed} аналитика",
        f"{seed} тестирование",
        f"{seed} улучшение",
        f"{seed} для нового проекта",
        f"{seed} для готового проекта",
        f"{seed} для онлайн-сервиса",
        f"{seed} для корпоративного сайта",
        f"{seed} для автоматизации бизнеса",
        f"разработать {seed}",
        f"сделать {seed}",
        f"создание {seed} для бизнеса",
        f"внедрение {seed} для бизнеса",
        f"настройка {seed} для бизнеса",
        f"консультация и {seed}",
        f"комплексный {seed}",
        f"индивидуальный {seed}",
        f"профессиональный {seed}",
        f"качественный {seed}",
        f"заказ услуги {seed}",
        f"помощь специалиста {seed}",
        f"работа по {seed}",
        f"услуги специалиста {seed}",
    )
    return tuple(dict.fromkeys((*variants, *continuation_variants)))


def replace_legacy_category_fallback_text(text: str, *, category_id: int, category_name: str) -> str | None:
    """Replace an old ``category 123`` template with the saved rubric name.

    Older durable runs used English technical templates even though the selected
    Kwork rubric name was already stored with the run.  Only exact legacy
    templates are eligible, so operator-edited queries remain untouched.
    """

    normalized_name = str(category_name or "").strip()
    if not normalized_name or isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0:
        return None

    legacy_seed = f"category {category_id}"
    legacy_variants = (
        legacy_seed,
        f"{legacy_seed} project",
        f"{legacy_seed} service",
        f"{legacy_seed} development",
        f"{legacy_seed} design",
        f"{legacy_seed} integration",
        f"{legacy_seed} automation",
        f"{legacy_seed} support",
        f"need {legacy_seed}",
        f"looking for {legacy_seed}",
        f"order {legacy_seed}",
        f"create {legacy_seed}",
        f"help with {legacy_seed}",
        f"urgent {legacy_seed}",
        f"custom {legacy_seed}",
        f"{legacy_seed} from scratch",
        f"{legacy_seed} improvement",
        f"{legacy_seed} audit",
        f"{legacy_seed} consultation",
        f"{legacy_seed} implementation",
        f"{legacy_seed} setup",
        f"{legacy_seed} migration",
        f"{legacy_seed} optimization",
        f"{legacy_seed} prototype",
        f"{legacy_seed} maintenance",
        f"{legacy_seed} update",
        f"{legacy_seed} fix",
        f"{legacy_seed} review",
        f"{legacy_seed} dashboard",
        f"{legacy_seed} application",
    )
    normalized_text = normalize_query_text(text)
    for index, legacy_text in enumerate(legacy_variants):
        if normalized_text == normalize_query_text(legacy_text):
            return _fallback_variations(normalized_name)[index]
    return None


__all__ = [
    "BuyerQueryGenerationInput",
    "BuyerQueryGenerationResult",
    "generate_buyer_query_candidates",
    "replace_legacy_category_fallback_text",
]
