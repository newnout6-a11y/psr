from __future__ import annotations

from collections.abc import Mapping

import pytest

from src.platforms.kwork_supply import (
    MOBILE_FIRST_PAGE_CAPABILITIES,
    BatchRequest,
    BatchResult,
    BatchState,
    ContractState,
    ProtectionStatus,
    SourceCursor,
    account_novelty,
    fingerprint_for_cards,
    validate_mobile_page,
)


@pytest.fixture
def first_page_cards() -> tuple[Mapping[str, object], ...]:
    return (
        {"id": 11, "title": "First"},
        {"id": 12, "title": "Second"},
        {"PID": 13, "title": "Third"},
    )


@pytest.fixture
def first_page_request() -> BatchRequest:
    return BatchRequest(
        source="mobile_kworks",
        shard_key="category:38",
        cursor=SourceCursor.page_cursor(1),
        requested_page_size=10,
        scope={"category_id": 38},
    )


@pytest.fixture
def first_page_result(first_page_cards: tuple[Mapping[str, object], ...]) -> BatchResult:
    return BatchResult(
        source="mobile_kworks",
        requested_cursor=SourceCursor.page_cursor(1),
        reported_cursor=SourceCursor.page_cursor(1),
        cards=first_page_cards,
        source_total=1000,
        source_total_found=13000,
        raw_response_ref="fixture://mobile/category-38/page-1",
        timing_ms=25,
        response_bytes=512,
        protection_status=ProtectionStatus.OK,
    )


@pytest.fixture
def prior_mobile_state(first_page_result: BatchResult) -> BatchState:
    return BatchState().accept(first_page_result)


def test_fingerprint_is_sorted_and_duplicate_insensitive(first_page_cards: tuple[Mapping[str, object], ...]):
    reordered_with_duplicate = (first_page_cards[2], first_page_cards[0], first_page_cards[1], first_page_cards[1])

    assert fingerprint_for_cards(first_page_cards) == fingerprint_for_cards(reordered_with_duplicate)
    assert len(fingerprint_for_cards(first_page_cards)) == 64


def test_novelty_accounting_preserves_new_ids_and_duplicate_counters():
    cards = (
        {"id": "known"},
        {"id": "new"},
        {"id": "new"},
        {"PID": "other"},
        {"title": "missing id"},
    )

    stats = account_novelty(cards, seen_ids=("known", "old"))

    assert stats.received_count == 5
    assert stats.identified_count == 4
    assert stats.unique_in_batch == 3
    assert stats.new_unique == 2
    assert stats.duplicate_from_previous == 1
    assert stats.duplicate_within_batch == 1
    assert stats.unknown_identity == 1
    assert stats.duplicate_count == 2
    assert stats.all_unique_ids == ("known", "new", "other")
    assert stats.new_unique_ids == ("new", "other")


def test_mobile_first_page_is_accepted(
    first_page_request: BatchRequest,
    first_page_result: BatchResult,
):
    verdict = validate_mobile_page(first_page_request, first_page_result)

    assert verdict.state is ContractState.ACCEPTED
    assert verdict.accepted is True
    assert verdict.novelty is not None
    assert verdict.novelty.new_unique == 3


def test_mobile_page_mismatch_is_a_contract_violation(
    first_page_cards: tuple[Mapping[str, object], ...],
    prior_mobile_state: BatchState,
):
    request = BatchRequest(
        source="mobile_kworks",
        shard_key="category:38",
        cursor=SourceCursor.page_cursor(2),
        requested_page_size=10,
    )
    repeated_first_page = BatchResult(
        source="mobile_kworks",
        requested_cursor=SourceCursor.page_cursor(2),
        reported_cursor=SourceCursor.page_cursor(1),
        cards=first_page_cards,
        protection_status=ProtectionStatus.OK,
    )

    verdict = validate_mobile_page(request, repeated_first_page, prior_mobile_state)

    assert verdict.state is ContractState.CONTRACT_VIOLATION
    assert "reported_cursor_mismatch" in verdict.reason_codes
    assert "zero_novelty" in verdict.reason_codes
    assert "repeated_fingerprint" in verdict.reason_codes
    assert verdict.novelty is not None
    assert verdict.novelty.duplicate_from_previous == 3


def test_mobile_page_two_is_rejected_without_proven_continuation(first_page_cards: tuple[Mapping[str, object], ...]):
    request = BatchRequest(
        source="mobile_kworks",
        shard_key="category:38",
        cursor=SourceCursor.page_cursor(2),
        requested_page_size=10,
    )
    matching_response = BatchResult(
        source="mobile_kworks",
        requested_cursor=SourceCursor.page_cursor(2),
        reported_cursor=SourceCursor.page_cursor(2),
        cards=first_page_cards,
        protection_status=ProtectionStatus.OK,
    )

    verdict = validate_mobile_page(
        request,
        matching_response,
        capabilities=MOBILE_FIRST_PAGE_CAPABILITIES,
    )

    assert verdict.state is ContractState.CONTRACT_VIOLATION
    assert verdict.reason_codes == ("page_continuation_not_supported",)


def test_protection_is_not_treated_as_an_empty_page(first_page_request: BatchRequest):
    blocked_response = BatchResult(
        source="mobile_kworks",
        requested_cursor=SourceCursor.page_cursor(1),
        reported_cursor=SourceCursor.page_cursor(1),
        cards=(),
        protection_status=ProtectionStatus.BLOCKED,
    )

    verdict = validate_mobile_page(first_page_request, blocked_response)

    assert verdict.state is ContractState.BLOCKED
    assert verdict.reason_codes == ("protection_signal",)
