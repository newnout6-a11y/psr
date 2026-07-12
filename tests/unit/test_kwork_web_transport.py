from __future__ import annotations

import pytest

from src.platforms import kwork_market as market_module
from src.platforms.kwork_market import KworkMarketClient, _clean_catalog_alias


def test_clean_catalog_alias_allows_canonical_nested_paths_only():
    assert _clean_catalog_alias("website-development/wordpress") == "website-development/wordpress"

    with pytest.raises(ValueError):
        _clean_catalog_alias("../website-development")
    with pytest.raises(ValueError):
        _clean_catalog_alias("https://kwork.ru/categories/website-development")


@pytest.mark.asyncio
async def test_web_catalog_uses_the_client_specific_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{}'
        text = "{}"

        def json(self):
            return {"success": True, "data": {"stateData": {"viewData": {"filters": {}, "kworks": []}}}}

    class AsyncClientStub:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _endpoint, **_kwargs):
            return Response()

    monkeypatch.setattr(market_module.httpx, "AsyncClient", AsyncClientStub)

    result = await KworkMarketClient(proxy_url="http://127.0.0.1:17992").get_web_catalog_filters("programming")

    assert captured["proxy"] == "http://127.0.0.1:17992"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_web_catalog_can_explicitly_bypass_the_legacy_environment_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{}'
        text = "{}"

        def json(self):
            return {"success": True, "data": {"stateData": {"viewData": {"filters": {}, "kworks": []}}}}

    class AsyncClientStub:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _endpoint, **_kwargs):
            return Response()

    monkeypatch.setattr(market_module.httpx, "AsyncClient", AsyncClientStub)
    monkeypatch.setattr(market_module, "_market_http_proxy_url", lambda **_kwargs: "http://legacy-proxy:9999")

    await KworkMarketClient(use_environment_proxy=False).get_web_catalog_filters("programming")

    assert captured["proxy"] is None


@pytest.mark.asyncio
async def test_catalog_filters_reads_aggregate_metadata_with_category_id():
    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        async def request(self, method: str, endpoint: str, **params):
            self.calls.append((method, endpoint, params))
            return {"response": {"kworksCount": 13000, "priceLimits": {"min": 500, "max": 50000}}}

    api = Api()
    result = await KworkMarketClient(api=api).get_catalog_filters(38)

    assert api.calls == [("post", "catalogFilters", {"categoryId": 38})]
    assert result["category_id"] == 38
    assert result["filters"]["kworksCount"] == 13000
