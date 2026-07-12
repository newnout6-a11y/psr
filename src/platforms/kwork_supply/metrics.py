"""Deterministic analytics over normalized observed Kwork listings.

This module intentionally has no repository, network, clock, or LLM
dependency. Counts describe the supplied normalized listing records, while a
reported aggregate scope total remains separately labelled so it cannot be
mistaken for observed card coverage.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from math import isfinite
from typing import Any, Literal, TypeAlias


DecimalLike: TypeAlias = Decimal | int | float | str
PriceStatus: TypeAlias = Literal["valid", "missing", "invalid"]

_RATIO_QUANTUM = Decimal("0.000001")
_QUANTILES: tuple[tuple[str, Decimal], ...] = (
    ("p10", Decimal("0.10")),
    ("p25", Decimal("0.25")),
    ("p50", Decimal("0.50")),
    ("p75", Decimal("0.75")),
    ("p90", Decimal("0.90")),
)


class MarketMetricsError(ValueError):
    """Raised when a public metrics input violates its deterministic contract."""


def linear_quantile(values: Iterable[DecimalLike], probability: DecimalLike) -> Decimal | None:
    """Return a quantile using explicit linear interpolation between order ranks.

    The rank formula is ``probability * (n - 1)``. For example, for values
    ``[100, 200, 1000, 5000]`` the P50 lies halfway between 200 and 1000 and
    is therefore exactly ``Decimal("600")``. All calculations use a local
    Decimal context, never binary floating-point arithmetic.
    """

    quantile = _as_decimal(probability, field="probability")
    if quantile < 0 or quantile > 1:
        raise MarketMetricsError("probability must be between 0 and 1")
    ordered = sorted(_as_decimal(value, field="value") for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]

    with localcontext() as context:
        context.prec = 50
        rank = quantile * Decimal(len(ordered) - 1)
        lower_index = int(rank.to_integral_value(rounding=ROUND_FLOOR))
        upper_index = min(lower_index + 1, len(ordered) - 1)
        fraction = rank - Decimal(lower_index)
        lower = ordered[lower_index]
        upper = ordered[upper_index]
        return lower + (upper - lower) * fraction


def project_market_metrics(
    listings: Iterable[Mapping[str, Any]],
    *,
    aggregate_scope_total: DecimalLike | None = None,
    cheap_threshold: DecimalLike | None = None,
) -> dict[str, Any]:
    """Project stable marketplace metrics from normalized listing mappings.

    ``listings`` must contain one normalized record per observed card. The
    projector intentionally does not deduplicate records: global deduplication
    belongs to the durable repository before analytics are invoked. Numeric
    outputs are integers or :class:`~decimal.Decimal`; shares are rounded to
    six decimal places with ``ROUND_HALF_UP`` for a stable wire conversion.
    """

    records = _listing_records(listings)
    aggregate_total = _aggregate_total(aggregate_scope_total)
    threshold = _positive_decimal_or_none(cheap_threshold, field="cheap_threshold")
    price_summary = _price_summary(records, cheap_threshold=threshold)
    seller_summary = _seller_summary(records)
    shard_rows, unattributed_count = _shard_price_rows(records, cheap_threshold=threshold)

    observed_count = len(records)
    return {
        "schema_version": 1,
        "numeric_contract": {
            "price_values": "Decimal",
            "shares": "Decimal rounded to 6 decimal places with ROUND_HALF_UP",
            "quantile_method": "linear interpolation at probability * (n - 1)",
        },
        "aggregate_scope": {
            "label": "reported aggregate scope volume; not observed card coverage",
            "reported_listing_count": aggregate_total,
        },
        "observed_cards": {
            "label": "observed normalized listing records",
            "observed_listing_count": observed_count,
            "observed_share_of_reported_aggregate": _ratio(observed_count, aggregate_total),
        },
        "price_distribution": price_summary,
        "seller_repetition_in_observed_cards": seller_summary,
        "price_distribution_by_shard": {
            "label": "price distribution by explicit shard attribution in observed records",
            "unattributed_listing_count": unattributed_count,
            "shards": shard_rows,
        },
    }


def market_metrics_to_wire(value: Any) -> Any:
    """Convert Decimal-rich metrics to lossless JSON-compatible values.

    Prices and shares are emitted as strings rather than binary floats, so a
    Desktop client can render the exact deterministic result produced here.
    """

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): market_metrics_to_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [market_metrics_to_wire(item) for item in value]
    return value


def _listing_records(listings: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    records: list[Mapping[str, Any]] = []
    for index, listing in enumerate(listings):
        if not isinstance(listing, Mapping):
            raise TypeError(f"listing at index {index} must be a mapping")
        records.append(listing)
    return tuple(records)


def _as_decimal(value: DecimalLike, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise MarketMetricsError(f"{field} must be numeric, not boolean")
    if isinstance(value, float):
        if not isfinite(value):
            raise MarketMetricsError(f"{field} must be finite")
        value = str(value)
    if not isinstance(value, (Decimal, int, str)):
        raise MarketMetricsError(f"{field} must be a Decimal, int, float, or numeric string")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise MarketMetricsError(f"{field} cannot be blank")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketMetricsError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise MarketMetricsError(f"{field} must be finite")
    return result


def _aggregate_total(value: DecimalLike | None) -> int | None:
    if value is None:
        return None
    parsed = _as_decimal(value, field="aggregate_scope_total")
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise MarketMetricsError("aggregate_scope_total must be a non-negative integer")
    return int(parsed)


def _positive_decimal_or_none(value: DecimalLike | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    parsed = _as_decimal(value, field=field)
    if parsed <= 0:
        raise MarketMetricsError(f"{field} must be positive")
    return parsed


def _price_status(value: Any) -> tuple[PriceStatus, Decimal | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "missing", None
    try:
        price = _as_decimal(value, field="price")
    except MarketMetricsError:
        return "invalid", None
    if price <= 0:
        return "invalid", None
    return "valid", price


def _price_summary(
    records: Iterable[Mapping[str, Any]],
    *,
    cheap_threshold: Decimal | None,
) -> dict[str, Any]:
    prices: list[Decimal] = []
    missing_count = 0
    invalid_count = 0
    observed_count = 0
    for record in records:
        observed_count += 1
        price_status, price = _price_status(record.get("price"))
        if price_status == "valid":
            assert price is not None
            prices.append(price)
        elif price_status == "missing":
            missing_count += 1
        else:
            invalid_count += 1

    prices.sort()
    cheap_count = sum(1 for price in prices if cheap_threshold is not None and price <= cheap_threshold)
    quantiles = {name: linear_quantile(prices, probability) for name, probability in _QUANTILES}
    return {
        "label": "price distribution in observed normalized listings",
        "price_count": len(prices),
        "missing_price_count": missing_count,
        "invalid_price_count": invalid_count,
        "without_valid_price_count": missing_count + invalid_count,
        "missing_price_share_of_observed_cards": _ratio(missing_count, observed_count),
        "without_valid_price_share_of_observed_cards": _ratio(missing_count + invalid_count, observed_count),
        "min": prices[0] if prices else None,
        **quantiles,
        "max": prices[-1] if prices else None,
        "cheap_threshold": cheap_threshold,
        "cheap_count": cheap_count if cheap_threshold is not None else None,
        "cheap_share_of_priced_cards": _ratio(cheap_count, len(prices)) if cheap_threshold is not None else None,
    }


def _seller_summary(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records_tuple = tuple(records)
    seller_counts = Counter(
        seller_key
        for record in records_tuple
        if (seller_key := _seller_key(record.get("seller_key"))) is not None
    )
    seller_listing_count = sum(seller_counts.values())
    repeated = [(seller_key, count) for seller_key, count in seller_counts.items() if count > 1]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    repeated_card_count = sum(count for _, count in repeated)
    top_seller_count = max(seller_counts.values(), default=0)
    hhi_numerator = sum(count * count for count in seller_counts.values())
    hhi_denominator = seller_listing_count * seller_listing_count
    return {
        "label": "seller repetition in observed cards; not a market-concentration estimate",
        "observed_listing_count": len(records_tuple),
        "listings_with_seller_count": seller_listing_count,
        "missing_seller_count": len(records_tuple) - seller_listing_count,
        "unique_seller_count": len(seller_counts),
        "repeated_seller_count": len(repeated),
        "repeated_card_count": repeated_card_count,
        "repeat_share_of_observed_cards": _ratio(repeated_card_count, len(records_tuple)),
        "repeat_share_of_listings_with_seller": _ratio(repeated_card_count, seller_listing_count),
        "top_seller_share_of_listings_with_seller": _ratio(top_seller_count, seller_listing_count),
        "observed_seller_hhi": _ratio(hhi_numerator, hhi_denominator),
        "top_repeated_sellers": [
            {"seller_key": seller_key, "observed_card_count": count} for seller_key, count in repeated
        ],
    }


def _seller_key(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, int):
        return str(value)
    return None


def _shard_price_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    cheap_threshold: Decimal | None,
) -> tuple[list[dict[str, Any]], int]:
    by_shard: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    unattributed_count = 0
    for record in records:
        shard_id = _shard_id(record.get("shard_id"))
        if shard_id is None:
            unattributed_count += 1
            continue
        by_shard[shard_id].append(record)
    rows = [
        {
            "shard_id": shard_id,
            "observed_listing_count": len(shard_records),
            "price_distribution": _price_summary(shard_records, cheap_threshold=cheap_threshold),
        }
        for shard_id, shard_records in sorted(by_shard.items(), key=lambda item: item[0])
    ]
    return rows, unattributed_count


def _shard_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _ratio(numerator: int, denominator: int | None) -> Decimal | None:
    if denominator is None or denominator <= 0:
        return None
    if numerator < 0:
        raise MarketMetricsError("ratio numerator cannot be negative")
    with localcontext() as context:
        context.prec = 50
        return (Decimal(numerator) / Decimal(denominator)).quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)
