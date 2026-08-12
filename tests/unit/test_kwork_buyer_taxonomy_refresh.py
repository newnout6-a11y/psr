from __future__ import annotations

from pathlib import Path

import pytest

from src.platforms.kwork_buyer.service import BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadProvenance
from src.platforms.kwork_buyer.taxonomy import BuyerTaxonomyService
from src.platforms.kwork_buyer.taxonomy_persistence import SQLiteBuyerTaxonomyStore
from src.platforms.kwork_buyer.taxonomy_refresh import (
    BuyerTaxonomyReadCapabilities,
    BuyerTaxonomyRefreshCapabilityError,
    BuyerTaxonomyRefreshController,
    BuyerTaxonomyRefreshDisabledError,
    BuyerTaxonomyRefreshError,
)


class _CatalogClient:
    def __init__(self, *, malformed_category: bool = False) -> None:
        self.malformed_category = malformed_category
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.close_calls = 0
        self.send_calls = 0

    async def fetch_catalog_rubrics(self) -> dict[str, object]:
        self.calls.append(("rubrics", {}))
        return {"response": [{"id": 11, "name": "Development"}]}

    async def fetch_catalog_categories(self, *, rubric_id: int) -> dict[str, object]:
        self.calls.append(("categories", {"rubric_id": rubric_id}))
        if self.malformed_category:
            return {"response": [{"id": 38}]}
        return {
            "response": [
                {
                    "id": 38,
                    "name": "Site repair",
                    "classifier_id": 901,
                    "children": [{"id": 79, "name": "Frontend"}],
                }
            ]
        }

    async def fetch_category_attributes(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(("attributes", {"category_id": category_id}))
        return {
            "response": [
                {
                    "id": "cms",
                    "name": "CMS",
                    "children": [{"value": "wordpress", "name": "WordPress"}],
                }
            ]
        }

    async def fetch_catalog_filters(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(("filters", {"category_id": category_id}))
        return {"response": {"filters": [{"id": "budget", "name": "Budget", "options": ["1000-5000"]}]}}

    async def fetch_catalog_main(self) -> dict[str, object]:
        self.calls.append(("catalog_main", {}))
        return {"response": {"popular_categories_block": [{"category_id": 38, "name": "website repair"}]}}

    async def send_message(self) -> None:
        self.send_calls += 1
        raise AssertionError("taxonomy refresh must not send a remote message")

    async def close(self) -> None:
        self.close_calls += 1


class _ReaderFactory:
    def __init__(self, client: _CatalogClient, *, mismatched_account: bool = False) -> None:
        self.client = client
        self.mismatched_account = mismatched_account
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, run_id: str, account_registration_id: str) -> BuyerTaxonomyReadCapabilities:
        self.calls.append((run_id, account_registration_id))
        return BuyerTaxonomyReadCapabilities(
            self.client,
            BuyerReadProvenance(
                worker_id="worker-1",
                account_registration_id="other-account" if self.mismatched_account else account_registration_id,
                transport_id="vpnte-slot-1",
                egress_ip="198.51.100.10",
                source="taxonomy_refresh",
            ),
        )


async def _controller(
    tmp_path: Path,
    *,
    enabled: bool = True,
    malformed_category: bool = False,
    mismatched_account: bool = False,
) -> tuple[BuyerTaxonomyRefreshController, SQLiteBuyerTaxonomyStore, _CatalogClient, _ReaderFactory]:
    store = SQLiteBuyerTaxonomyStore(tmp_path / "buyer-taxonomy-refresh.sqlite3")
    service = BuyerTaxonomyService(store)
    client = _CatalogClient(malformed_category=malformed_category)
    factory = _ReaderFactory(client, mismatched_account=mismatched_account)
    controller = BuyerTaxonomyRefreshController(
        service,
        reader_factory=factory,
        settings=BuyerSearchSettings(taxonomy_refresh=enabled),
    )
    return controller, store, client, factory


@pytest.mark.asyncio
async def test_refresh_persists_a_bounded_account_and_run_scoped_snapshot(tmp_path: Path) -> None:
    controller, store, client, factory = await _controller(tmp_path)

    result = await controller.refresh(
        run_id="run-1",
        account_registration_id="account-1",
        category_ids=[38],
        catalog_seed_limit=10,
    )

    snapshot = result["snapshot"]
    context = await BuyerTaxonomyService(store).get_category_context(
        38,
        snapshot_id=snapshot["snapshot_id"],
    )

    assert result["remote_write"] is False
    assert result["category_count"] == 3
    assert result["attribute_count"] == 1
    assert result["filter_count"] == 1
    assert result["term_count"] >= 1
    assert snapshot["source"] == "kwork-catalog:account-1"
    assert result["provenance"]["run_id"] == "run-1"
    assert result["provenance"]["account_registration_id"] == "account-1"
    assert context["generation_defaults"]["category_path"] == [11, 38]
    assert context["attributes"][0]["descriptor_id"] == "cms"
    assert context["filters"][0]["descriptor_id"] == "budget"
    assert factory.calls == [("run-1", "account-1")]
    assert client.calls == [
        ("rubrics", {}),
        ("categories", {"rubric_id": 11}),
        ("attributes", {"category_id": 38}),
        ("filters", {"category_id": 38}),
        ("catalog_main", {}),
    ]
    assert client.close_calls == 1
    assert client.send_calls == 0


@pytest.mark.asyncio
async def test_refresh_stops_before_borrowing_a_reader_when_the_feature_is_disabled(tmp_path: Path) -> None:
    controller, _store, client, factory = await _controller(tmp_path, enabled=False)

    with pytest.raises(BuyerTaxonomyRefreshDisabledError, match="disabled"):
        await controller.refresh(run_id="run-1", account_registration_id="account-1")

    assert factory.calls == []
    assert client.calls == []
    assert client.close_calls == 0


@pytest.mark.asyncio
async def test_refresh_rejects_mismatched_account_provenance_and_releases_the_reader(tmp_path: Path) -> None:
    controller, _store, client, factory = await _controller(tmp_path, mismatched_account=True)

    with pytest.raises(BuyerTaxonomyRefreshCapabilityError, match="does not match"):
        await controller.refresh(run_id="run-1", account_registration_id="account-1")

    assert factory.calls == [("run-1", "account-1")]
    assert client.calls == []
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_refresh_never_persists_a_partial_snapshot_after_malformed_catalog_data(tmp_path: Path) -> None:
    controller, store, client, _factory = await _controller(tmp_path, malformed_category=True)

    with pytest.raises(BuyerTaxonomyRefreshError, match="category name"):
        await controller.refresh(run_id="run-1", account_registration_id="account-1")

    assert await BuyerTaxonomyService(store).list_snapshots() == ()
    assert client.close_calls == 1

