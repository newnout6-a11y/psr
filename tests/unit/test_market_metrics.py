from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.platforms.kwork_supply import MarketMetricsError, linear_quantile, market_metrics_to_wire, project_market_metrics


def test_linear_quantile_uses_explicit_rank_interpolation_with_decimal_results():
    values = [100, 200, 1000, 5000]

    assert linear_quantile(values, Decimal("0.10")) == Decimal("130.00")
    assert linear_quantile(values, Decimal("0.25")) == Decimal("175.00")
    assert linear_quantile(values, Decimal("0.50")) == Decimal("600.00")
    assert linear_quantile(values, Decimal("0.75")) == Decimal("2000.00")
    assert linear_quantile(values, Decimal("0.90")) == Decimal("3800.00")
    assert linear_quantile([], Decimal("0.50")) is None


def test_project_market_metrics_keeps_observed_and_aggregate_metrics_separate():
    metrics = project_market_metrics(
        [
            {"listing_id": 1, "seller_key": "alice", "price": 100, "shard_id": "shard-b"},
            {"listing_id": 2, "seller_key": "alice", "price": "200", "shard_id": "shard-a"},
            {"listing_id": 3, "seller_key": "bob", "price": 5000, "shard_id": "shard-a"},
            {"listing_id": 4, "seller_key": None, "price": None},
            {"listing_id": 5, "seller_key": "carol", "price": "not-a-price", "shard_id": "shard-b"},
            {"listing_id": 6, "seller_key": "dave", "price": 0},
        ],
        aggregate_scope_total=1000,
        cheap_threshold=200,
    )

    assert metrics["aggregate_scope"] == {
        "label": "reported aggregate scope volume; not observed card coverage",
        "reported_listing_count": 1000,
    }
    assert metrics["observed_cards"] == {
        "label": "observed normalized listing records",
        "observed_listing_count": 6,
        "observed_share_of_reported_aggregate": Decimal("0.006000"),
    }

    prices = metrics["price_distribution"]
    assert prices["price_count"] == 3
    assert prices["missing_price_count"] == 1
    assert prices["invalid_price_count"] == 2
    assert prices["p10"] == Decimal("120.00")
    assert prices["p50"] == Decimal("200.00")
    assert prices["p90"] == Decimal("4040.00")
    assert prices["cheap_count"] == 2
    assert prices["cheap_share_of_priced_cards"] == Decimal("0.666667")

    sellers = metrics["seller_repetition_in_observed_cards"]
    assert sellers["unique_seller_count"] == 4
    assert sellers["repeated_card_count"] == 2
    assert sellers["repeat_share_of_observed_cards"] == Decimal("0.333333")
    assert sellers["observed_seller_hhi"] == Decimal("0.280000")
    assert sellers["top_repeated_sellers"] == [{"seller_key": "alice", "observed_card_count": 2}]
    assert "not a market-concentration estimate" in sellers["label"]

    shards = metrics["price_distribution_by_shard"]
    assert shards["unattributed_listing_count"] == 2
    assert [row["shard_id"] for row in shards["shards"]] == ["shard-a", "shard-b"]
    assert shards["shards"][0]["price_distribution"]["p50"] == Decimal("2600.00")


def test_project_market_metrics_is_order_independent_and_has_no_float_outputs():
    listings = [
        {"listing_id": 1, "seller_key": "b", "price": 100.0, "shard_id": "s-2"},
        {"listing_id": 2, "seller_key": "a", "price": "200.50", "shard_id": "s-1"},
        {"listing_id": 3, "seller_key": "b", "price": Decimal("300"), "shard_id": "s-2"},
        {"listing_id": 4, "seller_key": "a", "price": None},
    ]

    forward = project_market_metrics(listings, aggregate_scope_total="12", cheap_threshold="200.50")
    backward = project_market_metrics(reversed(listings), aggregate_scope_total=12, cheap_threshold=Decimal("200.50"))

    assert forward == backward
    assert _contains_float(forward) is False


def test_metrics_reject_invalid_public_numeric_contracts():
    with pytest.raises(MarketMetricsError, match="between 0 and 1"):
        linear_quantile([1, 2], "1.01")
    with pytest.raises(MarketMetricsError, match="non-negative integer"):
        project_market_metrics([], aggregate_scope_total="2.5")
    with pytest.raises(MarketMetricsError, match="cheap_threshold must be positive"):
        project_market_metrics([], cheap_threshold=0)
    with pytest.raises(TypeError, match="listing at index 0"):
        project_market_metrics(["not-a-mapping"])


def test_metrics_wire_values_keep_decimal_precision_without_float_conversion():
    wire = market_metrics_to_wire({"price": Decimal("200.50"), "nested": [Decimal("0.333333")]})

    assert wire == {"price": "200.50", "nested": ["0.333333"]}


def _contains_float(value: Any) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_contains_float(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_float(item) for item in value)
    return False
