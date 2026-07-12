from __future__ import annotations

from decimal import Decimal

import pytest

from src.platforms.kwork_supply import (
    EnrichmentPolicy,
    EnrichmentSelectionError,
    select_enrichment_candidates,
    select_enrichment_sample,
)


def _listings() -> list[dict[str, object]]:
    return [
        {"listing_id": 1, "listing_key": "kwork:1", "price": 10, "seller_key": "alice", "shard_id": "a"},
        {"listing_id": 2, "listing_key": "kwork:2", "price": 20, "seller_key": "alice", "shard_id": "b"},
        {"listing_id": 3, "listing_key": "kwork:3", "price": 100, "seller_key": "bob", "shard_id": "a"},
        {"listing_id": 4, "listing_key": "kwork:4", "price": 200, "seller_key": "carol", "shard_id": "b"},
        {"listing_id": 5, "listing_key": "kwork:5", "price": None, "seller_key": None, "shard_id": "c"},
        {"listing_id": 6, "listing_key": "kwork:6", "price": Decimal("300"), "seller_key": "dave", "shard_id": "c"},
        {"listing_id": 7, "listing_key": "kwork:7", "price": 400, "seller_key": "eve", "shard_id": "c"},
    ]


def test_selector_is_order_independent_stratified_and_returns_evidence():
    policy = EnrichmentPolicy(max_items=5)

    forward = select_enrichment_candidates(_listings(), policy=policy)
    reverse = select_enrichment_sample(reversed(_listings()), policy=policy)

    assert forward == reverse
    assert forward["raw_listing_count"] == 7
    assert forward["selected_count"] == 5
    assert forward["omitted_listing_count"] == 2
    assert forward["selection_is_bounded"] is True
    assert [candidate["listing_id"] for candidate in forward["candidates"]] == [1, 4, 5, 6, 2]
    assert all(candidate["selection_reasons"] for candidate in forward["candidates"])
    assert all(
        {
            "shard_id",
            "price",
            "price_band",
            "seller_key",
            "seller_observed_count",
            "seller_frequency_band",
            "stratum_key",
        }
        <= candidate["evidence"].keys()
        for candidate in forward["candidates"]
    )
    assert forward["strata"]["selected_shard_counts"] == {"a": 1, "b": 2, "c": 2}
    assert forward["strata"]["selected_price_band_counts"] == {"high": 1, "low": 2, "middle": 1, "unknown": 1}
    assert forward["strata"]["selected_seller_frequency_counts"] == {"repeated": 2, "singleton": 2, "unknown": 1}


def test_selector_never_turns_a_ten_thousand_listing_input_into_ten_thousand_enrichments():
    listings = [
        {
            "listing_id": index,
            "price": (index % 1000) + 1,
            "seller_key": f"seller-{index % 47}",
            "shard_id": f"shard-{index % 11}",
        }
        for index in range(10_000)
    ]

    selection = select_enrichment_candidates(listings, policy=EnrichmentPolicy(max_items=12))

    assert selection["raw_listing_count"] == 10_000
    assert selection["selected_count"] == 12
    assert selection["omitted_listing_count"] == 9_988
    assert selection["selection_is_bounded"] is True
    assert len({candidate["listing_identity"] for candidate in selection["candidates"]}) == 12
    assert all("listing" not in candidate for candidate in selection["candidates"])


def test_selector_supports_bounded_override_and_rejects_ambiguous_input():
    policy = EnrichmentPolicy(max_items=2, hard_max_items=4)

    overridden = select_enrichment_candidates(_listings(), policy=policy, max_items=4)
    assert overridden["selected_count"] == 4
    assert overridden["selection_policy"]["max_items"] == 4

    with pytest.raises(EnrichmentSelectionError, match="hard_max_items"):
        select_enrichment_candidates(_listings(), policy=policy, max_items=5)
    with pytest.raises(EnrichmentSelectionError, match="duplicate listing identity"):
        select_enrichment_candidates([{"listing_id": 1}, {"listing_id": 1}])
    with pytest.raises(EnrichmentSelectionError, match="hard_max_items"):
        EnrichmentPolicy(max_items=501)
