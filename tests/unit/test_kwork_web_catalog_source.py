from __future__ import annotations

from collections.abc import Mapping

import pytest

from src.platforms.kwork_supply import (
    BatchRequest,
    BatchState,
    ContractState,
    CursorKind,
    ProtectionStatus,
    fingerprint_for_cards,
)
from src.platforms.kwork_supply.sources.web_catalog import WebCatalogSource


@pytest.fixture
def source() -> WebCatalogSource:
    return WebCatalogSource()


@pytest.fixture
def initial_request(source: WebCatalogSource) -> BatchRequest:
    return source.build_request(alias="website-repair", category_id=38)


@pytest.fixture
def first_web_payload() -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "stateData": {
                "viewData": {
                    "filters": {
                        "activeCategoryId": 38,
                        "kworksCount": 13000,
                    },
                    "kworks": {
                        "total": 1000,
                        "total_found": 13000,
                        "posts": {
                            "data": [
                                {"id": 101, "title": "First offer"},
                                {"id": 102, "title": "Second offer"},
                                {"id": 103, "title": "Third offer"},
                            ]
                        },
                    },
                }
            }
        },
    }


def make_web_payload(
    cards: list[Mapping[str, object]],
    *,
    active_category_id: int = 38,
) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "stateData": {
                "viewData": {
                    "filters": {"activeCategoryId": active_category_id, "kworksCount": 13000},
                    "kworks": {
                        "total": 1000,
                        "total_found": 13000,
                        "posts": {"data": cards},
                    },
                }
            }
        },
    }


def test_parser_extracts_posts_data_and_builds_exclude_ids_cursor(
    source: WebCatalogSource,
    initial_request: BatchRequest,
    first_web_payload: dict[str, object],
):
    result = source.parse_batch(
        initial_request,
        first_web_payload,
        raw_response_ref="fixture://website-repair/first",
        timing_ms=17,
        response_bytes=1234,
    )

    assert [card["id"] for card in result.cards] == [101, 102, 103]
    assert result.actual_item_count == 3
    assert result.source_total == 1000
    assert result.source_total_found == 13000
    assert result.fingerprint == fingerprint_for_cards(result.cards)
    assert result.next_cursor is not None
    assert result.next_cursor.kind is CursorKind.EXCLUDE_IDS
    assert result.next_cursor.exclude_ids == ("101", "102", "103")
    assert result.metadata["active_category_id"] == 38
    assert result.metadata["shape_valid"] is True
    assert result.raw_response_ref == "fixture://website-repair/first"
    assert result.timing_ms == 17
    assert result.response_bytes == 1234


def test_cursor_params_use_exclude_ids_and_one_page(
    source: WebCatalogSource,
    initial_request: BatchRequest,
    first_web_payload: dict[str, object],
):
    result = source.parse_batch(initial_request, first_web_payload)

    assert source.request_params(result.next_cursor) == {
        "excludeIds": "101,102,103",
        "onePage": 1,
    }


def test_partition_filters_are_preserved_without_owning_the_cursor(source: WebCatalogSource):
    request = source.build_request(
        alias="website-repair",
        category_id=38,
        filters={"price_from": 1000, "attribute[10]": "python"},
    )

    assert request.scope["filters"] == {"price_from": 1000, "attribute[10]": "python"}
    batch = source.parse_batch(request, make_web_payload([{"id": 101}]))
    assert batch.next_cursor is not None
    assert source.request_params(
        batch.next_cursor,
        filters=request.scope["filters"],
    ) == {
        "price_from": 1000,
        "attribute[10]": "python",
        "excludeIds": "101",
        "onePage": 1,
    }
    with pytest.raises(ValueError, match="managed by"):
        source.build_request(alias="website-repair", category_id=38, filters={"onePage": 1})


def test_valid_web_batch_requires_matching_active_category(
    source: WebCatalogSource,
    initial_request: BatchRequest,
    first_web_payload: dict[str, object],
):
    result = source.parse_batch(initial_request, first_web_payload)

    verdict = source.validate_batch(initial_request, result, previous=None)

    assert verdict.state is ContractState.ACCEPTED
    assert verdict.novelty is not None
    assert verdict.novelty.new_unique == 3


def test_continuation_accumulates_excluded_ids_and_counts_novelty(
    source: WebCatalogSource,
    initial_request: BatchRequest,
    first_web_payload: dict[str, object],
):
    first_result = source.parse_batch(initial_request, first_web_payload)
    first_verdict = source.validate_batch(initial_request, first_result, previous=None)
    assert first_verdict.accepted is True
    previous = BatchState().accept(first_result)
    continuation_request = source.build_request(
        alias="website-repair",
        category_id=38,
        cursor=first_result.next_cursor,
    )
    second_result = source.parse_batch(
        continuation_request,
        make_web_payload([{"id": 104}, {"id": 105}]),
    )

    verdict = source.validate_batch(continuation_request, second_result, previous)

    assert verdict.state is ContractState.ACCEPTED
    assert verdict.novelty is not None
    assert verdict.novelty.new_unique == 2
    assert second_result.actual_item_count == 2
    assert second_result.next_cursor is not None
    assert second_result.next_cursor.exclude_ids == ("101", "102", "103", "104", "105")


def test_mismatched_active_category_is_not_accepted(
    source: WebCatalogSource,
    initial_request: BatchRequest,
):
    result = source.parse_batch(initial_request, make_web_payload([{"id": 101}], active_category_id=99))

    verdict = source.validate_batch(initial_request, result, previous=None)

    assert verdict.state is ContractState.CONTRACT_VIOLATION
    assert "active_category_mismatch" in verdict.reason_codes


def test_403_is_blocked_not_empty(source: WebCatalogSource, initial_request: BatchRequest):
    result = source.parse_batch(initial_request, {}, status_code=403)

    verdict = source.validate_batch(initial_request, result, previous=None)

    assert result.protection_status is ProtectionStatus.BLOCKED
    assert verdict.state is ContractState.BLOCKED
    assert "protection_signal" in verdict.reason_codes


def test_empty_posts_data_is_exhausted_but_malformed_shape_is_a_violation(
    source: WebCatalogSource,
    initial_request: BatchRequest,
):
    empty_result = source.parse_batch(initial_request, make_web_payload([]))
    malformed_result = source.parse_batch(
        initial_request,
        {
            "success": True,
            "data": {"stateData": {"viewData": {"filters": {"activeCategoryId": 38}, "kworks": {"posts": {}}}}},
        },
    )

    empty_verdict = source.validate_batch(initial_request, empty_result, previous=None)
    malformed_verdict = source.validate_batch(initial_request, malformed_result, previous=None)

    assert empty_result.metadata["shape_valid"] is True
    assert empty_verdict.state is ContractState.EXHAUSTED
    assert malformed_result.metadata["shape_valid"] is False
    assert malformed_verdict.state is ContractState.CONTRACT_VIOLATION
    assert "card_shape_invalid" in malformed_verdict.reason_codes


def test_normalize_projects_real_web_catalog_title_and_seller_fields(source: WebCatalogSource):
    card = source.normalize(
        {
            "PID": 47366251,
            "gtitle": "Webview приложение с оплатой",
            "userId": 1053972,
            "userName": "businessapp-trade",
        }
    )

    assert card["listing_key"] == "47366251"
    assert card["title"] == "Webview приложение с оплатой"
    assert card["seller_id"] == 1053972
    assert card["seller_key"] == "businessapp-trade"
