from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import src.platforms.kwork_autopublish as kwork_autopublish
from src.platforms.kwork_autopublish import (
    KworkAutopublishService,
    PUBLISH_CONFIRMATION_PHRASE,
    _competitor_cover_items,
    _cover_text_from_draft,
    _extract_json_object,
    _image_api_key,
    _image_api_url,
    _portfolio_payload,
    _render_portfolio_assets,
    _sanitize_kwork_text,
    _should_overlay_cover_text,
)


def test_extract_json_object_from_wrapped_text():
    parsed = _extract_json_object('text before {"title":"A","price":500} text after')

    assert parsed == {"title": "A", "price": 500}


def test_image_connection_uses_newapi_channel_conn(monkeypatch):
    monkeypatch.delenv("KWORK_COVER_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("KWORK_COVER_IMAGE_BASE_URL", raising=False)
    monkeypatch.setenv(
        "KWORK_COVER_IMAGE_CONN",
        '{"_type":"newapi_channel_conn","key":"image-key","url":"https://byesu.com"}',
    )
    monkeypatch.setenv("KWORK_COVER_IMAGE_API_PREFIX", "/v1")

    assert _image_api_key() == "image-key"
    assert _image_api_url("images/generations", "https://api.openai.com/v1/images/generations") == (
        "https://byesu.com/v1/images/generations"
    )


def test_fallback_cover_prompt_includes_competitor_images():
    service = KworkAutopublishService()

    prompt = service._fallback_cover_prompt(
        {"title": "Сделаю телеграм бота", "description": "Описание"},
        {
            "category_name": "Разработка и IT",
            "market_context": {
                "competitors": [
                    {"title": "Бот", "image_url": "https://cdn-edge.kwork.ru/pics/t3/demo.jpg"},
                ]
            },
        },
    )

    assert "https://cdn-edge.kwork.ru/pics/t3/demo.jpg" in prompt
    assert "Add large readable Russian text" in prompt
    assert "No extra readable text" in prompt
    assert "preferably left or upper-left" not in prompt


def test_cover_prompt_brief_requires_image_text():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Сделаю Telegram-бота", "description": "Описание"},
        {"audience": "малого бизнеса", "cover_text": "Telegram-бот под ключ"},
    )

    assert brief["required_cover_text"]["title"] == "Telegram-бот под ключ"
    assert "include the required Russian offer text directly inside the generated image" in brief["requirements"]


def test_cover_prompt_brief_reserves_text_area_for_deterministic_overlay():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Адаптация логотипа"},
        {"cover_text": "Логотип на русском", "cover_text_overlay": True},
    )

    requirements = " ".join(brief["requirements"])
    assert "do not render any readable text" in requirements
    assert "lower third visually calm" in requirements


def test_competitor_cover_items_include_portfolio_examples_without_duplicates():
    items = _competitor_cover_items(
        {
            "market_context": {
                "competitors": [
                    {
                        "title": "Логотип",
                        "image_url": "https://cdn.example/cover.jpg",
                        "portfolio_images": [
                            "https://cdn.example/cover.jpg",
                            "https://cdn.example/work-a.jpg",
                            "https://cdn.example/work-b.jpg",
                        ],
                    }
                ]
            }
        }
    )

    assert [item["image_url"] for item in items] == [
        "https://cdn.example/cover.jpg",
        "https://cdn.example/work-a.jpg",
        "https://cdn.example/work-b.jpg",
    ]
    assert [item["reference_kind"] for item in items] == ["cover", "portfolio", "portfolio"]


def test_cover_prompt_brief_includes_visual_analysis():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Bot", "description": "Description"},
        {
            "market_context": {
                "competitors": [{"title": "A", "image_url": "https://cdn/a.jpg"}],
                "filter_scope": {"selected": [{"labels": ["Telegram"]}]},
                "filter_requests": [{"classifierId": 3612}],
                "demand": {"status": "ok", "wants_count": 12},
            }
        },
        visual_analysis={"status": "analyzed", "brief": "Use a clean blue dashboard style."},
    )

    assert brief["competitor_visual_analysis"]["status"] == "analyzed"
    assert "clean blue dashboard" in brief["competitor_visual_analysis"]["brief"]
    assert brief["selected_market_slice"]["filter_scope"]["selected"][0]["labels"] == ["Telegram"]
    assert brief["selected_market_slice"]["demand"]["wants_count"] == 12


def test_cover_prompt_brief_requires_concrete_deliverable_for_variant():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Сделаю лендинг", "description": "Готовая посадочная страница"},
        {
            "category_name": "Разработка и IT",
            "variant_index": 1,
            "variant_count": 3,
            "creative_direction": "Show the finished page in a real browser.",
        },
    )

    requirements = " ".join(brief["requirements"])
    assert "finished landing page" in requirements
    assert "generic icons or glowing geometry" in requirements
    assert brief["variant"] == {
        "index": 2,
        "count": 3,
        "creative_direction": "Show the finished page in a real browser.",
    }


@pytest.mark.asyncio
async def test_build_cover_prompt_uses_competitor_images_and_recent_history(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    root = tmp_path / "assets"
    cover_dir = root / "kwork_autopublish"
    cover_dir.mkdir(parents=True)
    old_cover = cover_dir / "old.png"
    old_cover.write_bytes(b"old-image")
    (cover_dir / "old.json").write_text(
        """
        {
          "created_at": "2026-07-07T00:00:00Z",
          "status": "generated",
          "path": "old.png",
          "title": "Old cover",
          "prompt": "dark SaaS dashboard with left text panel",
          "visual_style_brief": "left text panel, blue dashboard"
        }
        """,
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    async def fake_analyze(draft, request):
        captured["request"] = request
        request["_cover_competitor_image_data_urls"] = ["data:image/jpeg;base64,competitor"]
        return {
            "status": "analyzed",
            "brief": "Competitors use flat blue banners.",
            "images_seen": 1,
            "image_urls": ["https://cdn/a.jpg"],
        }

    class FakeRouter:
        async def generate_with_images(self, **kwargs):
            captured.update(kwargs)
            return "Create a warmer editorial 3:2 cover with the required Russian text on a natural high-contrast area."

    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", root)
    monkeypatch.setattr(service, "analyze_competitor_covers", fake_analyze)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())

    prompt, source = await service.build_cover_prompt(
        {"title": "Forum setup", "description": "Forum service"},
        {"cover_text": "Создам форум", "market_context": {"competitors": []}},
    )

    assert source == "llm_vision"
    assert "warmer editorial" in prompt
    assert captured["image_urls"] == ["data:image/jpeg;base64,competitor", "data:image/png;base64,b2xkLWltYWdl"]
    assert "recent_generated_cover_history" in str(captured["prompt"])
    assert "avoid repeating" in str(captured["prompt"])
    assert "selected_market_slice" in str(captured["prompt"])
    request_context = captured["request"]["_cover_prompt_context"]
    assert request_context["prompt_writer_route"] == "llm_vision"
    assert request_context["prompt_images_sent"] == 2
    assert request_context["competitor_images_sent"] == 1
    assert request_context["history_images_sent"] == 1


@pytest.mark.asyncio
async def test_build_cover_prompt_marks_text_retry_after_vision_failure(monkeypatch):
    service = KworkAutopublishService()

    async def fake_analyze(draft, request):
        request["_cover_competitor_image_data_urls"] = ["data:image/jpeg;base64,competitor"]
        return {
            "status": "analyzed",
            "brief": "Competitors use dense banners.",
            "images_seen": 1,
            "image_urls": ["https://cdn/a.jpg"],
        }

    class FakeRouter:
        async def generate_with_images(self, **kwargs):
            raise RuntimeError("vision down")

        async def generate(self, **kwargs):
            return "Create a bright editorial cover with a distinct diagonal layout and readable Russian offer text."

    monkeypatch.setattr(service, "analyze_competitor_covers", fake_analyze)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())

    request = {"cover_text": "Forum cover", "market_context": {"competitors": []}}
    prompt, source = await service.build_cover_prompt({"title": "Forum setup"}, request)

    assert source == "llm_text_after_vision_failure"
    assert "diagonal layout" in prompt
    assert request["_cover_prompt_context"]["prompt_writer_route"] == "llm_text_after_vision_failure"
    assert "vision down" in request["_cover_prompt_context"]["warning"]


def test_recent_cover_history_includes_orphan_png(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    cover_dir = tmp_path / "kwork_autopublish"
    cover_dir.mkdir(parents=True)
    orphan = cover_dir / "orphan.png"
    orphan.write_bytes(b"orphan-image")

    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)

    history = service._recent_cover_history(limit=2)

    assert history[0]["status"] == "orphan_png"
    assert history[0]["path"] == str(orphan)
    assert history[0]["data_url"].startswith("data:image/")


@pytest.mark.asyncio
async def test_analyze_competitor_covers_uses_downloaded_images(monkeypatch):
    service = KworkAutopublishService()
    captured = {}

    async def fake_download(items):
        return [{**items[0], "data_url": "data:image/jpeg;base64,aaa"}]

    class FakeRouter:
        async def generate_with_images(self, **kwargs):
            captured.update(kwargs)
            return "Average style: crisp SaaS scene, blue accent, readable Russian text area."

    monkeypatch.setattr(service, "_download_competitor_covers", fake_download)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())

    result = await service.analyze_competitor_covers(
        {"title": "Bot", "description": "Description"},
        {"market_context": {"competitors": [{"title": "A", "image_url": "https://cdn/a.jpg"}]}},
    )

    assert result["status"] == "analyzed"
    assert result["images_seen"] == 1
    assert captured["image_urls"] == ["data:image/jpeg;base64,aaa"]
    assert "Actually inspect the attached images" in captured["prompt"]
    assert "crisp SaaS" in result["brief"]


@pytest.mark.asyncio
async def test_competitor_cover_analysis_can_be_disabled(monkeypatch):
    service = KworkAutopublishService()

    async def fail_download(_items):
        raise AssertionError("disabled analysis must not download competitor covers")

    monkeypatch.setattr(service, "_download_competitor_covers", fail_download)
    result = await service.analyze_competitor_covers(
        {"title": "Landing"},
        {
            "use_competitor_image_analysis": False,
            "market_context": {"competitors": [{"image_url": "https://cdn/a.jpg"}]},
        },
    )

    assert result == {"status": "disabled", "brief": "", "images_seen": 0, "image_urls": []}


@pytest.mark.asyncio
async def test_build_cover_prompt_reuses_precomputed_competitor_analysis(monkeypatch):
    service = KworkAutopublishService()

    async def fail_analyze(_draft, _request):
        raise AssertionError("precomputed analysis must be reused")

    monkeypatch.setattr(service, "analyze_competitor_covers", fail_analyze)
    request = {
        "use_cover_prompt_llm": False,
        "_cover_visual_analysis": {
            "status": "analyzed",
            "brief": "Real product screenshots with clean spacing.",
            "images_seen": 4,
            "image_urls": ["https://cdn/a.jpg"],
        },
    }

    prompt, source = await service.build_cover_prompt({"title": "Landing"}, request)

    assert source == "fallback"
    assert "Real product screenshots" in prompt


def test_cover_text_is_short_offer():
    title, subtitle = _cover_text_from_draft(
        {"title": "Сделаю Telegram-бота или скрипт автоматизации"},
        {"audience": "малого бизнеса"},
    )

    assert title == "Telegram-бот или скрипт"
    assert subtitle == "для малого бизнеса"


def test_cover_text_ignores_question_mark_corruption():
    title, subtitle = _cover_text_from_draft(
        {"title": "Адаптирую логотип и шрифт под кириллицу", "auditory": "для брендов"},
        {"cover_text": "??????? ?? ???????", "cover_subtitle": "????????? ?????"},
    )

    assert title == "Логотип на русском"
    assert subtitle == "для брендов"


def test_sanitize_kwork_text_removes_preorder_contact_phrases():
    text = _sanitize_kwork_text("Сложные варианты можно обсудить до заказа. Напишите до заказа, если есть вопросы.")

    assert "до заказа" not in text.lower()
    assert "уточнить в рамках заказа" in text


def test_manual_cover_overlay_requires_explicit_request(monkeypatch):
    monkeypatch.setenv("KWORK_COVER_TEXT_OVERLAY", "true")

    assert _should_overlay_cover_text({}) is False
    assert _should_overlay_cover_text({"cover_text_overlay": False}) is False
    assert _should_overlay_cover_text({"cover_text_overlay": True}) is True


def test_build_form_payload_preserves_repeated_checkbox_fields():
    service = KworkAutopublishService()

    pairs = service.build_form_payload(
        {
            "category_id": 41,
            "title": "Сделаю бота",
            "description": "Описание",
            "csrftoken": "override-csrf",
            "hidden_fields": {"csrftoken": "old-csrf", "draft_id": "777", "foo_token": "bar"},
            "attribute_selection": {
                "attribute[208]": 3587,
                "attribute[3610][]": [3612, 5273361],
            },
            "cover_upload": {
                "first_photo_json": {"image_path": "/tmp/cover.png"},
                "first_photo_path": "/tmp/cover.png",
                "crop": {"x": 0, "y": 0, "w": 660, "h": 440},
            },
            "portfolios": [
                {
                    "draftHash": 1,
                    "cover": {"idPortfolioMedia": 77, "type": "image"},
                    "items": [{"id": 77, "portfolio_type": "photo", "position": 0}],
                }
            ],
        }
    )

    assert pairs.count(("attribute[3610][]", "3612")) == 1
    assert pairs.count(("attribute[3610][]", "5273361")) == 1
    assert ("attribute[208]", "3587") in pairs
    assert ("csrftoken", "override-csrf") in pairs
    assert ("draft_id", "777") in pairs
    assert ("foo_token", "bar") in pairs
    assert ("csrftoken", "old-csrf") not in pairs
    assert any(name == "first_photo_json" and "image_path" in value for name, value in pairs)
    assert ("first-kwork-photo", "null") in pairs
    assert any(name == "first-kwork-photo-size[]" for name, _ in pairs)
    assert any(name == "portfolio" and '"idPortfolioMedia": 77' in value for name, value in pairs)


def test_render_portfolio_assets_creates_five_distinct_boards(monkeypatch, tmp_path):
    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)

    assets = _render_portfolio_assets(
        {
            "category_id": 25,
            "title": "Адаптирую логотип под кириллицу",
            "cover_text": "Логотип на русском",
        }
    )

    assert len(assets) == 5
    paths = [Path(item["path"]) for item in assets]
    assert all(path.is_file() and path.stat().st_size > 30_000 for path in paths)
    assert len({hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}) == 5
    assert all(item["asset_url"].endswith(Path(item["path"]).name) for item in assets)


def test_portfolio_payload_matches_kwork_browser_shape():
    payload = _portfolio_payload({"id": 77, "crop": None}, "Кириллица для логотипа", 0)

    assert payload["cover"] == {
        "id": None,
        "crop": {"x": 0, "y": 0, "w": 1, "h": 1},
        "type": "image",
        "idPortfolioMedia": 77,
    }
    assert payload["items"] == [
        {"id": 77, "crop": {"x": 0, "y": 0, "w": 1, "h": 1}, "position": 0, "portfolio_type": "photo"}
    ]


@pytest.mark.asyncio
async def test_generate_draft_fallback_without_llm():
    service = KworkAutopublishService()

    result = await service.generate_draft(
        {
            "category_id": 41,
            "category_name": "Разработка и IT",
            "service_summary": "телеграм бота",
            "use_llm": False,
            "price": 1000,
            "work_time": 2,
        }
    )

    assert result["ok"] is True
    assert result["draft"]["category_id"] == 41
    assert result["draft"]["price"] == 1000
    assert result["draft"]["work_time"] == 2
    assert "телеграм бота" in result["draft"]["title"].lower()


@pytest.mark.asyncio
async def test_publish_draft_dry_run_builds_save_payload():
    service = KworkAutopublishService()
    draft = {
        "category_id": 41,
        "title": "Сделаю бота",
        "description": "Описание",
        "instruction": "Дайте ТЗ",
        "auditory": "Бизнес",
        "price": 1000,
        "work_time": 2,
        "attributes": {"10": "11"},
        "attribute_selection": {"attribute[208]": 3587, "attribute[3610][]": [3612, 5273361]},
        "faq": [{"question": "Q", "answer": "A"}],
    }

    result = await service.publish_draft(draft, dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["payload"]["category_id"] == 41
    assert result["payload"]["min_volume_price"] == 1000
    assert result["payload"]["attributes"] == {"attribute[208]": 3587, "attribute[3610][]": [3612, 5273361]}
    assert result["payload"]["attribute_ids"] == [3587, 3612, 5273361]
    assert ("attribute[3610][]", "3612") in [tuple(item) for item in result["payload"]["form_payload"]]


@pytest.mark.asyncio
async def test_publish_draft_live_uses_new_snapshot_and_save(monkeypatch):
    service = KworkAutopublishService()
    saved_payloads = []

    class FakeListingClient:
        def __init__(self, cookies):
            self.cookies = cookies

        async def open_new(self):
            return {
                "ok": True,
                "final_url": "https://kwork.ru/new",
                "hidden_fields": {"csrftoken": "csrf-1", "draft_id": "777"},
                "csrftoken": "csrf-1",
                "draft_id": "777",
            }

        async def upload_cover(self, *args, **kwargs):
            raise AssertionError("cover upload should not be called without cover_image_path")

        async def save_kwork(self, form_payload, referer=None):
            saved_payloads.append((form_payload, referer))
            return {"ok": True, "code": "success", "redirect_url": "/manage_kworks"}

        async def verify_saved_kwork(self, save_result, draft):
            return {"ok": True, "code": "verified"}

    monkeypatch.setattr(service, "_web_cookies", lambda: _fake_cookies_async())
    monkeypatch.setattr("src.platforms.kwork_autopublish.KworkWebListingClient", FakeListingClient)

    draft = {
        "category_id": 41,
        "title": "Сделаю бота",
        "description": "Описание",
        "price": 1000,
        "work_time": 2,
        "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[208]": 3587},
        "cover_upload": {
            "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
            "first_photo_path": "/tmp/kwork-cover.png",
        },
    }
    token = service.issue_publish_token(draft)["token"]

    result = await service.publish_draft(
        draft,
        dry_run=False,
        confirm_token=token,
        confirmation=PUBLISH_CONFIRMATION_PHRASE,
    )

    assert result["ok"] is True
    assert result["code"] == "verified"
    form_payload, referer = saved_payloads[0]
    assert ("csrftoken", "csrf-1") in form_payload
    assert ("draft_id", "777") in form_payload
    assert ("attribute[208]", "3587") in form_payload
    assert referer == "https://kwork.ru/new"


@pytest.mark.asyncio
async def test_publish_draft_live_uploads_cover_and_adds_crop(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    cover = tmp_path / "cover.png"
    cover.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x03\x00\x00\x00\x02\x08\x02\x00\x00\x00"
        b"\x12\x16\xf1M\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    saved_payloads = []

    class FakeListingClient:
        def __init__(self, cookies):
            self.cookies = cookies

        async def open_new(self):
            return {"ok": True, "final_url": "https://kwork.ru/new", "hidden_fields": {"csrftoken": "csrf-1"}}

        async def upload_cover(self, path, **kwargs):
            assert Path(path) == cover
            return {
                "ok": True,
                "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
                "first_photo_path": "/tmp/kwork-cover.png",
                "draft_id": "888",
            }

        async def save_kwork(self, form_payload, referer=None):
            saved_payloads.append(form_payload)
            return {"ok": True, "code": "success"}

        async def verify_saved_kwork(self, save_result, draft):
            return {"ok": True, "code": "verified"}

    monkeypatch.setattr(service, "_web_cookies", lambda: _fake_cookies_async())
    monkeypatch.setattr("src.platforms.kwork_autopublish.KworkWebListingClient", FakeListingClient)

    draft = {
        "category_id": 41,
        "title": "Сделаю бота",
        "description": "Описание",
        "price": 1000,
        "work_time": 2,
        "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[208]": 3587},
        "cover_image_path": str(cover),
    }
    token = service.issue_publish_token(draft)["token"]

    result = await service.publish_draft(
        draft,
        dry_run=False,
        confirm_token=token,
        confirmation=PUBLISH_CONFIRMATION_PHRASE,
    )

    assert result["ok"] is True
    payload = saved_payloads[0]
    assert any(name == "first_photo_json" and "image_path" in value for name, value in payload)
    assert ("first_photo_path", "/tmp/kwork-cover.png") in payload
    assert any(name == "first-kwork-photo-size[]" and '"w"' in value for name, value in payload)


@pytest.mark.asyncio
async def test_publish_design_draft_uploads_required_portfolio(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    saved_payloads = []
    uploaded_paths = []
    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)

    class FakeListingClient:
        def __init__(self, cookies):
            self.cookies = cookies

        async def open_new(self):
            return {"ok": True, "final_url": "https://kwork.ru/new", "hidden_fields": {"csrftoken": "csrf-1"}}

        async def upload_portfolio_image(self, path, **kwargs):
            uploaded_paths.append(Path(path))
            index = len(uploaded_paths)
            return {"ok": True, "id": 100 + index, "hash": f"h-{index}", "crop": None}

        async def save_kwork(self, form_payload, referer=None):
            saved_payloads.append(form_payload)
            return {"ok": True, "code": "success"}

        async def verify_saved_kwork(self, save_result, draft):
            return {"ok": True, "code": "verified"}

    monkeypatch.setattr(service, "_web_cookies", lambda: _fake_cookies_async())
    monkeypatch.setattr("src.platforms.kwork_autopublish.KworkWebListingClient", FakeListingClient)

    draft = {
        "category_id": 25,
        "title": "Адаптирую логотип под кириллицу",
        "description": "Описание",
        "price": 1250,
        "work_time": 3,
        "attribute_manifest": {"controls": [{"name": "attribute[1624]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[1624]": 401928},
        "cover_upload": {
            "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "cover-hash"},
            "first_photo_path": "/tmp/kwork-cover.png",
            "first_photo_hash": "cover-hash",
        },
    }
    token = service.issue_publish_token(draft)["token"]

    result = await service.publish_draft(
        draft,
        dry_run=False,
        confirm_token=token,
        confirmation=PUBLISH_CONFIRMATION_PHRASE,
    )

    assert result["ok"] is True
    assert len(uploaded_paths) == 5
    assert ("package_volume", "1") in saved_payloads[0]
    assert ("bundle_standard_duration", "3") in saved_payloads[0]
    assert any(name == "bundle_standard_description" for name, _ in saved_payloads[0])
    assert ("bundle_extra_standard_value[category][190]", "1") in saved_payloads[0]
    assert ("bundle_extra_standard_value[category][191]", "1") in saved_payloads[0]
    portfolios_pair = next(value for name, value in saved_payloads[0] if name == "portfolio")
    portfolios = kwork_autopublish.json.loads(portfolios_pair)
    assert len(portfolios) == 5
    assert portfolios[0]["cover"]["idPortfolioMedia"] == 101


@pytest.mark.asyncio
async def test_publish_draft_live_requires_post_save_verification(monkeypatch):
    service = KworkAutopublishService()

    class FakeListingClient:
        def __init__(self, cookies):
            self.cookies = cookies

        async def open_new(self):
            return {"ok": True, "final_url": "https://kwork.ru/new", "hidden_fields": {"csrftoken": "csrf-1"}}

        async def save_kwork(self, form_payload, referer=None):
            return {"ok": True, "code": "success", "redirect_url": "/manage_kworks"}

        async def verify_saved_kwork(self, save_result, draft):
            return {"ok": False, "code": "not_found_after_save"}

    monkeypatch.setattr(service, "_web_cookies", lambda: _fake_cookies_async())
    monkeypatch.setattr("src.platforms.kwork_autopublish.KworkWebListingClient", FakeListingClient)

    draft = {
        "category_id": 41,
        "title": "Сделаю бота",
        "description": "Описание",
        "price": 1000,
        "work_time": 2,
        "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[208]": 3587},
        "cover_upload": {
            "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
            "first_photo_path": "/tmp/kwork-cover.png",
        },
    }
    token = service.issue_publish_token(draft)["token"]

    result = await service.publish_draft(
        draft,
        dry_run=False,
        confirm_token=token,
        confirmation=PUBLISH_CONFIRMATION_PHRASE,
    )

    assert result["ok"] is False
    assert result["code"] == "post_save_verification_failed"
    assert result["save_result"]["ok"] is True


@pytest.mark.asyncio
async def test_publish_draft_live_preserves_manual_verification_from_verify_result(monkeypatch):
    service = KworkAutopublishService()

    class FakeListingClient:
        def __init__(self, cookies):
            self.cookies = cookies

        async def open_new(self):
            return {"ok": True, "final_url": "https://kwork.ru/new", "hidden_fields": {"csrftoken": "csrf-1"}}

        async def save_kwork(self, form_payload, referer=None):
            return {"ok": True, "code": "success", "redirect_url": "/manage_kworks"}

        async def verify_saved_kwork(self, save_result, draft):
            return {
                "ok": False,
                "code": "manual_verification_required",
                "detail": "Kwork requires a manual SmartCaptcha/robot check.",
                "final_url": "https://kwork.ru/",
            }

    monkeypatch.setattr(service, "_web_cookies", lambda: _fake_cookies_async())
    monkeypatch.setattr("src.platforms.kwork_autopublish.KworkWebListingClient", FakeListingClient)

    draft = {
        "category_id": 41,
        "title": "РЎРґРµР»Р°СЋ Р±РѕС‚Р°",
        "description": "РћРїРёСЃР°РЅРёРµ",
        "price": 1000,
        "work_time": 2,
        "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[208]": 3587},
        "cover_upload": {
            "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
            "first_photo_path": "/tmp/kwork-cover.png",
        },
    }
    token = service.issue_publish_token(draft)["token"]

    result = await service.publish_draft(
        draft,
        dry_run=False,
        confirm_token=token,
        confirmation=PUBLISH_CONFIRMATION_PHRASE,
    )

    assert result["ok"] is False
    assert result["code"] == "manual_verification_required"
    assert result["detail"] == "Kwork requires a manual SmartCaptcha/robot check."
    assert result["verify_result"]["final_url"] == "https://kwork.ru/"


@pytest.mark.asyncio
async def test_publish_draft_live_blocks_without_cover_before_network(monkeypatch):
    service = KworkAutopublishService()

    async def fail_cookies():
        raise AssertionError("preflight should run before cookie lookup")

    monkeypatch.setattr(service, "_web_cookies", fail_cookies)

    result = await service.publish_draft(
        {
            "category_id": 41,
            "title": "Сделаю бота",
            "description": "Описание",
            "price": 1000,
            "work_time": 2,
            "attribute_selection": {"attribute[208]": 3587},
        },
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["code"] == "live_preflight_failed"
    assert "cover" in result["preflight"]["missing"]


def test_autopublish_routes_issue_token_and_require_confirmation():
    from fastapi.testclient import TestClient

    from src.api.server import app

    draft = {
        "category_id": 41,
        "title": "Сделаю бота",
        "description": "Описание",
        "price": 1000,
        "work_time": 2,
        "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[208]": 3587},
        "cover_upload": {
            "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
            "first_photo_path": "/tmp/kwork-cover.png",
        },
    }

    with TestClient(app) as client:
        preflight = client.post("/api/kwork/autopublish/preflight", json={"draft": draft})
        assert preflight.status_code == 200
        preflight_data = preflight.json()
        assert preflight_data["ok"] is True
        assert preflight_data["token"]

        publish = client.post("/api/kwork/autopublish/publish", json={"draft": draft, "dry_run": False})
        assert publish.status_code == 200
        publish_data = publish.json()
        assert publish_data["ok"] is False
        assert publish_data["code"] == "confirmation_phrase_required"


def test_live_preflight_ignores_disabled_controls():
    service = KworkAutopublishService()
    result = service.live_preflight(
        {
            "category_id": 41,
            "title": "Сделаю бота",
            "description": "Описание",
            "price": 1000,
            "work_time": 2,
            "attribute_manifest": {
                "controls": [
                    {"name": "attribute[208]", "disabled": False},
                    {"name": "attribute[999]", "disabled": True},
                ],
                "unresolved_required": [],
            },
            "attribute_selection": {"attribute[208]": 3587},
            "cover_upload": {
                "first_photo_json": {"image_path": "/tmp/kwork-cover.png", "hash": "h1"},
                "first_photo_path": "/tmp/kwork-cover.png",
            },
        }
    )

    assert result["ok"] is True
    assert result["missing"] == []


async def _fake_cookies_async():
    return {"PHPSESSID": "abc"}
