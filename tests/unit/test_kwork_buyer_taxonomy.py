from __future__ import annotations

from pathlib import Path

import pytest

from src.platforms.kwork_buyer.taxonomy import (
    BuyerTaxonomyConflictError,
    BuyerTaxonomyNotFoundError,
    BuyerTaxonomyService,
)
from src.platforms.kwork_buyer.taxonomy_persistence import SQLiteBuyerTaxonomyStore


def _snapshot_payload(*, snapshot_id: str = "taxonomy-dev-v1") -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "source": "kwork-catalog",
        "source_revision": "2026-07-16T20:00:00Z",
        "captured_at": "2026-07-16T20:00:00Z",
        "provenance": {"account_registration_id": "account-1", "endpoints": ["catalogCategories", "categoryAttributes"]},
        "categories": [
            {"category_id": 11, "name": "Development and IT", "rubric_id": 11},
            {
                "category_id": 38,
                "name": "Site repair and configuration",
                "parent_category_id": 11,
                "rubric_id": 11,
                "classifier_id": 901,
                "category_path": [11, 38],
            },
            {
                "category_id": 39,
                "name": "Website development",
                "parent_category_id": 11,
                "rubric_id": 11,
            },
        ],
        "attributes": [
            {
                "category_id": 38,
                "attribute_id": "cms",
                "name": "CMS",
                "values": [{"value": "wordpress", "name": "WordPress"}, "Tilda"],
            }
        ],
        "filters": [
            {
                "category_id": 38,
                "filter_id": "budget",
                "name": "Budget",
                "options": ["up to 5000", "5000-15000"],
            }
        ],
        "catalog_seeds": [{"category_id": 38, "text": "website repair"}],
        "suggestions": [{"category_id": 38, "text": "fix wordpress website"}],
    }


@pytest.mark.asyncio
async def test_snapshot_context_is_durable_and_category_first(tmp_path: Path) -> None:
    store = SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db")
    service = BuyerTaxonomyService(store)

    recorded = await service.record_snapshot(_snapshot_payload())
    repeated = await service.record_snapshot(_snapshot_payload())
    context = await service.get_category_context(38)

    assert repeated["snapshot_id"] == recorded["snapshot_id"]
    assert recorded["category_count"] == 3
    assert recorded["attribute_count"] == 1
    assert context["generation_defaults"] == {
        "category_id": 38,
        "category_path": [11, 38],
        "taxonomy_snapshot_id": "taxonomy-dev-v1",
        "taxonomy_source": "kwork-catalog",
    }
    assert [item["category_id"] for item in context["ancestors"]] == [11]
    assert context["attributes"][0]["descriptor_id"] == "cms"
    assert context["filters"][0]["descriptor_id"] == "budget"
    vocabulary = {(item["text"], item["source"]) for item in context["vocabulary"]}
    assert ("Site repair and configuration", "category") in vocabulary
    assert ("WordPress", "attribute_value") in vocabulary
    assert ("website repair", "catalog_seed") in vocabulary
    assert ("fix wordpress website", "suggestion") in vocabulary


@pytest.mark.asyncio
async def test_snapshot_id_and_source_revision_are_immutable(tmp_path: Path) -> None:
    service = BuyerTaxonomyService(SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db"))
    await service.record_snapshot(_snapshot_payload())
    changed = _snapshot_payload(snapshot_id="taxonomy-dev-v2")
    changed["categories"] = [
        {"category_id": 11, "name": "Changed category"},
    ]
    changed["attributes"] = []
    changed["filters"] = []
    changed["catalog_seeds"] = []
    changed["suggestions"] = []

    with pytest.raises(BuyerTaxonomyConflictError, match="revision"):
        await service.record_snapshot(changed)


@pytest.mark.asyncio
async def test_taxonomy_requires_a_snapshot_before_planner_context(tmp_path: Path) -> None:
    service = BuyerTaxonomyService(SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db"))

    with pytest.raises(BuyerTaxonomyNotFoundError, match="no durable taxonomy snapshot"):
        await service.get_category_context(38)
