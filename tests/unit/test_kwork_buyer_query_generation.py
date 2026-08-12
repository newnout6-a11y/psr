from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.models import BuyerQueryOrigin, BuyerRunMode
from src.platforms.kwork_buyer.query_generation import (
    BuyerQueryGenerationInput,
    generate_buyer_query_candidates,
    replace_legacy_category_fallback_text,
)


def test_manual_generation_preserves_operator_queries_for_planner_collision_reporting() -> None:
    result = generate_buyer_query_candidates(
        BuyerQueryGenerationInput(
            mode=BuyerRunMode.MANUAL,
            supplied_queries=("CRM integration", " crm   integration ", "Analytics dashboard"),
            minimum_count=10,
        )
    )

    assert result.generator == "operator"
    assert not result.used_fallback
    assert [candidate.text for candidate in result.candidates] == ["CRM integration", "crm   integration", "Analytics dashboard"]
    assert all(candidate.origin is BuyerQueryOrigin.MANUAL for candidate in result.candidates)


def test_brief_generation_builds_the_requested_fallback_capacity() -> None:
    result = generate_buyer_query_candidates(
        BuyerQueryGenerationInput(
            mode=BuyerRunMode.BRIEF,
            brief="Telegram bot for sales",
            category_id=17,
            category_path=(2, 17),
            filters={"price_to": 100_000},
            minimum_count=6,
        )
    )

    assert result.generator == "deterministic_fallback"
    assert result.used_fallback
    assert len(result.candidates) == 6
    assert len({candidate.text.casefold() for candidate in result.candidates}) == 6
    assert all(candidate.category_id == 17 for candidate in result.candidates)
    assert all(candidate.category_path == (2, 17) for candidate in result.candidates)
    assert all(candidate.filters == {"price_to": 100_000} for candidate in result.candidates)


def test_category_generation_uses_the_selected_rubric_name_instead_of_a_technical_id() -> None:
    result = generate_buyer_query_candidates(
        BuyerQueryGenerationInput(
            mode=BuyerRunMode.CATEGORY,
            category_id=80,
            category_name="Десктоп программирование",
            category_path=(11, 80),
            minimum_count=4,
        )
    )

    assert [candidate.text for candidate in result.candidates] == [
        "Десктоп программирование",
        "заказ Десктоп программирование",
        "услуги Десктоп программирование",
        "Десктоп программирование разработка",
    ]
    assert all("category 80" not in candidate.text.casefold() for candidate in result.candidates)


def test_category_fallback_supplies_a_second_non_repeating_query_wave() -> None:
    request = BuyerQueryGenerationInput(
        mode=BuyerRunMode.CATEGORY,
        category_id=80,
        category_name="Desktop programming",
        category_path=(11, 80),
        minimum_count=30,
    )
    first_wave = generate_buyer_query_candidates(request)
    second_wave = generate_buyer_query_candidates(
        BuyerQueryGenerationInput(
            mode=request.mode,
            category_id=request.category_id,
            category_name=request.category_name,
            category_path=request.category_path,
            minimum_count=30,
            excluded_queries=tuple(candidate.text for candidate in first_wave.candidates),
        )
    )

    assert len(first_wave.candidates) == 30
    assert len(second_wave.candidates) == 30
    assert {candidate.text.casefold() for candidate in first_wave.candidates}.isdisjoint(
        candidate.text.casefold() for candidate in second_wave.candidates
    )


def test_legacy_category_templates_are_repaired_without_touching_other_queries() -> None:
    assert replace_legacy_category_fallback_text(
        "need category 80",
        category_id=80,
        category_name="Десктоп программирование",
    ) == "нужен Десктоп программирование"
    assert replace_legacy_category_fallback_text(
        "операторский запрос",
        category_id=80,
        category_name="Десктоп программирование",
    ) is None


def test_invalid_query_origin_is_rejected() -> None:
    request = BuyerQueryGenerationInput(
        mode=BuyerRunMode.HYBRID,
        supplied_queries=({"text": "logo", "origin": "unknown"},),
    )

    with pytest.raises(ValueError, match="origin"):
        generate_buyer_query_candidates(request)
