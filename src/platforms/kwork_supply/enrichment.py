"""Pure bounded selection of normalized listings for selective enrichment.

The selector works only with supplied listing mappings. It does not access the
network, database, worker runtime, or system clock. A hard policy ceiling
prevents a 10k collection from becoming a 10k-detail-enrichment workload.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, TypeAlias


DecimalLike: TypeAlias = Decimal | int | float | str

_MAX_ENRICHMENT_ITEMS = 500
_MAX_PRICE_BANDS = 10
_UNATTRIBUTED_SHARD = "unattributed"
_UNKNOWN_PRICE_BAND = "unknown"


class EnrichmentSelectionError(ValueError):
    """Raised when a listing or selection policy is not deterministic enough."""


@dataclass(frozen=True, slots=True)
class EnrichmentPolicy:
    """Bounded policy for one deterministic enrichment selection pass."""

    max_items: int = 100
    hard_max_items: int = _MAX_ENRICHMENT_ITEMS
    price_band_count: int = 3

    def __post_init__(self) -> None:
        for field_name in ("max_items", "hard_max_items", "price_band_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise EnrichmentSelectionError(f"{field_name} must be an integer")
        if self.hard_max_items <= 0 or self.hard_max_items > _MAX_ENRICHMENT_ITEMS:
            raise EnrichmentSelectionError(f"hard_max_items must be between 1 and {_MAX_ENRICHMENT_ITEMS}")
        if self.max_items <= 0 or self.max_items > self.hard_max_items:
            raise EnrichmentSelectionError("max_items must be between 1 and hard_max_items")
        if self.price_band_count < 2 or self.price_band_count > _MAX_PRICE_BANDS:
            raise EnrichmentSelectionError(f"price_band_count must be between 2 and {_MAX_PRICE_BANDS}")


@dataclass(frozen=True, slots=True)
class _PreparedListing:
    identity: str
    listing_id: Any
    listing_key: str | None
    shard_id: str
    seller_key: str | None
    seller_observed_count: int
    seller_frequency_band: str
    price: Decimal | None
    price_band: str

    @property
    def stratum_key(self) -> tuple[str, str, str]:
        return (self.shard_id, self.price_band, self.seller_frequency_band)

    @property
    def features(self) -> tuple[tuple[str, str], ...]:
        return (
            ("shard", self.shard_id),
            ("price_band", self.price_band),
            ("seller_frequency", self.seller_frequency_band),
        )


def select_enrichment_candidates(
    listings: Iterable[Mapping[str, Any]],
    *,
    policy: EnrichmentPolicy | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Return a deterministic, bounded, stratified enrichment manifest.

    Each selected candidate has a stable listing reference, selection reasons,
    and compact evidence for its shard, price band, and seller frequency. The
    function never returns more than the configured policy ceiling, even when
    the input contains thousands of normalized listings.
    """

    effective_policy = policy or EnrichmentPolicy()
    if not isinstance(effective_policy, EnrichmentPolicy):
        raise TypeError("policy must be an EnrichmentPolicy")
    limit = _resolve_limit(effective_policy, max_items)
    prepared = _prepare_listings(listings, price_band_count=effective_policy.price_band_count)
    selected = _select_stratified(prepared, limit=limit)
    selected_candidates = [_candidate_manifest(item, reasons) for item, reasons in selected]
    raw_count = len(prepared)
    return {
        "schema_version": 1,
        "selection_policy": {
            "max_items": limit,
            "hard_max_items": effective_policy.hard_max_items,
            "price_band_count": effective_policy.price_band_count,
            "strategy": "deterministic shard-price-seller coverage followed by stratum balance",
        },
        "raw_listing_count": raw_count,
        "eligible_listing_count": raw_count,
        "selected_count": len(selected_candidates),
        "omitted_listing_count": raw_count - len(selected_candidates),
        "selection_is_bounded": raw_count > len(selected_candidates),
        "strata": _strata_manifest(prepared, selected_candidates),
        "candidates": selected_candidates,
    }


def select_enrichment_sample(
    listings: Iterable[Mapping[str, Any]],
    *,
    policy: EnrichmentPolicy | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    """Compatibility name for :func:`select_enrichment_candidates`."""

    return select_enrichment_candidates(listings, policy=policy, max_items=max_items)


def _resolve_limit(policy: EnrichmentPolicy, max_items: int | None) -> int:
    if max_items is None:
        return policy.max_items
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise EnrichmentSelectionError("max_items must be an integer")
    if max_items <= 0 or max_items > policy.hard_max_items:
        raise EnrichmentSelectionError("max_items must be between 1 and policy.hard_max_items")
    return max_items


def _prepare_listings(
    listings: Iterable[Mapping[str, Any]],
    *,
    price_band_count: int,
) -> tuple[_PreparedListing, ...]:
    rows: list[tuple[str, Mapping[str, Any], Any, str | None, str, str | None, Decimal | None]] = []
    seen_identities: set[str] = set()
    for index, listing in enumerate(listings):
        if not isinstance(listing, Mapping):
            raise TypeError(f"listing at index {index} must be a mapping")
        identity, listing_id, listing_key = _listing_identity(listing)
        if identity in seen_identities:
            raise EnrichmentSelectionError(f"duplicate listing identity: {identity}")
        seen_identities.add(identity)
        rows.append(
            (
                identity,
                listing,
                listing_id,
                listing_key,
                _shard_id(listing.get("shard_id")),
                _seller_key(listing),
                _price(listing.get("price")),
            )
        )

    seller_counts = Counter(seller for _, _, _, _, _, seller, _ in rows if seller is not None)
    prepared = [
        _PreparedListing(
            identity=identity,
            listing_id=listing_id,
            listing_key=listing_key,
            shard_id=shard_id,
            seller_key=seller_key,
            seller_observed_count=seller_counts.get(seller_key, 0),
            seller_frequency_band=_seller_frequency_band(seller_key, seller_counts),
            price=price,
            price_band=_UNKNOWN_PRICE_BAND,
        )
        for identity, _, listing_id, listing_key, shard_id, seller_key, price in rows
    ]
    price_bands = _price_bands(prepared, price_band_count=price_band_count)
    return tuple(sorted((replace(item, price_band=price_bands[item.identity]) for item in prepared), key=lambda item: item.identity))


def _select_stratified(
    listings: tuple[_PreparedListing, ...],
    *,
    limit: int,
) -> list[tuple[_PreparedListing, tuple[str, ...]]]:
    if not listings or limit <= 0:
        return []
    remaining = list(listings)
    uncovered_features = {feature for listing in listings for feature in listing.features}
    selected_per_stratum: dict[tuple[str, str, str], int] = defaultdict(int)
    selected: list[tuple[_PreparedListing, tuple[str, ...]]] = []

    while remaining and len(selected) < limit:
        chosen = min(
            remaining,
            key=lambda listing: (
                -sum(feature in uncovered_features for feature in listing.features),
                selected_per_stratum[listing.stratum_key],
                listing.identity,
            ),
        )
        new_features = tuple(feature for feature in chosen.features if feature in uncovered_features)
        reasons = tuple(_coverage_reason(kind) for kind, _ in new_features) or ("stratum_balance",)
        selected.append((chosen, reasons))
        selected_per_stratum[chosen.stratum_key] += 1
        uncovered_features.difference_update(new_features)
        remaining.remove(chosen)
    return selected


def _candidate_manifest(listing: _PreparedListing, reasons: tuple[str, ...]) -> dict[str, Any]:
    return {
        "listing_identity": listing.identity,
        "listing_id": listing.listing_id,
        "listing_key": listing.listing_key,
        "selection_reasons": list(reasons),
        "evidence": {
            "shard_id": listing.shard_id,
            "price": listing.price,
            "price_band": listing.price_band,
            "seller_key": listing.seller_key,
            "seller_observed_count": listing.seller_observed_count,
            "seller_frequency_band": listing.seller_frequency_band,
            "stratum_key": list(listing.stratum_key),
        },
    }


def _strata_manifest(
    listings: tuple[_PreparedListing, ...],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_identities = {str(candidate["listing_identity"]) for candidate in candidates}
    return {
        "input_shard_counts": _count_sorted(listing.shard_id for listing in listings),
        "input_price_band_counts": _count_sorted(listing.price_band for listing in listings),
        "input_seller_frequency_counts": _count_sorted(listing.seller_frequency_band for listing in listings),
        "selected_shard_counts": _count_sorted(
            listing.shard_id for listing in listings if listing.identity in selected_identities
        ),
        "selected_price_band_counts": _count_sorted(
            listing.price_band for listing in listings if listing.identity in selected_identities
        ),
        "selected_seller_frequency_counts": _count_sorted(
            listing.seller_frequency_band for listing in listings if listing.identity in selected_identities
        ),
    }


def _count_sorted(values: Iterable[str]) -> dict[str, int]:
    counts = Counter(values)
    return {key: counts[key] for key in sorted(counts)}


def _price_bands(listings: Iterable[_PreparedListing], *, price_band_count: int) -> dict[str, str]:
    bands = {listing.identity: _UNKNOWN_PRICE_BAND for listing in listings}
    priced = sorted((listing for listing in listings if listing.price is not None), key=lambda listing: (listing.price, listing.identity))
    if not priced:
        return bands
    for index, listing in enumerate(priced):
        band_index = min((index * price_band_count) // len(priced), price_band_count - 1)
        bands[listing.identity] = _price_band_label(band_index, price_band_count)
    return bands


def _price_band_label(index: int, count: int) -> str:
    if count == 3:
        return ("low", "middle", "high")[index]
    return f"band_{index + 1}_of_{count}"


def _coverage_reason(feature_kind: str) -> str:
    return {
        "shard": "shard_coverage",
        "price_band": "price_band_coverage",
        "seller_frequency": "seller_frequency_coverage",
    }[feature_kind]


def _listing_identity(listing: Mapping[str, Any]) -> tuple[str, Any, str | None]:
    listing_id = listing.get("listing_id")
    if listing_id is not None and _text(listing_id) is not None:
        return (f"listing_id:{_text(listing_id)}", listing_id, _text(listing.get("listing_key")))
    listing_key = _text(listing.get("listing_key"))
    if listing_key is not None:
        return (f"listing_key:{listing_key}", listing_key, listing_key)
    for field in ("id", "PID", "share_url", "url"):
        value = _text(listing.get(field))
        if value is not None:
            return (f"{field}:{value}", value, _text(listing.get("listing_key")))
    raise EnrichmentSelectionError("listing requires a stable listing_id, listing_key, id, PID, share_url, or url")


def _shard_id(value: object) -> str:
    return _text(value) or _UNATTRIBUTED_SHARD


def _seller_key(listing: Mapping[str, Any]) -> str | None:
    for field in ("seller_key", "seller_id", "user_id", "seller", "username"):
        value = _text(listing.get(field))
        if value is not None:
            return value
    return None


def _seller_frequency_band(seller_key: str | None, counts: Counter[str]) -> str:
    if seller_key is None:
        return "unknown"
    return "repeated" if counts[seller_key] > 1 else "singleton"


def _price(value: object) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not isfinite(value):
            return None
        value = str(value)
    if not isinstance(value, (Decimal, int, str)):
        return None
    if isinstance(value, str):
        value = value.strip()
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, (int, Decimal)):
        return str(value)
    return None


__all__ = [
    "EnrichmentPolicy",
    "EnrichmentSelectionError",
    "select_enrichment_candidates",
    "select_enrichment_sample",
]
