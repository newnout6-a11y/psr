from __future__ import annotations

import pytest

from src.api.routes import kwork


@pytest.mark.asyncio
async def test_kwork_register_verify_uses_server_side_registration_id(monkeypatch):
    captured: dict[str, object] = {}

    class Service:
        async def verify_registration_activation(self, **kwargs):
            captured.update(kwargs)
            return {"ok": False, "status": "activation_pending", "registration_id": kwargs["registration_id"]}

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: Service())
    payload = kwork.KworkRegistrationVerificationRequest(registration_id="a" * 32)

    result = await kwork.kwork_register_verify(payload)

    assert result["registration_id"] == "a" * 32
    assert captured == {"registration_id": "a" * 32, "mail_password": "", "firstmail_api_key": ""}


@pytest.mark.asyncio
async def test_kwork_register_batch_passes_count_and_vpnte_slots(monkeypatch):
    captured: dict[str, object] = {}

    class Service:
        async def register_accounts_batch(self, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "requested_count": kwargs["account_count"], "results": []}

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: Service())
    payload = kwork.KworkRegisterBatchRequest(
        account_count=3,
        vpnte_slots=[12, 14],
    )

    result = await kwork.kwork_register_batch(payload)

    assert result["requested_count"] == 3
    assert captured["vpnte_slots"] == [12, 14]
    assert captured["avoid_used_ips"] is False


def test_kwork_register_batch_accepts_more_slots_than_accounts():
    payload = kwork.KworkRegisterBatchRequest(
        account_count=100,
        vpnte_slots=list(range(1, 122)),
    )

    assert payload.account_count == 100
    assert payload.vpnte_slots == list(range(1, 122))


def test_kwork_register_request_ignores_external_kwork_password():
    payload = kwork.KworkRegisterRequest.model_validate({"kwork_password": "external-password"})

    assert "kwork_password" not in payload.model_dump()


@pytest.mark.asyncio
async def test_kwork_registration_credentials_route_returns_saved_credentials(monkeypatch):
    class Service:
        def get_registration_credentials(self, registration_id):
            assert registration_id == "b" * 32
            return {
                "registration_id": registration_id,
                "email": "saved@catchmail.io",
                "username": "saveduser",
                "password": "PsrSavedPasswordA1",
            }

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: Service())

    result = await kwork.kwork_register_credentials(
        kwork.KworkRegistrationCredentialsRequest(registration_id="b" * 32)
    )

    assert result["password"] == "PsrSavedPasswordA1"


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


def test_kwork_verification_summary_separates_global_script_from_challenge():
    result = kwork._summarize_kwork_verification(
        captcha_status={
            "ok": False,
            "required": False,
            "source": "getCaptchaStatus",
            "error": "KworkException: Некорректные значения параметров",
        },
        cookie_count=2,
        pages=[
            {
                "path": "/new",
                "status_code": 200,
                "final_url": "https://kwork.ru/new",
                "evidence": {
                    "manual_required": False,
                    "script_matches": ["smartcaptcha", "smart-token"],
                    "strong_matches": [],
                    "weak_matches": [],
                    "challenge_url": False,
                    "challenge_status": False,
                    "has_new_form": True,
                },
            }
        ],
        generated_at="2026-07-09T13:00:00Z",
    )

    assert result["status"] == "ok"
    assert result["manual_verification_required"] is False
    assert result["web_session_ok"] is True
    assert result["smartcaptcha_scripts_seen"] is True
    assert "not itself a captcha challenge" in result["detail"]


def test_kwork_verification_summary_reports_api_flag_only():
    result = kwork._summarize_kwork_verification(
        captcha_status={"ok": True, "required": True, "source": "getCaptchaStatus"},
        cookie_count=2,
        pages=[
            {
                "path": "/projects",
                "status_code": 200,
                "final_url": "https://kwork.ru/projects",
                "evidence": {"manual_required": False, "script_matches": []},
            }
        ],
        generated_at="2026-07-09T13:00:00Z",
    )

    assert result["status"] == "api_flag_only"
    assert result["manual_verification_required"] is False
    assert result["captcha_required"] is False


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
async def test_kwork_market_web_catalog_passes_alias_options(monkeypatch):
    captured: dict[str, object] = {}

    class FakeService:
        async def _fetch_session_hub_cookies(self):
            return {"slrememberme": "present"}

    class FakeClient:
        async def get_web_catalog_filters(self, alias, *, page=1, page_size=10, include_raw=False, cookies=None):
            captured.update(
                {
                    "alias": alias,
                    "page": page,
                    "page_size": page_size,
                    "include_raw": include_raw,
                    "cookies": cookies,
                }
            )
            return {"alias": alias, "success": True, "protection_status": "ok"}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: FakeService())
    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeClient())

    result = await kwork.kwork_market_web_catalog("programming", page=2, page_size=12, include_raw=True)

    assert result["success"] is True
    assert captured == {
        "alias": "programming",
        "page": 2,
        "page_size": 12,
        "include_raw": True,
        "cookies": {"slrememberme": "present"},
        "closed": True,
    }


@pytest.mark.asyncio
async def test_kwork_market_web_catalog_snapshot_passes_payload_and_cookies(monkeypatch):
    captured: dict[str, object] = {}

    class FakeService:
        async def _fetch_session_hub_cookies(self):
            return {"slrememberme": "present"}

    class FakeClient:
        async def get_web_catalog_alias_snapshot(self, **kwargs):
            captured.update(kwargs)
            return {
                "source": "test",
                "aggregate": {"alias_count": len(kwargs.get("aliases") or [])},
                "results": [],
                "errors": [],
            }

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "get_kwork_service", lambda: FakeService())
    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeClient())

    result = await kwork.kwork_market_web_catalog_snapshot(
        kwork.KworkWebCatalogSnapshotRequest(
            aliases=["programming", "design"],
            page=2,
            page_size=12,
            delay_seconds=0.5,
            include_raw=True,
            write_file=False,
        )
    )

    assert result["aggregate"]["alias_count"] == 2
    assert captured["aliases"] == ["programming", "design"]
    assert captured["page"] == 2
    assert captured["page_size"] == 12
    assert captured["delay_seconds"] == 0.5
    assert captured["include_raw"] is True
    assert captured["write_file"] is False
    assert captured["cookies"] == {"slrememberme": "present"}
    assert captured["closed"] is True


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


@pytest.mark.asyncio
async def test_kwork_market_intelligence_snapshot_route_passes_payload(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMarketClient:
        async def get_market_intelligence_snapshot(self, **kwargs):
            captured.update(kwargs)
            return {
                "source": "test",
                "aggregate": {"seed_count": len(kwargs.get("seeds") or [])},
                "supply": [],
                "query_demand": {},
            }

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeMarketClient())

    result = await kwork.kwork_market_intelligence_snapshot(
        kwork.KworkMarketIntelligenceRequest(
            seeds=[{"name": "Bots", "category_id": 41, "classifier_id": 100}],
            max_seeds=1,
            pages=1,
            include_demand=True,
            demand_queries=["telegram"],
            include_seller_details=True,
            seller_detail_limit=3,
            include_want_details=True,
            want_detail_limit=1,
            include_price_rules=True,
            include_account_context=True,
            write_file=False,
        )
    )

    assert result["aggregate"]["seed_count"] == 1
    assert captured["seeds"] == [{"name": "Bots", "category_id": 41, "classifier_id": 100}]
    assert captured["max_seeds"] == 1
    assert captured["demand_queries"] == ["telegram"]
    assert captured["include_seller_details"] is True
    assert captured["seller_detail_limit"] == 3
    assert captured["include_want_details"] is True
    assert captured["want_detail_limit"] == 1
    assert captured["include_price_rules"] is True
    assert captured["include_account_context"] is True
    assert captured["write_file"] is False
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_kwork_market_buyer_scout_route_passes_payload(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMarketClient:
        async def get_buyer_scout(self, **kwargs):
            captured.update(kwargs)
            return {"source": "test", "top": [{"id": 1}], "aggregate": {"unique_projects": 1}}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeMarketClient())

    result = await kwork.kwork_market_buyer_scout(
        kwork.KworkBuyerScoutRequest(
            probes=[{"name": "telegram", "categories": "all", "query": "telegram", "kworks_filter_to": 5}],
            category_id=41,
            classifier_id=100,
            category_name="Доработка и настройка сайта",
            classifier_name="Доработка сайта",
            attribute_selection={"attribute[1]": "2"},
            attribute_controls=[{"name": "attribute[1]", "type": "radio"}],
            max_probes=1,
            page=1,
            project_page_limit=2,
            per_probe_limit=7,
            top_limit=9,
            include_project_details=True,
            include_want_details=False,
            detail_limit=3,
            budget_max=5000,
            include_query_suggestions=True,
            query_suggestion_limit=4,
            write_file=True,
        )
    )

    assert result["aggregate"]["unique_projects"] == 1
    assert captured["probes"] == [
        {"name": "telegram", "categories": "all", "query": "telegram", "kworks_filter_to": 5}
    ]
    assert captured["category_id"] == 41
    assert captured["classifier_id"] == 100
    assert captured["category_name"] == "Доработка и настройка сайта"
    assert captured["classifier_name"] == "Доработка сайта"
    assert captured["attribute_selection"] == {"attribute[1]": "2"}
    assert captured["attribute_controls"] == [{"name": "attribute[1]", "type": "radio"}]
    assert captured["max_probes"] == 1
    assert captured["project_page_limit"] == 2
    assert captured["per_probe_limit"] == 7
    assert captured["top_limit"] == 9
    assert captured["include_project_details"] is True
    assert captured["include_want_details"] is False
    assert captured["detail_limit"] == 3
    assert captured["budget_max"] == 5000
    assert captured["include_query_suggestions"] is True
    assert captured["query_suggestion_limit"] == 4
    assert captured["write_file"] is True
    assert captured["closed"] is True


def test_kwork_market_buyer_scout_request_defaults_are_fast():
    payload = kwork.KworkBuyerScoutRequest()

    assert payload.include_project_details is False
    assert payload.include_want_details is False
    assert payload.include_buyer_history is False
    assert payload.project_page_limit == 2
    assert payload.budget_max == 5000


@pytest.mark.asyncio
async def test_kwork_market_intelligence_history_route_reads_index(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMarketClient:
        def get_market_intelligence_history(self, **kwargs):
            captured.update(kwargs)
            return {"entry_count": 1, "entries": [{"generated_at": "now"}], "exists": True}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: FakeMarketClient())

    result = await kwork.kwork_market_intelligence_history(limit=7)

    assert result["entry_count"] == 1
    assert captured["limit"] == 7
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_kwork_market_categories_reports_missing_proxy_dependency(monkeypatch):
    closed = {"value": False}

    class BrokenMarketClient:
        async def get_categories_tree(self):
            raise ImportError("Proxy support requires optional dependency aiohttp-socks")

        async def close(self):
            closed["value"] = True

    monkeypatch.setattr(kwork, "KworkMarketClient", lambda: BrokenMarketClient())

    with pytest.raises(kwork.HTTPException) as exc:
        await kwork.kwork_market_categories()

    assert exc.value.status_code == 503
    assert "aiohttp-socks" in str(exc.value.detail)
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_kwork_market_catalog_aliases_filters_to_selected_category():
    result = await kwork.kwork_market_catalog_aliases(category_id=38)

    assert result["schema_version"] == 1
    assert result["total"] >= 2
    assert result["items"][0] == {
        "category_id": 38,
        "category_name": "Доработка и настройка сайта",
        "label": "Доработка и настройка сайта",
        "alias": "website-repair",
        "recommended": True,
    }
    assert {item["alias"] for item in result["items"]} >= {
        "website-repair",
        "website-repair/dorabotka-sayta",
    }


@pytest.mark.asyncio
async def test_kwork_market_supply_scan_route_passes_rubric_scope(monkeypatch):
    captured: dict[str, object] = {}

    class Scanner:
        async def scan(self, **kwargs):
            captured.update(kwargs)
            return {"source": "psr.kwork_supply_scan", "scope": kwargs}

        async def close(self):
            captured["closed"] = True

    monkeypatch.setattr(kwork, "KworkSupplyScanner", Scanner)

    result = await kwork.kwork_market_supply_scan(
        kwork.KworkSupplyScanRequest(
            category_id=38,
            category_name="Site work",
            classifier_id=10,
            classifier_name="Slice A",
            coverage="deep",
            include_llm=False,
            write_file=False,
        )
    )

    assert result["source"] == "psr.kwork_supply_scan"
    assert result["deprecation"]["replacement"] == "/api/kwork/market/jobs"
    assert captured == {
        "category_id": 38,
        "category_name": "Site work",
        "classifier_id": 10,
        "classifier_name": "Slice A",
        "coverage": "deep",
        "include_llm": False,
        "write_file": False,
        "closed": True,
    }


@pytest.mark.asyncio
async def test_kwork_market_assistant_route_passes_saved_context(monkeypatch):
    captured: dict[str, str] = {}

    class Assistant:
        async def ask(self, context_id: str, message: str):
            captured.update({"context_id": context_id, "message": message})
            return {"context_id": context_id, "answer": "Evidence-backed answer.", "refresh": None}

    monkeypatch.setattr(kwork, "MarketAssistant", Assistant)

    result = await kwork.kwork_market_assistant(
        kwork.KworkMarketAssistantRequest(context_id="context-1234", message="What is sparse?")
    )

    assert result["answer"] == "Evidence-backed answer."
    assert captured == {"context_id": "context-1234", "message": "What is sparse?"}
