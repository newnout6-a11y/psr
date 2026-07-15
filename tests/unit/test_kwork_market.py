from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.platforms.kwork_market import (
    KworkMarketClient,
    _COMPETITOR_DETAIL_CACHE,
    _MARKET_METRICS_CACHE,
    _PRICE_RULES_CACHE,
    _SELLER_DETAIL_CACHE,
    _clean_text,
    _market_api_retry_attempts,
    build_market_insights,
)


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
                        "subcategories": [{"id": 2, "name": "Child", "children": [{"id": 3, "name": "Leaf"}]}],
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
        if endpoint == "catalogMainv2":
            return {
                "response": {
                    "popular_categories_block": [
                        {"name": "Main bots", "category_id": 41, "classifier_id": 100, "kworks_count": 20}
                    ]
                }
            }
        if endpoint == "catalogRubrics":
            return {"response": [{"id": 7, "name": "Development"}]}
        if endpoint == "catalogCategories":
            return {
                "response": [
                    {
                        "id": 41,
                        "name": "Programming",
                        "kworks_count": 42,
                        "children": [{"id": 46, "name": "Telegram", "kworks_count": 12}],
                    }
                ]
            }
        if endpoint == "userByUsername":
            return {
                "response": {
                    "id": 77,
                    "username": params.get("username"),
                    "rating": 4.9,
                    "reviews_count": 123,
                    "skills": [{"name": "Python"}, {"name": "Telegram"}],
                    "portfolio_list": [{"id": 1}],
                }
            }
        if endpoint == "kworksCategoriesList":
            return {"response": [{"id": 41, "name": "Programming", "kworks_count": 3}]}
        if endpoint == "userKworks":
            return {
                "response": {
                    "kworks": [
                        {
                            "id": 777,
                            "title": "Seller inventory bot",
                            "price": 1000,
                            "worker": {"username": "seller"},
                        }
                    ]
                }
            }
        if endpoint == "portfolioList":
            return {
                "response": {
                    "portfolio": [
                        {
                            "id": 901,
                            "title": "Telegram bot case",
                            "category_id": 41,
                            "category_name": "Programming",
                            "type": "image",
                            "photo": "https://cdn.example/portfolio.jpg",
                            "views": 88,
                            "views_dirty": 90,
                            "comments_count": 2,
                            "images": [{"url": "https://cdn.example/1.jpg"}],
                            "videos": [],
                            "audios": [],
                            "pdf": [],
                        }
                    ],
                    "paging": {"total": 7, "pages": 1},
                }
            }
        if endpoint == "userReviews":
            review_type = params.get("type")
            items = [
                {
                    "id": 1001,
                    "review_display_id": "R-1001",
                    "time_added": "2026-07-08",
                    "text": "Strong delivery",
                    "good": 1,
                    "bad": 0,
                    "kwork": {"id": 777, "title": "Seller inventory bot"},
                }
            ]
            if review_type == "negative":
                items = [
                    {
                        "id": 1002,
                        "review_display_id": "R-1002",
                        "time_added": "2026-07-07",
                        "text": "Late delivery",
                        "good": 0,
                        "bad": 1,
                        "answer": {"text": "Fixed"},
                        "kwork": {"id": 777, "title": "Seller inventory bot"},
                    }
                ]
            return {"response": {"reviews": items}, "paging": {"total": len(items), "pages": 1}}
        if endpoint == "getKworkDetails":
            return {
                "response": {
                    "kwork_title": "Build a bot",
                    "kwork_description": "I will build a Telegram bot",
                    "kwork_instructions": "Send requirements",
                    "unit_and_quantity": "1 bot",
                    "default_kwork_price": 1500,
                    "term": 172800,
                    "short_user_info": {"username": "seller"},
                }
            }
        if endpoint == "getKworkDetailsExtra":
            return {
                "response": {
                    "reviews_count": 121,
                    "goodReviews": 119,
                    "badReviews": 2,
                    "frequently_asked_questions_count": 3,
                    "kwork_ratings": {"communication": 5},
                    "last_reviews": [{"id": 1}],
                    "recommended_kworks": [
                        {
                            "id": 778,
                            "title": "Related bot",
                            "price": 2000,
                            "worker": {"username": "seller2", "rating": 4.8, "reviews_count": 30},
                        }
                    ],
                    "similar_kworks": [],
                    "other_kworks": [],
                }
            }
        return {}


class TimeoutKworkApi(FakeKworkApi):
    async def request(self, method: str, endpoint: str, **params):
        self.calls.append((method, endpoint, params))
        raise RuntimeError("Request POST /kworks failed after 1 attempts: TimeoutError (TimeoutError())")


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


def test_web_catalog_state_summary_handles_dict_kworks_shape():
    payload = {
        "success": True,
        "data": {
            "stateData": {
                "isRedesign": True,
                "viewData": {
                    "filters": {
                        "activeCategoryId": 11,
                        "kworksCount": 38000,
                        "selectedAttributesIds": [3587],
                        "activeCat": {"id": 11, "name": "Programming", "alias": "programming"},
                    },
                    "kworks": {
                        "html": "<div>card html</div>",
                        "items": [{"id": 1, "title": "Bot"}],
                        "paging": {"page": 1},
                    },
                },
            }
        },
    }

    summary = KworkMarketClient.summarize_web_catalog_state("programming", payload)

    assert summary["success"] is True
    assert summary["filters"]["active_category_id"] == 11
    assert summary["filters"]["kworks_count"] == 38000
    assert summary["filters"]["active_category"]["alias"] == "programming"
    assert summary["kworks"]["type"] == "dict"
    assert summary["kworks"]["list_counts"]["items"] == 1
    assert "paging" in summary["kworks"]["dict_keys"]


def test_clean_text_handles_kwork_volume_dict_and_html():
    hour = "\u0447\u0430\u0441 \u0440\u0430\u0431\u043e\u0442\u044b"
    edit = "\u043f\u0440\u0430\u0432\u043a\u0430"
    bitrix = "\u0411\u0438\u0442\u0440\u0438\u043a\u0441"
    maintenance = "\u0434\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0430"
    bad_hour = f"1 {hour}".encode("utf-8").decode("cp1251")
    bad_edit = f"1 {edit}".encode("utf-8").decode("latin1")
    bad_text = f"{bitrix} {maintenance}".encode("utf-8").decode("latin1")

    assert _clean_text({"volume_type_id": 0, "volume_service_in_kwork": bad_hour}) == f"1 {hour}"
    assert (
        _clean_text(f"{{'volume_type_id': 0, 'base_volume': 1, 'volume_service_in_kwork': '{bad_edit}'}}")
        == f"1 {edit}"
    )
    assert _clean_text("{'volume_type_id': 0, 'base_volume': 1, 'volume_service_in_kwork': ''}") == ""
    assert (
        _clean_text(f"<p><strong>{bad_text.split()[0]}</strong> {bad_text.split()[1]}</p>") == f"{bitrix} {maintenance}"
    )
    assert (
        _clean_text(f"&lt;p&gt;&lt;strong&gt;{bad_text.split()[0]}&lt;/strong&gt; {bad_text.split()[1]}&lt;/p&gt;")
        == f"{bitrix} {maintenance}"
    )


def test_build_market_insights_summarizes_competition_for_ui():
    insights = build_market_insights(
        [
            {
                "title": "Р вЂќР С•РЎР‚Р В°Р В±Р С•РЎвЂљР С”Р В° WordPress Р С‘ PHP",
                "description": "<p>Р ВРЎРѓР С—РЎР‚Р В°Р Р†Р В»РЎР‹ Р С•РЎв‚¬Р С‘Р В±Р С”Р С‘ WordPress</p>",
                "service_size": {"volume_service_in_kwork": "1 РЎвЂЎР В°РЎРѓ"},
                "price": 1000,
                "reviews": 150,
                "worker": "seller_a",
            },
            {
                "title": "Р СњР В°РЎРѓРЎвЂљРЎР‚Р С•Р в„–Р С”Р В° Р вЂР С‘РЎвЂљРЎР‚Р С‘Р С”РЎРѓ PHP",
                "description": "Р вЂР С‘РЎвЂљРЎР‚Р С‘Р С”РЎРѓ, CMS, PHP",
                "price": 2000,
                "reviews": 240,
                "worker": "seller_a",
            },
            {
                "title": "Р Р€РЎРѓР С”Р С•РЎР‚Р ВµР Р…Р С‘Р Вµ WordPress",
                "description": "Pagespeed Р С‘ Р С•Р С—РЎвЂљР С‘Р СР С‘Р В·Р В°РЎвЂ Р С‘РЎРЏ",
                "price": 7000,
                "reviews": 40,
                "worker": "seller_b",
            },
        ],
        kworks_count=13626,
        classifiers=[
            {"id": 1271, "name": "Р вЂќР С•РЎР‚Р В°Р В±Р С•РЎвЂљР С”Р В° РЎРѓР В°Р в„–РЎвЂљР В°", "kworks_count": 6905},
            {"id": 1587, "name": "Р Р€РЎРѓР С”Р С•РЎР‚Р ВµР Р…Р С‘Р Вµ РЎРѓР В°Р в„–РЎвЂљР В°", "kworks_count": 790},
        ],
    )

    assert insights["price"] == {"min": 1000, "median": 2000, "max": 7000, "sample_size": 3}
    assert insights["seller_review_strength"]["reviews_100_plus"] == 2
    assert insights["seller_repetition_in_sample"]["repeated_sellers"] == [{"seller": "seller_a", "cards": 2}]
    assert (
        insights["top_classifiers"][0]["name"]
        == "\u0414\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0441\u0430\u0439\u0442\u0430"
    )
    assert any(item["term"] == "wordpress" for item in insights["title_terms"])
    assert any("13626" in item for item in insights["bullets"])
    assert any("В выбранном срезе" in item for item in insights["bullets"])
    assert any("Упакуй доверие" in item for item in insights["recommendations"])
    visible_text = (
        insights["bullets"] + insights["recommendations"] + [item["name"] for item in insights["top_classifiers"]]
    )
    assert not any(any(marker in item for marker in ("Рџ", "Р’", "Ð", "PSC")) for item in visible_text)
    assert insights["recommendations"]
    assert any(
        item["query"] == "\u0414\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0441\u0430\u0439\u0442\u0430"
        for item in insights["search_queries"]
    )
    assert any(item["source"] == "classifier" for item in insights["search_queries"])


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_scopes_demand_queries_to_seed_rubric():
    requests: list[dict] = []

    class DemandClient(KworkMarketClient):
        async def _get_projects_snapshot(self, **kwargs):
            requests.append(kwargs)
            return {"status": "ok", "wants_count": 1, "sample_count": 0, "sample": []}

    client = DemandClient(api=FakeKworkApi())

    await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Доработка сайта", "category_id": 38, "classifier_id": 1271}],
        include_demand=True,
    )

    assert any(item.get("query") == "Доработка сайта" for item in requests)
    assert {item.get("categories") for item in requests} == {"38"}


@pytest.mark.asyncio
async def test_web_catalog_alias_snapshot_throttles_and_summarizes(tmp_path):
    class SnapshotClient(KworkMarketClient):
        async def get_web_catalog_filters(self, alias, **kwargs):
            return {
                "alias": alias,
                "success": alias != "blocked",
                "protection_status": "blocked" if alias == "blocked" else "ok",
                "cookie_count": len(kwargs.get("cookies") or {}),
            }

    client = SnapshotClient(api=FakeKworkApi())

    data = await client.get_web_catalog_alias_snapshot(
        ["programming", "programming", "blocked", "../bad"],
        delay_seconds=0,
        cookies={"slrememberme": "present"},
        write_file=True,
        output_dir=tmp_path,
    )

    assert data["config"]["aliases"] == ["programming", "blocked"]
    assert data["config"]["cookie_count"] == 1
    assert data["aggregate"]["alias_count"] == 2
    assert data["aggregate"]["ok_count"] == 1
    assert data["aggregate"]["blocked_count"] == 1
    assert data["errors"][0]["status"] == "invalid_alias"
    assert data["results"][0]["cookie_count"] == 1
    assert data["file_path"].startswith(str(tmp_path))
    assert (
        json.loads((tmp_path / Path(data["file_path"]).name).read_text(encoding="utf-8"))["aggregate"]["ok_count"] == 1
    )


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
async def test_market_metrics_degrades_when_kworks_times_out():
    api = TimeoutKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_metrics(category_id=41, include_demand=False)

    assert api.calls[-1] == ("post", "kworks", {"page": 1, "categoryId": 41})
    assert data["status"] == "timeout"
    assert data["kworks_count"] == 0
    assert data["competitors"] == []
    assert data["demand"]["status"] == "skipped"
    assert "timed out" in data["detail"]


def test_market_api_retry_attempts_defaults_and_env(monkeypatch):
    monkeypatch.delenv("KWORK_MARKET_API_RETRY_ATTEMPTS", raising=False)
    assert _market_api_retry_attempts() == 2

    monkeypatch.setenv("KWORK_MARKET_API_RETRY_ATTEMPTS", "4")
    assert _market_api_retry_attempts() == 4

    monkeypatch.setenv("KWORK_MARKET_API_RETRY_ATTEMPTS", "bad")
    assert _market_api_retry_attempts() == 2


@pytest.mark.asyncio
async def test_catalog_market_seeds_merge_catalog_main_and_taxonomy():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    seeds = await client.get_catalog_market_seeds(limit=5)

    assert seeds[0]["category_id"] == 41
    assert seeds[0]["source"] == "catalog_taxonomy"
    assert any(seed["category_id"] == 46 and seed["rubric_id"] == 7 for seed in seeds)
    assert ("post", "catalogMainv2", {}) in api.calls
    assert ("post", "catalogRubrics", {}) in api.calls
    assert ("post", "catalogCategories", {"rubricId": 7}) in api.calls


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
            {"name": "attribute[208]", "options": [{"id": 3587, "label": "Р В§Р В°РЎвЂљ-Р В±Р С•РЎвЂљРЎвЂ№"}]},
            {
                "name": "attribute[3610][]",
                "question": "Р СџР В»Р В°РЎвЂљРЎвЂћР С•РЎР‚Р СР В°",
                "options": [{"id": 3612, "label": "Telegram"}],
            },
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
                "question": "Р СџР В»Р В°РЎвЂљРЎвЂћР С•РЎР‚Р СР В°",
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


@pytest.mark.asyncio
async def test_competitor_detail_adds_extra_related_kworks():
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    detail = await client.fetch_competitor_detail(
        None,
        {"id": 555, "share_url": "https://kwork.ru/kwork/555", "title": "Build a bot"},
    )

    assert detail["description"] == "I will build a Telegram bot"
    assert detail["extra"]["status"] == "ok"
    assert detail["extra"]["review_summary"]["reviews_count"] == 121
    assert detail["extra"]["related_kworks"]["recommended"]["items"][0]["title"] == "Related bot"
    assert ("post", "getKworkDetails", {"id": 555}) in api.calls
    assert ("post", "getKworkDetailsExtra", {"id": 555}) in api.calls


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_collects_seed_supply(tmp_path):
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
        include_demand=False,
        write_file=True,
        output_dir=tmp_path,
    )

    assert data["aggregate"]["seed_count"] == 1
    assert data["aggregate"]["cards_seen"] == 1
    assert data["aggregate"]["top_sellers"] == [("seller", 1)]
    assert data["supply"][0]["seed"]["classifier_id"] == 100
    assert data["supply"][0]["kworks_count"] == 42
    assert data["supply"][0]["demand"]["status"] == "skipped"
    assert api.calls[-1] == ("post", "kworks", {"page": 1, "classifierId": 100})
    assert data["file_path"].startswith(str(tmp_path))
    assert data["latest_path"] == str(tmp_path / "latest.json")
    assert data["index_path"] == str(tmp_path / "index.jsonl")

    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    index_lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert latest["file_path"] == data["file_path"]
    assert latest["aggregate"]["cards_seen"] == 1
    assert latest["top_opportunities"][0]["seed_name"] == "Bots"
    assert len(index_lines) == 1
    assert json.loads(index_lines[0])["file_path"] == data["file_path"]


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_builds_market_rankings():
    class DemandClient(KworkMarketClient):
        async def _get_projects_snapshot(self, **kwargs):
            return {"status": "ok", "wants_count": 15, "sample_count": 0, "sample": []}

    client = DemandClient(api=FakeKworkApi())

    data = await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
        include_demand=True,
    )

    ranking = data["market_rankings"][0]
    assert data["aggregate"]["top_opportunities"][0]["seed_name"] == "Bots"
    assert ranking["seed_name"] == "Bots"
    assert ranking["supply_kworks_count"] == 42
    assert ranking["demand_wants_count"] == 15
    assert ranking["demand_per_1000_kworks"] == 357.143
    assert ranking["sample_price_min"] == 400
    assert ranking["sample_price_avg"] == 400
    assert ranking["sample_unique_sellers"] == 1
    assert ranking["signals"]["demand_level"] == "medium"
    assert ranking["signals"]["supply_level"] == "niche"


def test_market_intelligence_history_reads_latest_and_index(tmp_path):
    client = KworkMarketClient(api=FakeKworkApi())
    latest = {
        "generated_at": "2026-07-08T00:00:02Z",
        "file_path": "second.json",
        "aggregate": {"cards_seen": 2},
        "top_opportunities": [{"seed_name": "Two"}],
    }
    first = {"generated_at": "2026-07-08T00:00:01Z", "file_path": "first.json"}
    (tmp_path / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    (tmp_path / "index.jsonl").write_text(
        "\n".join([json.dumps(first), "{bad json", json.dumps(latest)]),
        encoding="utf-8",
    )

    history = client.get_market_intelligence_history(limit=10, output_dir=tmp_path)

    assert history["exists"] is True
    assert history["latest"]["file_path"] == "second.json"
    assert history["entry_count"] == 2
    assert [item["file_path"] for item in history["entries"]] == ["first.json", "second.json"]
    assert history["latest_path"] == str(tmp_path / "latest.json")
    assert history["index_path"] == str(tmp_path / "index.jsonl")


def test_market_project_summary_and_ranking_preserve_demand_signals():
    project = KworkMarketClient._summarize_project(
        {
            "id": 9001,
            "name": "Need a Telegram bot",
            "categoryId": 41,
            "parentCategoryId": 11,
            "possiblePriceLimit": 5000,
            "kwork_count": 1,
            "timeLeft": "2 days",
            "userNeedPortfolio": True,
            "allowHigherPrice": True,
            "user_hired_percent": 80,
            "user": {"id": 42, "username": "buyer", "data": {"wants_hired_percent": 80}},
        }
    )

    assert project["price"] == 5000
    assert project["offers"] == 1
    assert project["user_need_portfolio"] is True
    assert project["allow_higher_price"] is True
    assert project["user_hired_percent"] == 80

    rankings = KworkMarketClient.build_market_rankings(
        [
            {
                "seed": {"name": "Bots", "category_id": 41},
                "kworks_count": 100,
                "cards": [{"id": 1, "price": 1000, "worker": "seller"}],
                "demand": {"status": "ok", "wants_count": 3, "sample": [project]},
            }
        ]
    )

    ranking = rankings[0]
    assert ranking["demand_sample_offers_min"] == 1
    assert ranking["demand_sample_low_offer_count"] == 1
    assert ranking["demand_sample_budget_avg"] == 5000
    assert ranking["demand_sample_portfolio_required_count"] == 1
    assert ranking["demand_sample_higher_price_allowed_count"] == 1
    assert ranking["signals"]["low_offer_demand"] is True
    assert ranking["signals"]["portfolio_heavy_demand"] is True
    assert ranking["signals"]["higher_price_allowed"] is True


@pytest.mark.asyncio
async def test_projects_snapshot_can_enrich_top_wants(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[tuple[str, str, dict]] = []

    class WantApi:
        async def request(self, method, endpoint, **params):
            calls.append((method, endpoint, params))
            return {
                "response": {
                    "id": params["id"],
                    "title": "Detailed want",
                    "description": "Need an MVP",
                    "views": 44,
                    "orders": 3,
                    "views_history": [1, 2, 3],
                }
            }

    class Service:
        async def get_wants_count(self, **kwargs):
            return 2

        async def get_raw_projects(self, **kwargs):
            return (
                [
                    {"id": 9001, "name": "Want one", "kwork_count": 1},
                    {"id": 9002, "name": "Want two", "kwork_count": 5},
                ],
                {"page": 1},
            )

        async def get_api(self):
            return WantApi()

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client._get_projects_snapshot(
        categories="41",
        include_want_details=True,
        want_detail_limit=1,
    )

    assert data["sample"][0]["want_detail"]["views"] == 44
    assert data["sample"][0]["want_detail"]["views_history_count"] == 3
    assert "want_detail" not in data["sample"][1]
    assert calls == [("post", "want", {"use_token": True, "id": 9001})]


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_attaches_price_rules():
    class PriceClient(KworkMarketClient):
        async def get_price_rules(self, category_id, attribute_id=None):
            return {"response": {"prices": {"minPrice": 500, "maxPrice": 70000, "typicalPrice": 1500}}}

    client = PriceClient(api=FakeKworkApi())

    data = await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
        include_demand=False,
        include_price_rules=True,
    )

    assert data["supply"][0]["price_rules"]["min_price"] == 500
    assert data["supply"][0]["price_rules"]["max_price"] == 70000
    assert data["market_rankings"][0]["price_rule_min"] == 500
    assert data["market_rankings"][0]["price_rule_max"] == 70000


@pytest.mark.asyncio
async def test_price_rules_cache_reuses_successful_response(monkeypatch):
    from src.platforms import kwork_market as kwork_market_module

    _PRICE_RULES_CACHE.clear()
    calls: list[dict] = []

    class Response:
        content = b"{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"response": {"prices": {"minPrice": 500, "maxPrice": 70000}}}

    class AsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, path, *, params):
            calls.append({"path": path, "params": params, "proxy": self.kwargs.get("proxy")})
            return Response()

    monkeypatch.setattr(kwork_market_module.httpx, "AsyncClient", AsyncClient)
    client = KworkMarketClient(api=FakeKworkApi(), use_environment_proxy=False)

    first = await client.get_price_rules(25)
    second = await client.get_price_rules(25)

    assert len(calls) == 1
    assert first["status"] == "ok"
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"
    assert second["response"]["prices"]["minPrice"] == 500


@pytest.mark.asyncio
async def test_price_rules_transport_failure_is_non_blocking(monkeypatch):
    from src.platforms import kwork_market as kwork_market_module

    _PRICE_RULES_CACHE.clear()
    monkeypatch.setenv("KWORK_MARKET_API_RETRY_ATTEMPTS", "2")
    calls = 0

    class AsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, path, *, params):
            nonlocal calls
            calls += 1
            request = kwork_market_module.httpx.Request("GET", f"https://kwork.ru{path}")
            raise kwork_market_module.httpx.ReadError("connection closed", request=request)

    monkeypatch.setattr(kwork_market_module.httpx, "AsyncClient", AsyncClient)
    result = await KworkMarketClient(api=FakeKworkApi(), use_environment_proxy=False).get_price_rules(25)

    assert calls == 2
    assert result["success"] is False
    assert result["status"] == "unavailable"
    assert result["prices"] is None
    assert "ReadError" in result["detail"]


@pytest.mark.asyncio
async def test_buyer_scout_without_rubric_or_explicit_probes_does_not_use_default_windows(monkeypatch):
    from src.platforms import kwork as kwork_module

    class Service:
        async def get_raw_projects(self, **kwargs):
            raise AssertionError("no request should be made without a rubric or explicit probe")

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    data = await KworkMarketClient(api=FakeKworkApi()).get_buyer_scout(
        probes=[],
        include_project_details=False,
        include_want_details=False,
        include_query_suggestions=False,
    )

    assert data["probes"] == []
    assert data["aggregate"]["unique_projects"] == 0


def test_buyer_project_score_penalizes_budget_above_cap():
    capped = KworkMarketClient.score_buyer_project({"offers": 0, "price": 30000}, budget_max=5000)
    reachable = KworkMarketClient.score_buyer_project({"offers": 0, "price": 5000}, budget_max=5000)

    assert "above budget cap" in capped["score_reasons"]
    assert capped["score"] < reachable["score"]


@pytest.mark.asyncio
async def test_buyer_query_suggestions_deduplicate_probe_queries(monkeypatch):
    client = KworkMarketClient(api=FakeKworkApi())
    calls: list[str] = []

    async def fake_fetch(query, **kwargs):
        calls.append(query)
        return {"status": "ok", "query": query, "suggestions": [{"suggestion": f"{query} task"}]}

    monkeypatch.setattr(client, "fetch_want_search_suggestions", fake_fetch)

    suggestions = await client.build_buyer_query_suggestions(
        [
            {"query": "telegram"},
            {"query": "telegram"},
            {"query": "python"},
            {"query": ""},
        ],
        max_queries=5,
        suggestion_limit=5,
    )

    assert calls == ["telegram", "python"]
    assert suggestions[0]["suggestions"][0]["suggestion"] == "telegram task"


def test_buyer_probe_filters_keep_only_mobile_api_params():
    filters = KworkMarketClient._buyer_probe_filters(
        {
            "price_from": 30000,
            "kworks_filter_to": 5,
            "hiring_from": 30,
            "keyword": "telegram",
            "kworks_filters": 0,
            "prices_filters": 4,
            "sort": "date",
        }
    )

    assert filters == {
        "price_from": 30000,
        "kworks_filter_to": 5,
        "hiring_from": 30,
    }


def test_market_project_summary_cleans_title_html_entities():
    project = KworkMarketClient._summarize_project(
        {
            "id": 3202311,
            "title": "Need 5&ndash;6 Telegram <b>Stories</b> &amp; bot",
            "kwork_count": 0,
        }
    )

    assert project["title"] == "Need 5\u20136 Telegram Stories & bot"


def test_market_project_summary_repairs_mojibake_text():
    good_title = "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0442\u0435\u043b\u0435\u0433\u0440\u0430\u043c \u0431\u043e\u0442\u0430"
    good_description = "\u0431\u043e\u0442 \u043d\u0430 python"
    cp1251_as_latin1_title = "\u0421\u043a\u0440\u0438\u043f\u0442 \u043d\u0430 n8n".encode("cp1251").decode("latin1")
    project = KworkMarketClient._summarize_project(
        {
            "id": 3202312,
            "title": good_title.encode("utf-8").decode("cp1251"),
            "description": f"<p>{good_description.encode('utf-8').decode('cp1251')}</p>",
            "kwork_count": 0,
        }
    )

    assert project["title"] == good_title
    assert _clean_text(f"<p>{good_description.encode('utf-8').decode('cp1251')}</p>") == good_description
    assert _clean_text(cp1251_as_latin1_title) == "\u0421\u043a\u0440\u0438\u043f\u0442 \u043d\u0430 n8n"


def test_market_project_summary_preserves_buyer_identity_history():
    project = KworkMarketClient._summarize_project(
        {
            "id": 3202311,
            "title": "Need Telegram stories",
            "username": "Salvador1w1332",
            "user_id": 13492958,
            "user_projects_count": 139,
            "user_active_projects_count": 4,
            "user_hired_percent": 37,
            "has_offer": False,
            "is_viewed": True,
            "kwork_count": 0,
            "possible_price_limit": 90000,
        }
    )

    assert project["username"] == "Salvador1w1332"
    assert project["user_id"] == 13492958
    assert project["user_projects_count"] == 139
    assert project["user_active_projects_count"] == 4
    assert project["user_hired_percent"] == 37
    assert project["has_offer"] is False
    assert project["is_viewed"] is True
    assert project["buyer_projects_url"] == "https://kwork.ru/projects/list/Salvador1w1332"
    assert project["user"]["username"] == "Salvador1w1332"


@pytest.mark.asyncio
async def test_buyer_scout_ranks_low_offer_projects_and_attaches_details(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[tuple[str, dict]] = []

    class ScoutApi:
        async def request(self, method, endpoint, **params):
            calls.append((endpoint, params))
            if endpoint == "want":
                return {
                    "response": [
                        {
                            "id": params["id"],
                            "title": "Want detail",
                            "views": 10,
                            "orders": 1,
                            "views_history": [{"count_view": 3}],
                        }
                    ]
                }
            if endpoint == "project":
                return {
                    "response": {
                        "id": params["id"],
                        "title": "Project detail",
                        "description": "Detailed project",
                        "offers": 0,
                        "price": 30000,
                    }
                }
            return {"response": {}}

    class Service:
        async def get_wants_count(self, **kwargs):
            return 2

        async def get_raw_projects(self, **kwargs):
            calls.append(("projects", kwargs))
            return (
                [
                    {
                        "id": 1,
                        "title": "Zero offer Telegram",
                        "description": "Need a detailed Telegram automation brief with enough context.",
                        "price": 30000,
                        "possible_price_limit": 90000,
                        "offers": 0,
                        "username": "buyer_one",
                        "user_id": 42,
                        "user_projects_count": 6,
                        "allow_higher_price": True,
                        "user_hired_percent": 50,
                    },
                    {
                        "id": 2,
                        "title": "Crowded",
                        "description": "Small task",
                        "price": 500,
                        "offers": 40,
                    },
                ],
                {"paging": {"page": 1}},
            )

        async def get_project_details_raw(self, project_id):
            return {
                "id": project_id,
                "title": "Project detail",
                "description": "Detailed project",
                "offers": 0,
                "price": 30000,
            }

        async def get_token_api(self):
            return ScoutApi()

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    async def fake_buyer_history(username, **kwargs):
        calls.append(("buyer_history", {"username": username, **kwargs}))
        return {
            "status": "ok",
            "username": username,
            "projects_count": 2,
            "total": 2,
            "projects": [{"id": 1, "title": "Zero offer Telegram"}],
        }

    monkeypatch.setattr(client, "fetch_buyer_history", fake_buyer_history)

    data = await client.get_buyer_scout(
        probes=[{"name": "telegram", "categories": "all", "query": "telegram", "kworks_filter_to": 5}],
        detail_limit=1,
        include_buyer_history=True,
        buyer_history_limit=1,
        top_limit=2,
    )

    assert data["status"] == "ok"
    assert data["aggregate"]["unique_projects"] == 2
    summary = data["aggregate"]["buyer_summary"]
    assert summary["budget_max"] == 5000
    assert summary["zero_offer_count"] == 1
    assert summary["low_offer_count"] == 1
    assert summary["budget_fit_count"] == 0
    assert summary["best_windows"][0]["name"] == "telegram"
    assert summary["next_actions"]
    assert any("Сначала ответь" in item for item in summary["next_actions"])
    assert not any(
        "Reply first" in item or "Рџ" in item or "Р’" in item or "Ð" in item for item in summary["next_actions"]
    )
    assert data["top"][0]["id"] == 1
    assert data["top"][0]["score"] > data["top"][1]["score"]
    assert data["top"][0]["project_detail"]["status"] == "ok"
    assert data["top"][0]["want_detail"]["views"] == 10
    assert data["top"][0]["buyer_history"]["projects_count"] == 2
    signal_kinds = {item["kind"] for item in data["aggregate"]["market_signals"]}
    assert {"zero_offer", "repeat_buyers", "proven_buyers"} <= signal_kinds
    assert "probe_leaders" not in signal_kinds
    signal_labels = {item["kind"]: item["label"] for item in data["aggregate"]["market_signals"]}
    assert (
        signal_labels["zero_offer"]
        == "\u041b\u043e\u0442\u044b \u0431\u0435\u0437 \u043e\u0442\u043a\u043b\u0438\u043a\u043e\u0432"
    )
    assert (
        signal_labels["proven_buyers"]
        == "\u041f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0438 \u043d\u0430\u043d\u0438\u043c\u0430\u044e\u0442"
    )
    signal_text = [
        text
        for item in data["aggregate"]["market_signals"]
        for text in (str(item.get("label") or ""), str(item.get("detail") or ""))
    ]
    assert not any(any(marker in item for marker in ("Рџ", "Р’", "Ð", "PSC")) for item in signal_text)
    assert "high_budget_low_offer" not in signal_kinds
    assert ("want", {"use_token": True, "id": 1}) in calls
    assert ("buyer_history", {"username": "buyer_one", "limit": 6}) in calls
    assert any(call[0] == "projects" and call[1]["kworks_filter_to"] == 5 for call in calls)


@pytest.mark.asyncio
async def test_buyer_scout_uses_rubric_ai_recommendations(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[dict] = []

    class Service:
        async def get_raw_projects(self, **kwargs):
            calls.append(kwargs)
            query = str(kwargs.get("query") or "")
            if not query:
                return (
                    [
                        {
                            "id": 11,
                            "title": "Доработка сайта на WordPress",
                            "description": "Нужно поправить шаблон и ускорить главную страницу.",
                            "category_id": 41,
                            "price": 5000,
                            "offers": 1,
                            "username": "buyer_one",
                            "user_hired_percent": 40,
                        },
                        {
                            "id": 12,
                            "title": "Настройка сайта и формы",
                            "description": "Нужна настройка формы и базового SEO.",
                            "category_id": 41,
                            "price": 7000,
                            "offers": 0,
                            "username": "buyer_two",
                            "user_hired_percent": 50,
                        },
                        {
                            "id": 13,
                            "title": "Настройка рекламы на Ютуб",
                            "description": "Чужой лот не должен попасть в рубрику сайтов.",
                            "category_id": 999,
                            "price": 5000,
                            "offers": 0,
                            "username": "buyer_other",
                            "user_hired_percent": 50,
                        },
                    ],
                    {"paging": {"total": 2}},
                )
            if query == "доработка сайта":
                return (
                    [
                        {
                            "id": 21,
                            "title": "Доработка сайта",
                            "description": "Срочно доработать сайт и исправить ошибки.",
                            "category_id": 41,
                            "price": 6000,
                            "offers": 0,
                            "username": "buyer_three",
                            "user_hired_percent": 45,
                        }
                    ],
                    {"paging": {"total": 1}},
                )
            return ([], {"paging": {"total": 0}})

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    async def fake_recommendations(**kwargs):
        return [
            {
                "query": "доработка сайта",
                "priority": 5,
                "why": "Это главный термин текущей рубрики.",
                "budget": "5000-7000 ₽",
                "competition": "средняя",
                "examples": ["Доработка сайта на WordPress"],
            }
        ]

    monkeypatch.setattr(client, "build_buyer_rubric_recommendations", fake_recommendations)

    data = await client.get_buyer_scout(
        category_id=41,
        category_name="Доработка и настройка сайта",
        classifier_id=100,
        classifier_name="Доработка сайта",
        include_project_details=False,
        include_want_details=False,
        include_buyer_history=False,
        include_query_suggestions=False,
        include_control_windows=False,
        project_page_limit=1,
        per_probe_limit=12,
        top_limit=5,
    )

    assert data["status"] == "ok"
    assert data["aggregate"]["search_recommendations"][0]["query"] == "доработка сайта"
    assert data["aggregate"]["buyer_summary"]["recommendations"][0]["count"] == 1
    assert data["aggregate"]["buyer_summary"]["recommendations"][0]["priority"] == 5
    assert data["probes"][0]["query"] == "доработка сайта"
    assert data["aggregate"]["unique_projects"] == 1
    assert any(call.get("query") == "" for call in calls)
    assert any(call.get("query") == "доработка сайта" for call in calls)
    assert {call.get("categories") for call in calls} == {"41"}
    assert all("Ютуб" not in str(project.get("title")) for project in data["top"])


@pytest.mark.asyncio
async def test_buyer_scout_control_probe_does_not_change_rubric_ranking(monkeypatch):
    from src.platforms import kwork as kwork_module

    class Service:
        async def get_raw_projects(self, **kwargs):
            query = str(kwargs.get("query") or "")
            if query == "сайты":
                return (
                    [{"id": 1, "title": "Доработка сайта", "category_id": 41, "price": 3000, "offers": 0}],
                    {"paging": {"total": 1}},
                )
            if query == "ручная проверка":
                return (
                    [{"id": 2, "title": "Лот только для контроля", "category_id": 41, "price": 3000, "offers": 0}],
                    {"paging": {"total": 1}},
                )
            return (
                [{"id": 3, "title": "Сайт", "category_id": 41, "price": 3000, "offers": 0}],
                {"paging": {"total": 1}},
            )

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    async def recommendations(**kwargs):
        return [{"query": "сайты", "priority": 5, "why": "есть спрос"}]

    monkeypatch.setattr(client, "build_buyer_rubric_recommendations", recommendations)
    data = await client.get_buyer_scout(
        category_id=41,
        probes=[{"name": "manual", "query": "ручная проверка"}],
        include_control_windows=True,
        include_project_details=False,
        include_want_details=False,
        include_query_suggestions=False,
    )

    assert data["aggregate"]["unique_projects"] == 1
    assert [project["id"] for project in data["top"]] == [1]
    assert data["aggregate"]["control_windows"][0]["sample"][0]["id"] == 2


@pytest.mark.asyncio
async def test_buyer_scout_marks_cookie_only_project_api_as_skipped(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[dict] = []

    class Service:
        async def get_raw_projects(self, **kwargs):
            calls.append(kwargs)
            return [], {
                "auth_mode": "cookie-only",
                "token_required": True,
                "detail": "POST /projects requires token-mode auth; current Session Hub client is cookie-only.",
            }

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client.get_buyer_scout(
        probes=[
            {"name": "telegram", "categories": "all", "query": "telegram", "kworks_filter_to": 5},
            {"name": "python", "categories": "all", "query": "python", "kworks_filter_to": 5},
        ],
        include_project_details=False,
        include_want_details=False,
        include_query_suggestions=False,
    )

    assert len(calls) == 1
    assert data["status"] == "empty"
    assert data["probes"][0]["status"] == "skipped"
    assert data["probes"][0]["meta"]["token_required"] is True
    assert data["endpoint_errors"][0]["detail"].startswith("POST /projects requires token-mode auth")


@pytest.mark.asyncio
async def test_buyer_scout_fans_out_pages_and_caches_duplicate_probe_requests(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[dict] = []

    class Service:
        async def get_raw_projects(self, **kwargs):
            calls.append(kwargs)
            page = int(kwargs.get("page") or 1)
            return (
                [
                    {
                        "id": f"{page}",
                        "title": f"Project page {page}",
                        "price": 1000,
                        "offers": page - 1,
                    }
                ],
                {"source": "web_state", "paging": {"page": page, "total": 2, "per_page": 1}},
            )

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client.get_buyer_scout(
        probes=[
            {"name": "first", "categories": "all", "query": "python", "kworks_filter_to": 5},
            {"name": "duplicate", "categories": "all", "query": "python", "kworks_filter_to": 5},
        ],
        max_probes=2,
        project_page_limit=2,
        per_probe_limit=1,
        top_limit=5,
        include_project_details=False,
        include_want_details=False,
        include_query_suggestions=False,
    )

    assert [call["page"] for call in calls] == [1, 2]
    assert data["aggregate"]["unique_projects"] == 2
    assert data["probes"][0]["sample_count"] == 2
    assert data["probes"][1]["pages"][0]["cache_hit"] is True
    assert {item["id"] for item in data["top"]} == {"1", "2"}


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_can_attach_account_context(monkeypatch):
    from src.platforms import kwork as kwork_module

    calls: list[tuple[str, str, dict]] = []

    class AccountApi:
        async def request(self, method, endpoint, **params):
            calls.append((method, endpoint, params))
            if endpoint == "actor":
                return {
                    "response": {
                        "id": 77,
                        "username": "seller",
                        "rating": 4.9,
                        "reviews_count": 123,
                        "active_kworks_count": 1,
                    }
                }
            if endpoint == "getActorInfo":
                return {"response": {"id": 77, "username": "seller"}}
            if endpoint == "kworksStatusList":
                return {"response": [{"status": "active", "count": 1}, {"status": "paused", "count": 2}]}
            if endpoint == "offers":
                return {"response": {"offers": [{"id": 1}]}, "connects": {"available": 10}}
            return {}

    class Service:
        async def get_api(self):
            return AccountApi()

    monkeypatch.setattr(kwork_module, "get_kwork_service", lambda: Service())
    client = KworkMarketClient(api=FakeKworkApi())

    data = await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
        include_demand=False,
        include_account_context=True,
    )

    account = data["account_context"]
    assert account["status"] == "ok"
    assert account["username"] == "seller"
    assert account["active_kworks_count"] == 1
    assert account["statuses"]["active"] == 1
    assert account["offers_count"] == 1
    assert ("post", "actor", {"use_token": True}) in calls
    assert ("post", "kworksStatusList", {"use_token": True}) in calls
    assert data["config"]["include_account_context"] is True


@pytest.mark.asyncio
async def test_market_intelligence_snapshot_collects_seller_profiles(monkeypatch):
    _SELLER_DETAIL_CACHE.clear()
    monkeypatch.setenv("KWORK_SELLER_DETAIL_DELAY", "0")
    api = FakeKworkApi()
    client = KworkMarketClient(api=api)

    data = await client.get_market_intelligence_snapshot(
        seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
        include_demand=False,
        include_seller_details=True,
        seller_detail_limit=2,
    )

    seller = data["seller_intelligence"][0]
    assert data["aggregate"]["seller_profiles_collected"] == 1
    assert seller["username"] == "seller"
    assert seller["reviews_count"] == 123
    assert seller["categories"][0]["id"] == 41
    assert seller["sample_kworks"][0]["title"] == "Seller inventory bot"
    assert seller["portfolio"]["total"] == 7
    assert seller["portfolio"]["items"][0]["views"] == 88
    assert seller["reviews"]["all"]["total"] == 1
    assert seller["reviews"]["negative"]["items"][0]["has_answer"] is True
    assert ("post", "userByUsername", {"username": "seller"}) in api.calls
    assert ("post", "kworksCategoriesList", {"user_id": 77}) in api.calls
    assert ("post", "userKworks", {"user_id": 77, "page": 1}) in api.calls
    assert ("post", "portfolioList", {"user_id": 77, "category_id": "all", "page": 1}) in api.calls
    assert ("post", "userReviews", {"user_id": 77, "type": "all", "page": 1}) in api.calls
    assert ("post", "userReviews", {"user_id": 77, "type": "negative", "page": 1}) in api.calls


@pytest.mark.asyncio
async def test_get_kworks_preserves_top_level_paging_metadata():
    class PagingApi:
        async def request(self, _method: str, _endpoint: str, **_params):
            return {
                "paging": {"page": 2, "pages": 12},
                "response": {"kworks": [{"id": "two"}]},
            }

    catalog = await KworkMarketClient(api=PagingApi()).get_kworks(category_id=38, page=2)

    assert catalog["paging"] == {"page": 2, "pages": 12}
    assert catalog["_request_params"] == {"page": 2, "categoryId": 38}
