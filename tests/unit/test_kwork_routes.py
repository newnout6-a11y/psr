from __future__ import annotations

import pytest

from src.api.routes import kwork


class _SessionHubOnlyService:
    async def _fetch_session_hub_cookies(self):
        return {"slrememberme": "present", "uad": "present"}

    async def get_api(self):
        raise AssertionError("status should not use password auth when Session Hub cookies exist")


@pytest.mark.asyncio
async def test_kwork_status_uses_session_hub_cookies(monkeypatch):
    monkeypatch.setattr(kwork, "get_kwork_service", lambda: _SessionHubOnlyService())
    monkeypatch.delenv("KWORK_EMAIL", raising=False)
    monkeypatch.delenv("KWORK_PASSWORD", raising=False)

    result = await kwork.kwork_status()

    assert result["configured"] is True
    assert result["api_ok"] is True
    assert result["api_error"] is None
    assert result["auth_mode"] == "cookie-only"
    assert result["session_hub_ok"] is True
    assert result["session_hub_cookie_count"] == 2


@pytest.mark.asyncio
async def test_kwork_attribute_suggest_fallback_uses_context():
    result = await kwork.kwork_market_category_attribute_suggest(
        41,
        kwork.KworkAttributeSuggestRequest(
            category_name="Разработка и IT / Скрипты, боты и mini apps",
            classifier_name="Чат-боты",
            service_summary="Telegram-бот для заявок и уведомлений",
            audience="малый бизнес",
            use_llm=False,
            control={
                "name": "attribute[27][]",
                "type": "checkbox",
                "multiple": True,
                "required": True,
                "options": [
                    {"id": 726, "label": "Xenforo"},
                    {"id": 28, "label": "Wordpress"},
                    {"id": 999, "label": "Telegram"},
                ],
            },
            selection={},
        ),
    )

    assert result["ok"] is True
    assert result["source"] == "fallback"
    assert result["selected_ids"] == [999]
    assert result["selection"]["attribute[27][]"] == [999]


@pytest.mark.asyncio
async def test_kwork_attribute_suggest_rejects_invalid_control():
    with pytest.raises(kwork.HTTPException) as exc:
        await kwork.kwork_market_category_attribute_suggest(
            41,
            kwork.KworkAttributeSuggestRequest(
                service_summary="anything",
                use_llm=False,
                control={"name": "attribute[1]", "options": []},
            ),
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_kwork_form_manifest_uses_session_hub_cookies(monkeypatch):
    captured: dict[str, object] = {}

    class FakeService:
        async def _fetch_session_hub_cookies(self):
            return {"slrememberme": "present"}

    class FakeListingClient:
        def __init__(self, cookies=None):
            captured["cookies"] = cookies

        async def build_attribute_manifest(self, category_id, *, selection=None, classifier_id=None, lang="ru"):
            captured["category_id"] = category_id
            captured["selection"] = selection
            captured["classifier_id"] = classifier_id
            captured["lang"] = lang
            return {
                "category_id": category_id,
                "lang": lang,
                "success": True,
                "code": "",
                "detail": "",
                "selected": selection or {},
                "controls": [],
                "metadata": {},
                "fragments": [],
                "unresolved_required": [],
            }

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: FakeService())
    monkeypatch.setattr(kwork, "KworkWebListingClient", FakeListingClient)

    result = await kwork.kwork_market_category_form_manifest(
        41,
        kwork.KworkFormManifestRequest(classifier_id=3587, selection={"attribute[208]": 3587}),
    )

    assert result["success"] is True
    assert captured["cookies"] == {"slrememberme": "present"}
    assert captured["category_id"] == 41
    assert captured["classifier_id"] == 3587


@pytest.mark.asyncio
async def test_kwork_form_manifest_returns_structured_exception(monkeypatch):
    class FakeService:
        async def _fetch_session_hub_cookies(self):
            return {}

    class BrokenListingClient:
        def __init__(self, cookies=None):
            pass

        async def build_attribute_manifest(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: FakeService())
    monkeypatch.setattr(kwork, "KworkWebListingClient", BrokenListingClient)

    result = await kwork.kwork_market_category_form_manifest(41, kwork.KworkFormManifestRequest())

    assert result["success"] is False
    assert result["code"] == "form_manifest_exception"
    assert "RuntimeError: boom" in result["detail"]
    assert result["controls"] == []


@pytest.mark.asyncio
async def test_kwork_market_metrics_post_passes_attribute_filters(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMarketClient:
        async def get_market_metrics(self, **kwargs):
            captured.update(kwargs)
            return {
                "category_id": kwargs["category_id"],
                "classifier_id": kwargs.get("classifier_id"),
                "page": kwargs.get("page"),
                "kworks_count": 0,
                "classifiers": [],
                "competitors": [],
                "raw_keys": [],
                "demand": {"status": "skipped"},
            }

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeMarketClient())

    result = await kwork.kwork_market_metrics_post(
        kwork.KworkMarketMetricsRequest(
            category_id=41,
            classifier_id=3587,
            include_demand=False,
            attribute_selection={"attribute[3610][]": [3612]},
            attribute_controls=[{"name": "attribute[3610][]", "options": [{"id": 3612, "label": "Telegram"}]}],
        )
    )

    assert result["category_id"] == 41
    assert captured["attribute_filters"] == {"attribute[3610][]": [3612]}
    assert captured["attribute_controls"] == [{"name": "attribute[3610][]", "options": [{"id": 3612, "label": "Telegram"}]}]
    assert captured["closed"] is True
