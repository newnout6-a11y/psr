from __future__ import annotations

import pytest

from src.platforms.kwork_buyer import (
    BuyerOperationKind,
    BuyerOperationState,
    BuyerQuery,
    BuyerQueryOrigin,
    BuyerQueryState,
    BuyerRunState,
    canonical_filter_json,
    deduplicate_queries,
    filter_hash,
    normalize_query_text,
    stable_query_hash,
    stable_query_key,
)


def test_normalize_query_text_normalizes_unicode_case_and_whitespace() -> None:
    assert normalize_query_text("  Ｔｅｌｅｇｒａｍ\u00a0BOT\t") == "telegram bot"


def test_filter_json_and_hash_ignore_mapping_insertion_order() -> None:
    first = {"budget": {"to": 20_000, "from": 1_000}, "categories": [11, 15]}
    second = {"categories": [11, 15], "budget": {"from": 1_000, "to": 20_000}}

    assert canonical_filter_json(first) == '{"budget":{"from":1000,"to":20000},"categories":[11,15]}'
    assert canonical_filter_json(first) == canonical_filter_json(second)
    assert filter_hash(first) == filter_hash(second)


def test_stable_query_identity_uses_normalized_text_category_and_filters() -> None:
    first_key = stable_query_key(" Telegram\u00a0BOT ", category_id=11, filters={"price_to": 2_000})
    second_key = stable_query_key("telegram bot", category_id=11, filters={"price_to": 2_000})

    assert first_key == second_key
    assert stable_query_hash("Telegram bot", category_id=11, filters={"price_to": 2_000}) == stable_query_hash(
        "telegram bot", category_id=11, filters={"price_to": 2_000}
    )
    assert first_key != stable_query_key("telegram bot", category_id=15, filters={"price_to": 2_000})
    assert first_key != stable_query_key("telegram bot", category_id=11, filters={"price_to": 3_000})


def test_deduplicate_queries_preserves_first_seen_display_text_and_order() -> None:
    assert deduplicate_queries([" Telegram Bot ", "telegram   bot", "AI assistant", "ai\u00a0assistant", "  "]) == [
        "Telegram Bot",
        "AI assistant",
    ]


def test_buyer_query_carries_auditable_identity_and_planner_metadata() -> None:
    query = BuyerQuery(
        query_id="query-1",
        run_id="run-1",
        text="  Telegram\u00a0BOT  ",
        origin=BuyerQueryOrigin.AI_EXPANSION,
        rationale="Matches the category brief.",
        category_id=11,
        category_path=(1, 11),
        filters={"price_to": 20_000, "languages": ["ru"]},
        parent_query_id="query-seed",
        priority=10,
        semantic_fingerprint="fp-1",
        predicted_total=42,
        approved=True,
        state=BuyerQueryState.APPROVED,
    )

    assert query.text == "Telegram\u00a0BOT"
    assert query.normalized_text == "telegram bot"
    assert query.filter_json == '{"languages":["ru"],"price_to":20000}'
    assert query.stable_key == (11, "telegram bot", query.filter_hash)
    assert query.stable_hash == stable_query_hash(query.text, category_id=11, filters=query.filters)


def test_buyer_query_rejects_empty_text_and_inconsistent_category_path() -> None:
    with pytest.raises(ValueError, match="text"):
        BuyerQuery(
            query_id="query-1",
            run_id="run-1",
            text=" \t ",
            origin=BuyerQueryOrigin.MANUAL,
            rationale="",
            category_id=11,
        )
    with pytest.raises(ValueError, match="category_path"):
        BuyerQuery(
            query_id="query-1",
            run_id="run-1",
            text="telegram bot",
            origin=BuyerQueryOrigin.MANUAL,
            rationale="",
            category_id=11,
            category_path=(11, 15),
        )


def test_buyer_enums_cover_planned_runtime_lifecycle() -> None:
    assert BuyerRunState.DRAFT.value == "draft"
    assert BuyerRunState.COMPLETED.value == "completed"
    assert BuyerOperationKind.FETCH_BUYER_PROJECTS.value == "fetch_buyer_projects"
    assert BuyerOperationKind.SYNC_BUYER_CONVERSATION.value == "sync_buyer_conversation"
    assert BuyerOperationState.RETRY_WAIT.value == "retry_wait"
