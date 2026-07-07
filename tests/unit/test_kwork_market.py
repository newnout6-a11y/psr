from __future__ import annotations

import pytest

from src.platforms.kwork_market import KworkMarketClient, _COMPETITOR_DETAIL_CACHE, _MARKET_METRICS_CACHE


class FakeKworkApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, endpoint: str, **params):
        self.calls.append((method, endpoint, params))
        if endpoint == "categories":
            return {
                "response": [
                    {
                        "id": 1,
                        "name": "Root",
                        "subcategories": [
                            {"id": 2, "name": "Child", "children": [{"id": 3, "name": "Leaf"}]}
                        ],
                    }
                ]
            }
        if endpoint == "categoryAttributes":
            return {
                "response": [
                    {
                        "id": 10,
                        "name": "Type",
                        "required": True,
                        "children": [{"id": 11, "name": "Bots", "kworks_count": 7}],
                    }
                ]
            }
        if endpoint == "kworks":
            return {
                "response": {
                    "kworks_count": 42,
                    "classifiers": [{"id": 100, "name": "Chat bots", "kworks_count": 20}],
                    "kworks": [
                        {
                            "id": 555,
                            "title": "Build a bot",
                            "price": 400,
                            "worker": {"username": "seller", "rating": 5, "reviews_count": 9},
                        }
                    ],
                }
            }
        return {}


@pytest.mark.asyncio
async def test_categories_tree_normalizes_nested_children():
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client.get_categories_tree()

    assert data["categories"][0]["id"] == 1
    assert data["categories"][0]["children"][0]["id"] == 2
    assert data["categories"][0]["children"][0]["children"][0]["id"] == 3


@pytest.mark.asyncio
async def test_category_attributes_are_flattened_with_required_paths():
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client.get_category_attributes(41)

    assert [item["path"] for item in data["flat"]] == ["Type", "Type > Bots"]
    assert data["flat"][0]["required"] is True
    assert data["flat"][1]["kworks_count"] == 7


@pytest.mark.asyncio
async def test_market_metrics_uses_category_id_and_summarizes_competition():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_metrics(category_id=41, include_demand=False)

    assert api.calls[-1] == ("post", "kworks", {"page": 1, "categoryId": 41})
    assert data["kworks_count"] == 42
    assert data["classifiers"][0]["id"] == 100
    assert data["competitors"][0]["title"] == "Build a bot"
    assert data["demand"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_market_metrics_prefers_classifier_id_when_present():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    await client.get_market_metrics(category_id=41, classifier_id=100, include_demand=False)

    assert api.calls[-1] == ("post", "kworks", {"page": 1, "classifierId": 100})


@pytest.mark.asyncio
async def test_market_metrics_uses_selected_attribute_option_as_effective_classifier():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_metrics(
        category_id=41,
        classifier_id=3587,
        include_demand=False,
        attribute_filters={"attribute[208]": 3587, "attribute[3610][]": [3612]},
        attribute_controls=[
            {"name": "attribute[208]", "options": [{"id": 3587, "label": "Чат-боты"}]},
            {"name": "attribute[3610][]", "question": "Платформа", "options": [{"id": 3612, "label": "Telegram"}]},
        ],
    )

    assert api.calls[-1] == (
        "post",
        "kworks",
        {"page": 1, "classifierId": 3612, "attribute[208]": "3587", "attribute[3610][]": ["3612"]},
    )
    assert data["filter_scope"]["effective_classifier_ids"] == [3612]
    assert data["filter_scope"]["selected"][1]["labels"] == ["Telegram"]


@pytest.mark.asyncio
async def test_market_metrics_fans_out_multiselect_classifiers_and_dedupes():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_metrics(
        category_id=41,
        classifier_id=3587,
        include_demand=False,
        attribute_filters={"attribute[3610][]": [3612, 5273361]},
        attribute_controls=[
            {
                "name": "attribute[3610][]",
                "question": "Платформа",
                "options": [{"id": 3612, "label": "Telegram"}, {"id": 5273361, "label": "MAX"}],
            }
        ],
    )

    assert api.calls[-2:] == [
        ("post", "kworks", {"page": 1, "classifierId": 3612, "attribute[3610][]": ["3612", "5273361"]}),
        ("post", "kworks", {"page": 1, "classifierId": 5273361, "attribute[3610][]": ["3612", "5273361"]}),
    ]
    assert data["filter_scope"]["effective_classifier_ids"] == [3612, 5273361]
    assert data["kworks_count"] == 84
    assert len(data["competitors"]) == 1


@pytest.mark.asyncio
async def test_market_metrics_caps_multiselect_fanout(monkeypatch):
    monkeypatch.setenv("KWORK_MARKET_MAX_FANOUT", "2")
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_metrics(
        category_id=41,
        include_demand=False,
        attribute_filters={"attribute[3610][]": [3612, 5273361, 999999]},
        attribute_controls=[
            {
                "name": "attribute[3610][]",
                "options": [
                    {"id": 3612, "label": "Telegram"},
                    {"id": 5273361, "label": "MAX"},
                    {"id": 999999, "label": "Other"},
                ],
            }
        ],
    )

    kwork_calls = [call for call in api.calls if call[1] == "kworks"]
    assert len(kwork_calls) == 2
    assert data["filter_scope"]["effective_classifier_ids"] == [3612, 5273361]


@pytest.mark.asyncio
async def test_market_metrics_cache_reuses_same_live_slice():
    _MARKET_METRICS_CACHE.clear()
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)
    client._owns_api = True

    first = await client.get_market_metrics(category_id=41, include_demand=False, include_competitor_details=False)
    second = await client.get_market_metrics(category_id=41, include_demand=False, include_competitor_details=False)

    assert [call[1] for call in api.calls].count("kworks") == 1
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"


@pytest.mark.asyncio
async def test_competitor_detail_cache_reuses_download(monkeypatch):
    _COMPETITOR_DETAIL_CACHE.clear()
    monkeypatch.setenv("KWORK_COMPETITOR_DETAIL_DELAY", "0")
    calls: list[int] = []

    class DetailClient(KworkMarketClient):
        async def fetch_competitor_detail(self, client, competitor):
            calls.append(competitor["id"])
            return {"detail_status": "ok", "description": f"detail {competitor['id']}"}

    client = DetailClient(api=FakeKworkApi())
    competitors = [
        {"id": 1, "share_url": "https://kwork.ru/kwork/1", "title": "one"},
        {"id": 2, "share_url": "https://kwork.ru/kwork/2", "title": "two"},
    ]

    first = await client.enrich_competitors(competitors, limit=2)
    second = await client.enrich_competitors(competitors, limit=2)

    assert calls == [1, 2]
    assert first[0]["description"] == "detail 1"
    assert second[1]["description"] == "detail 2"
