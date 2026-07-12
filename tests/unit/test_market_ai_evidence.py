from __future__ import annotations

from decimal import Decimal
import json

import pytest

from src.platforms.kwork_supply import (
    AiEvidenceError,
    AiEvidencePolicy,
    EnrichmentPolicy,
    build_ai_evidence_packet,
    build_stratified_ai_evidence,
    select_enrichment_candidates,
)


def _listings() -> list[dict[str, object]]:
    return [
        {
            "listing_id": 1,
            "listing_key": "kwork:1",
            "title": "First strategic landing page",
            "price": Decimal("100"),
            "seller_key": "alice",
            "shard_id": "a",
            "canonical": {"raw_response": "must not be exposed"},
        },
        {
            "listing_id": 2,
            "listing_key": "kwork:2",
            "title": "Second product card",
            "price": 500,
            "seller_key": "alice",
            "shard_id": "b",
            "raw_response_ref": "private/raw/response.gz",
        },
        {
            "listing_id": 3,
            "listing_key": "kwork:3",
            "title": "Third research package",
            "price": 1200,
            "seller_key": "bob",
            "shard_id": "a",
        },
        {
            "listing_id": 4,
            "listing_key": "kwork:4",
            "title": "Fourth implementation package",
            "price": 3000,
            "seller_key": "carol",
            "shard_id": "b",
        },
        {
            "listing_id": 5,
            "listing_key": "kwork:5",
            "title": "Unknown price option",
            "price": None,
            "seller_key": None,
            "shard_id": "c",
        },
    ]


def _metrics() -> dict[str, object]:
    return {
        "aggregate_scope": {
            "label": "reported aggregate scope volume; not observed card coverage",
            "reported_listing_count": 10_000,
        },
        "price_distribution": {"p50": Decimal("750.50"), "price_count": 4},
        "unsafe": {
            "raw_payload": "do not publish",
            "response_body": "also private",
            "safe": "kept",
        },
    }


def test_packet_is_json_safe_deterministic_and_explicitly_sample_based():
    listings = _listings()
    manifest = select_enrichment_candidates(listings, policy=EnrichmentPolicy(max_items=5))
    policy = AiEvidencePolicy(max_evidence_items=3, max_string_chars=20)

    forward = build_ai_evidence_packet(listings, _metrics(), manifest, policy=policy)
    reversed_manifest = {**manifest, "candidates": list(reversed(manifest["candidates"]))}
    reverse = build_stratified_ai_evidence(
        list(reversed(listings)),
        dict(reversed(tuple(_metrics().items()))),
        reversed_manifest,
        policy=policy,
    )
    serialized = json.dumps(forward, ensure_ascii=True, sort_keys=True, allow_nan=False)

    assert forward == reverse
    assert forward["sample_based"] is True
    assert "not market coverage" in forward["coverage_label"]
    assert forward["aggregate_metrics"]["price_distribution"]["p50"] == "750.50"
    assert forward["aggregate_metrics"]["unsafe"] == {"safe": "kept"}
    assert "do not publish" not in serialized
    assert "also private" not in serialized
    assert "private/raw/response.gz" not in serialized
    assert forward["evidence_sample"]["included_evidence_count"] == 3
    assert forward["evidence_sample"]["omitted_evidence_candidate_count"] == 2
    assert forward["evidence_sample"]["sample_is_bounded"] is True
    assert all("canonical" not in row and "raw_response_ref" not in row for row in forward["evidence_sample"]["records"])
    assert all(len(row["title"] or "") <= 20 for row in forward["evidence_sample"]["records"])
    assert all(row["selection_reasons"] for row in forward["evidence_sample"]["records"])
    assert forward["hypothesis_inputs"]["sample_based"] is True
    assert "sample-based hypotheses" in forward["hypothesis_inputs"]["claims_boundary"]


def test_packet_does_not_expand_ten_thousand_listings_or_candidates_into_prompt_evidence():
    listings = [
        {
            "listing_id": index,
            "listing_key": f"kwork:{index}",
            "title": f"Listing {index}",
            "price": (index % 1000) + 1,
            "seller_key": f"seller-{index % 31}",
            "shard_id": f"shard-{index % 7}",
            "raw_payload": "must never enter packet",
        }
        for index in range(10_000)
    ]
    manifest = select_enrichment_candidates(listings, policy=EnrichmentPolicy(max_items=20))

    packet = build_ai_evidence_packet(
        listings,
        {"observed": {"count": 10_000}},
        manifest,
        policy=AiEvidencePolicy(max_evidence_items=5),
    )

    assert packet["evidence_sample"]["observed_listing_count"] == 10_000
    assert packet["evidence_sample"]["enrichment_candidate_count"] == 20
    assert packet["evidence_sample"]["included_evidence_count"] == 5
    assert packet["evidence_sample"]["omitted_evidence_candidate_count"] == 15
    assert len(packet["evidence_sample"]["records"]) == 5
    assert "raw_payload" not in json.dumps(packet, ensure_ascii=True, sort_keys=True)


def test_packet_policy_and_identity_contracts_are_bounded_and_strict():
    with pytest.raises(AiEvidenceError, match="hard_max_evidence_items"):
        AiEvidencePolicy(max_evidence_items=101)
    with pytest.raises(AiEvidenceError, match="policy.hard_max_evidence_items"):
        build_ai_evidence_packet(
            _listings(),
            _metrics(),
            select_enrichment_candidates(_listings()),
            policy=AiEvidencePolicy(max_evidence_items=2, hard_max_evidence_items=3),
            max_items=4,
        )
    with pytest.raises(AiEvidenceError, match="duplicate listing identity"):
        build_ai_evidence_packet(
            [{"listing_id": 1}, {"listing_id": 1}],
            {},
            [],
        )
