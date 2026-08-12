from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_taxonomy import router
from src.platforms.kwork_buyer.taxonomy import BuyerTaxonomyService
from src.platforms.kwork_buyer.taxonomy_persistence import SQLiteBuyerTaxonomyStore


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    app.state.buyer_taxonomy = BuyerTaxonomyService(SQLiteBuyerTaxonomyStore(tmp_path / "taxonomy.db"))
    app.include_router(router)
    return TestClient(app)


def test_taxonomy_snapshot_and_generation_context_routes(tmp_path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/kwork/buyer-search/taxonomy/snapshots",
            json={
                "snapshot_id": "catalog-v1",
                "source": "kwork-catalog",
                "categories": [
                    {"category_id": 11, "name": "Development"},
                    {"category_id": 38, "name": "Site repair", "parent_category_id": 11},
                ],
                "attributes": [{"category_id": 38, "attribute_id": "cms", "name": "CMS", "values": ["WordPress"]}],
                "catalog_seeds": [{"category_id": 38, "text": "site repair"}],
            },
        )
        assert created.status_code == 201
        assert created.json()["snapshot_id"] == "catalog-v1"

        roots = client.get("/api/kwork/buyer-search/taxonomy/categories", params={"snapshot_id": "catalog-v1"})
        assert roots.status_code == 200
        assert [item["category_id"] for item in roots.json()["items"]] == [11]

        search = client.get(
            "/api/kwork/buyer-search/taxonomy/categories",
            params={"snapshot_id": "catalog-v1", "query": "repair"},
        )
        assert search.status_code == 200
        assert [item["category_id"] for item in search.json()["items"]] == [38]

        context = client.get("/api/kwork/buyer-search/taxonomy/categories/38/generation-context")
        assert context.status_code == 200
        assert context.json()["generation_defaults"]["category_path"] == [11, 38]
        assert {item["text"] for item in context.json()["vocabulary"]} >= {"Site repair", "CMS", "WordPress"}


def test_taxonomy_context_route_returns_not_found_without_snapshot(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/kwork/buyer-search/taxonomy/categories/38/generation-context")

    assert response.status_code == 404
