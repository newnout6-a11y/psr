from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_taxonomy import refresh_router, router
from src.platforms.kwork_buyer.service import BuyerSearchSettings
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadProvenance
from src.platforms.kwork_buyer.taxonomy import BuyerTaxonomyService
from src.platforms.kwork_buyer.taxonomy_persistence import SQLiteBuyerTaxonomyStore
from src.platforms.kwork_buyer.taxonomy_refresh import (
    BuyerTaxonomyReadCapabilities,
    BuyerTaxonomyRefreshController,
)


class _CatalogClient:
    def __init__(self, *, malformed_categories: bool = False) -> None:
        self.calls: list[str] = []
        self.close_calls = 0
        self.malformed_categories = malformed_categories

    async def fetch_catalog_rubrics(self) -> dict[str, object]:
        self.calls.append("rubrics")
        return {"response": [{"id": 11, "name": "Development"}]}

    async def fetch_catalog_categories(self, *, rubric_id: int) -> dict[str, object]:
        self.calls.append(f"categories:{rubric_id}")
        if self.malformed_categories:
            return {"response": [{"id": 38}]}
        return {"response": [{"id": 38, "name": "Site repair"}]}

    async def fetch_category_attributes(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(f"attributes:{category_id}")
        return {"response": []}

    async def fetch_catalog_filters(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(f"filters:{category_id}")
        return {"response": {}}

    async def fetch_catalog_main(self) -> dict[str, object]:
        self.calls.append("catalog_main")
        return {"response": {}}

    async def close(self) -> None:
        self.close_calls += 1


class _ReaderFactory:
    def __init__(self, client: _CatalogClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, run_id: str, account_registration_id: str) -> BuyerTaxonomyReadCapabilities:
        self.calls.append((run_id, account_registration_id))
        return BuyerTaxonomyReadCapabilities(
            self.client,
            BuyerReadProvenance(
                worker_id="worker-1",
                account_registration_id=account_registration_id,
                transport_id="vpnte-slot-1",
                egress_ip="198.51.100.10",
                source="taxonomy_refresh",
            ),
        )


def _client(
    tmp_path,
    *,
    enabled: bool = True,
    malformed_categories: bool = False,
) -> tuple[TestClient, _CatalogClient, _ReaderFactory]:
    store = SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db")
    service = BuyerTaxonomyService(store)
    catalog_client = _CatalogClient(malformed_categories=malformed_categories)
    factory = _ReaderFactory(catalog_client)
    app = FastAPI()
    app.state.buyer_taxonomy = service
    app.state.buyer_taxonomy_refresh = BuyerTaxonomyRefreshController(
        service,
        reader_factory=factory,
        settings=BuyerSearchSettings(taxonomy_refresh=enabled),
    )
    app.include_router(router)
    app.include_router(refresh_router)
    return TestClient(app), catalog_client, factory


def test_account_scoped_taxonomy_refresh_route_persists_the_remote_read_snapshot(tmp_path) -> None:
    client, catalog_client, factory = _client(tmp_path)

    with client:
        response = client.post(
            "/api/kwork/buyer-search/accounts/account-1/taxonomy/refresh",
            json={
                "run_id": "run-1",
                "category_ids": [38],
                "catalog_seed_limit": 0,
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["remote_write"] is False
    assert payload["snapshot"]["source"] == "kwork-catalog:account-1"
    assert payload["provenance"]["run_id"] == "run-1"
    assert factory.calls == [("run-1", "account-1")]
    assert catalog_client.calls == ["rubrics", "categories:11", "attributes:38", "filters:38"]
    assert catalog_client.close_calls == 1


def test_taxonomy_refresh_route_returns_conflict_when_the_feature_is_disabled(tmp_path) -> None:
    client, catalog_client, factory = _client(tmp_path, enabled=False)

    with client:
        response = client.post(
            "/api/kwork/buyer-search/accounts/account-1/taxonomy/refresh",
            json={"run_id": "run-1"},
        )

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]
    assert factory.calls == []
    assert catalog_client.calls == []


def test_taxonomy_refresh_route_requires_the_injected_controller(tmp_path) -> None:
    app = FastAPI()
    app.state.buyer_taxonomy = BuyerTaxonomyService(SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db"))
    app.include_router(router)
    app.include_router(refresh_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/kwork/buyer-search/accounts/account-1/taxonomy/refresh",
            json={"run_id": "run-1"},
        )

    assert response.status_code == 503


def test_taxonomy_refresh_route_maps_malformed_remote_catalog_data_to_unprocessable_content(tmp_path) -> None:
    client, catalog_client, _factory = _client(tmp_path, malformed_categories=True)

    with client:
        response = client.post(
            "/api/kwork/buyer-search/accounts/account-1/taxonomy/refresh",
            json={"run_id": "run-1", "catalog_seed_limit": 0},
        )

    assert response.status_code == 422
    assert "category name" in response.json()["detail"]
    assert catalog_client.close_calls == 1
