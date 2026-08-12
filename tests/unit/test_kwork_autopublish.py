from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest

import src.platforms.kwork_autopublish as kwork_autopublish
from src.platforms.kwork_autopublish import (
    KworkAutopublishService,
    PUBLISH_CONFIRMATION_PHRASE,
    _competitor_cover_items,
    _extract_json_object,
    _image_api_key,
    _image_model,
    _image_api_url,
    _portfolio_image_specs,
    _portfolio_payload,
    _sanitize_kwork_text,
    _scene_subject_constraints,
    _visual_strategy,
)


def _png_b64(width: int = 768, height: int = 512) -> str:
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (width, height), (30, 60, 90)).save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


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


def test_fallback_cover_prompt_is_literal_and_has_no_promotional_text():
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

    assert "already-shipped website, application screen" in prompt
    assert "never add a cover headline" in prompt
    assert "presentation board" in prompt
    assert "https://cdn-edge.kwork.ru" not in prompt
    assert "Add large readable Russian text" not in prompt


def test_cover_prompt_brief_routes_web_without_external_cover_text():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Сделаю Telegram-бота", "description": "Описание"},
        {"audience": "малого бизнеса", "category_name": "Разработка и IT"},
    )

    assert brief["visual_strategy"]["domain"] == "web_software"
    assert "never add a cover headline, caption, title, subtitle, label, or promotional typography" in brief["requirements"]
    assert "required_cover_text" not in brief


def test_cover_prompt_brief_non_text_domain_uses_full_canvas_without_overlay():
    service = KworkAutopublishService()

    brief = service._cover_prompt_brief(
        {"title": "Смонтирую рекламный ролик"},
        {"category_name": "Аудио и видео"},
    )

    requirements = " ".join(brief["requirements"])
    assert "do not render readable words" in requirements
    assert "do not reserve space for a later text overlay" in requirements


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

    assert brief["competitor_negative_evidence"]["status"] == "analyzed"
    assert "clean blue dashboard" in brief["competitor_negative_evidence"]["brief"]
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
    assert "literal visual proof" in requirements
    assert "floating cubes" in requirements
    assert "email address" in requirements
    assert brief["visual_strategy"]["domain"] == "web_software"
    assert brief["variant"] == {
        "index": 2,
        "count": 3,
        "creative_direction": "Show the finished page in a real browser.",
    }


@pytest.mark.asyncio
async def test_build_cover_prompt_does_not_reuse_old_images_as_style_references(monkeypatch, tmp_path):
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
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return "Create one coherent warmer editorial 3:2 scene showing the finished forum product edge to edge."

    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", root)
    monkeypatch.setattr(service, "analyze_competitor_covers", fake_analyze)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())
    monkeypatch.setenv("KWORK_COVER_PROMPT_PROVIDER", "openai")
    monkeypatch.setenv("KWORK_COVER_PROMPT_MODEL", "gpt-5.6-terra")

    prompt, source = await service.build_cover_prompt(
        {"title": "Forum setup", "description": "Forum service"},
        {"market_context": {"competitors": []}},
    )

    assert source == "llm_text_no_images"
    assert "warmer editorial" in prompt
    assert "image_urls" not in captured
    assert "recent_generated_cover_history" in str(captured["prompt"])
    assert "competitor_negative_evidence" in str(captured["prompt"])
    assert "visual_strategy" in str(captured["prompt"])
    assert "selected_market_slice" in str(captured["prompt"])
    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-5.6-terra"
    assert captured["allow_fallback"] is False
    request_context = captured["request"]["_cover_prompt_context"]
    assert request_context["prompt_writer_route"] == "llm_text_no_images"
    assert request_context["prompt_images_sent"] == 0
    assert request_context["competitor_images_sent"] == 0
    assert request_context["history_images_sent"] == 0
    assert request_context["prompt_writer_provider"] == "openai"
    assert request_context["prompt_writer_model"] == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_build_cover_prompt_never_sends_reference_images(monkeypatch):
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
            raise AssertionError("prompt writer must never receive reference images")

        async def generate(self, **kwargs):
            return "Create a bright editorial scene with one real finished forum product and no external headline."

    monkeypatch.setattr(service, "analyze_competitor_covers", fake_analyze)
    monkeypatch.setattr("src.brain.llm_router.get_llm_router", lambda: FakeRouter())
    monkeypatch.setenv("KWORK_COVER_PROMPT_PROVIDER", "openai")
    monkeypatch.setenv("KWORK_COVER_PROMPT_MODEL", "gpt-5.6-terra")

    request = {"market_context": {"competitors": []}}
    prompt, source = await service.build_cover_prompt({"title": "Forum setup"}, request)

    assert source == "llm_text_no_images"
    assert "finished forum product" in prompt
    assert request["_cover_prompt_context"]["prompt_writer_route"] == "llm_text_no_images"
    assert request["_cover_prompt_context"]["prompt_images_sent"] == 0


def test_recent_cover_history_filters_category_and_non_generated_assets(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    cover_dir = tmp_path / "kwork_autopublish"
    cover_dir.mkdir(parents=True)
    for name in ("same", "other", "fallback", "orphan"):
        (cover_dir / f"{name}.png").write_bytes(f"{name}-image".encode())
    (cover_dir / "same.json").write_text(
        '{"asset_kind":"cover","visual_pipeline_version":"gpt-image-2-concrete-v3","status":"generated","path":"same.png","title":"Same","category":"Разработка и IT","image_generation":{"requested_model":"gpt-image-2"}}',
        encoding="utf-8",
    )
    (cover_dir / "other.json").write_text(
        '{"asset_kind":"cover","visual_pipeline_version":"gpt-image-2-concrete-v3","status":"generated","path":"other.png","title":"Other","category":"Дизайн","image_generation":{"requested_model":"gpt-image-2"}}',
        encoding="utf-8",
    )
    (cover_dir / "fallback.json").write_text(
        '{"asset_kind":"cover","status":"generated_local_fallback","path":"fallback.png","title":"Fallback","category":"Разработка и IT"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)

    history = service._recent_cover_history(
        {"category_name": "Разработка и IT"},
        {"category_name": "Разработка и IT"},
        limit=4,
    )

    assert [item["title"] for item in history] == ["Same"]
    assert "data_url" not in history[0]
    assert "prompt" not in history[0]


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


@pytest.mark.parametrize(
    ("title", "category", "domain"),
    [
        ("Минималистичный сайт программиста", "Разработка и IT", "web_software"),
        ("Дизайн упаковки косметики", "Дизайн", "branding_design"),
        ("Юридическая проверка договора", "Бизнес", "legal_finance_business"),
        ("Фуд-фотосъёмка нового блюда", "Фото", "food_hospitality"),
        ("Онлайн-уроки английского", "Обучение", "education"),
        ("Монтаж рекламного видео", "Аудио и видео", "video_audio"),
        ("Смонтирую видео и сделаю моушн-дизайн", "Дизайн", "video_audio"),
        ("Персональная фитнес-тренировка", "Стиль жизни", "fitness_health"),
        ("Керамическая кружка ручной работы", "Хендмейд", "craft_product"),
    ],
)
def test_visual_strategy_routes_multiple_categories(title, category, domain):
    strategy = _visual_strategy({"title": title}, {"category_name": category})

    assert strategy["domain"] == domain
    assert strategy["visual_proof"]
    assert strategy["style"]


@pytest.mark.parametrize(
    ("service", "category", "domain"),
    [
        ("Фотосъёмка готового блюда", "Фото", "food_hospitality"),
        ("Маникюр с дизайном", "Стиль жизни", "beauty_wellness"),
    ],
)
def test_visual_strategy_ignores_generic_fallback_description(service, category, domain):
    autopublish = KworkAutopublishService()
    request = {"service_summary": service, "category_name": category}
    draft = autopublish._fallback_draft(request)

    assert _visual_strategy(draft, request)["domain"] == domain


def test_visual_strategy_matches_short_latin_keywords_as_tokens():
    strategy = _visual_strategy(
        {"title": "Build beautiful menu photography"},
        {"category_name": "Photography"},
    )

    assert strategy["domain"] == "food_hospitality"


def test_web_artifact_mode_forbids_people_devices_and_lifestyle_props():
    strategy = _visual_strategy(
        {"title": "Минималистичный сайт программиста"},
        {"category_name": "Разработка и IT", "scene_mode": "artifact"},
    )

    constraint = _scene_subject_constraints(strategy)
    assert "Do not show a person" in constraint
    assert "monitor" in constraint
    assert "coffee" in constraint
    assert "direct screenshot-like interface" in constraint


def test_portfolio_specs_are_service_specific_and_never_case_study_boards():
    specs = _portfolio_image_specs(
        {"title": "Минималистичный сайт программиста"},
        {"category_name": "Разработка и IT"},
        cover_prompt="One coherent shipped developer portfolio website.",
    )

    assert len(specs) == 5
    prompts = " ".join(item["prompt"] for item in specs)
    assert "Минималистичный сайт программиста" in prompts
    assert "No presentation board" in prompts
    assert "Кириллица для логотипа" not in prompts
    assert "BRAND / БРЕНД" not in prompts


def test_portfolio_specs_cover_requested_counts_up_to_ten():
    specs = _portfolio_image_specs(
        {"title": "Юридическая проверка договора"},
        {"category_name": "Бизнес"},
        count=10,
    )

    assert len(specs) == 10
    assert len({item["title"] for item in specs}) == 10


def test_portfolio_reuse_accepts_only_current_gpt_image_2_assets(tmp_path):
    service = KworkAutopublishService()
    legacy_path = tmp_path / "legacy.png"
    current_path = tmp_path / "current.png"
    legacy_path.write_bytes(b"legacy")
    current_path.write_bytes(b"current")
    draft = {
        "portfolio_required_count": 2,
        "portfolio_assets": [
            {
                "path": str(legacy_path),
                "image_generation": {"requested_model": "gpt-image-1"},
            },
            {
                "path": str(current_path),
                "image_generation": {"requested_model": "gpt-image-2"},
                "quality_gate": {"status": "passed", "ok": True, "score": 9, "issues": []},
                "visual_pipeline_version": kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION,
            },
        ],
    }

    assert service.ensure_portfolio_assets(draft) == [draft["portfolio_assets"][1]]


def test_sanitize_kwork_text_removes_preorder_contact_phrases():
    text = _sanitize_kwork_text("Сложные варианты можно обсудить до заказа. Напишите до заказа, если есть вопросы.")

    assert "до заказа" not in text.lower()
    assert "уточнить в рамках заказа" in text


def test_cover_image_model_is_locked_to_gpt_image_2(monkeypatch):
    monkeypatch.setenv("KWORK_COVER_IMAGE_MODEL", "gpt-image-2")
    assert _image_model() == "gpt-image-2"

    monkeypatch.setenv("KWORK_COVER_IMAGE_MODEL", "gpt-image-1")
    with pytest.raises(ValueError, match="must be gpt-image-2"):
        _image_model()


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


@pytest.mark.asyncio
async def test_generate_image_file_sends_only_gpt_image_2(monkeypatch, tmp_path):
    service = KworkAutopublishService()
    captured: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = "ok"
        headers = {"x-request-id": "req-image-2"}

        @staticmethod
        def json():
            return {"data": [{"b64_json": _png_b64()}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, json):
            assert headers["Authorization"] == "Bearer test-image-key"
            captured.append(json)
            return FakeResponse()

    monkeypatch.setenv("KWORK_COVER_IMAGE_API_KEY", "test-image-key")
    monkeypatch.setenv("KWORK_COVER_IMAGE_BASE_URL", "https://images.example")
    monkeypatch.setenv("KWORK_COVER_IMAGE_API_PREFIX", "/v1")
    monkeypatch.setenv("KWORK_COVER_IMAGE_CONN", "")
    monkeypatch.setenv("KWORK_COVER_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setenv("KWORK_COVER_IMAGE_SIZE", "1536x1024")
    monkeypatch.setenv("KWORK_COVER_IMAGE_QUALITY", "high")
    monkeypatch.setattr(kwork_autopublish.httpx, "AsyncClient", FakeClient)

    metadata = await service._generate_image_file(tmp_path / "cover.png", "one coherent website", purpose="cover")

    assert captured == [
        {
            "model": "gpt-image-2",
            "size": "1536x1024",
            "quality": "high",
            "prompt": "one coherent website",
        }
    ]
    assert metadata["requested_model"] == "gpt-image-2"
    assert metadata["served_model"] == ""
    assert metadata["model_verification"] == "request_only"
    assert metadata["request_id"] == "req-image-2"
    assert metadata["visual_pipeline_version"] == kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION


@pytest.mark.asyncio
async def test_visual_qa_uses_configured_gpt_5_6_terra(monkeypatch, tmp_path):
    from PIL import Image

    service = KworkAutopublishService()
    image_path = tmp_path / "cover.png"
    Image.new("RGB", (768, 512), (20, 40, 60)).save(image_path, "PNG")
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = "ok"
        headers = {"x-request-id": "req-qa"}

        @staticmethod
        def json():
            return {
                "model": "gpt-5.6-terra",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"ok":true,"score":9,"issues":[],"correction":""}',
                            }
                        ]
                    }
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update({"url": url, "headers": headers, "payload": json})
            return FakeResponse()

    monkeypatch.setenv("KWORK_COVER_QA_ENABLED", "true")
    monkeypatch.setenv("KWORK_COVER_QA_PROVIDER", "openai")
    monkeypatch.setenv("KWORK_COVER_QA_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("OPENAI_API_KEY", "test-terra-key")
    monkeypatch.setenv("OPENAI_API_KEYS", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example")
    monkeypatch.setenv("OPENAI_API_PREFIX", "/v1")
    monkeypatch.setattr(kwork_autopublish.httpx, "AsyncClient", FakeClient)

    result = await service.validate_generated_cover(
        image_path,
        {"title": "Минималистичный сайт программиста"},
        {"category_name": "Разработка и IT"},
    )

    assert result["status"] == "passed"
    assert result["review_provider"] == "openai"
    assert result["review_model_requested"] == "gpt-5.6-terra"
    assert captured["url"] == "https://llm.example/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-terra-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5.6-terra"
    assert payload["input"][0]["content"][1]["image_url"].startswith("data:image/")
    assert "web/software artifact mode contains any person" in payload["input"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_visual_qa_rejects_malformed_boolean_response(monkeypatch, tmp_path):
    from PIL import Image

    service = KworkAutopublishService()
    image_path = tmp_path / "cover.png"
    Image.new("RGB", (768, 512), (20, 40, 60)).save(image_path, "PNG")

    class FakeResponse:
        status_code = 200
        text = "ok"
        headers: dict[str, str] = {}

        @staticmethod
        def json():
            return {
                "model": "gpt-5.6-terra",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"ok":"false","score":9,"issues":[],"correction":""}',
                            }
                        ]
                    }
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, headers, json):
            return FakeResponse()

    monkeypatch.setenv("KWORK_COVER_QA_ENABLED", "true")
    monkeypatch.setenv("KWORK_COVER_QA_PROVIDER", "openai")
    monkeypatch.setenv("KWORK_COVER_QA_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("OPENAI_API_KEY", "test-terra-key")
    monkeypatch.setenv("OPENAI_API_KEYS", "")
    monkeypatch.setattr(kwork_autopublish.httpx, "AsyncClient", FakeClient)

    result = await service.validate_generated_cover(
        image_path,
        {"title": "Минималистичный сайт программиста"},
        {"category_name": "Разработка и IT"},
    )

    assert result["status"] == "unavailable"
    assert result["ok"] is False
    assert "must be boolean" in result["detail"]


@pytest.mark.asyncio
async def test_generate_cover_failure_returns_error_without_local_art(monkeypatch):
    service = KworkAutopublishService()

    async def fake_prompt(_draft, _request):
        return "one coherent finished product", "test"

    async def fail_image(*_args, **_kwargs):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setenv("KWORK_COVER_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setattr(service, "build_cover_prompt", fake_prompt)
    monkeypatch.setattr(service, "_generate_image_file", fail_image)

    result = await service.generate_cover(
        {"title": "Минималистичный сайт"},
        {"category_name": "Разработка и IT"},
    )

    assert result["status"] == "error"
    assert result["code"] == "gpt_image_2_generation_failed"
    assert result["requested_image_model"] == "gpt-image-2"
    assert "path" not in result
    assert "generated_local_fallback" not in str(result)


@pytest.mark.asyncio
async def test_generate_cover_retries_once_after_visual_qa_rejection(monkeypatch, tmp_path):
    from PIL import Image

    service = KworkAutopublishService()
    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)
    prompts: list[str] = []
    gates = [
        {"status": "rejected", "ok": False, "score": 4, "issues": ["generic abstract poster"], "correction": "show one real shipped website"},
        {"status": "passed", "ok": True, "score": 9, "issues": [], "correction": ""},
    ]

    async def fake_prompt(_draft, request):
        request["_cover_visual_strategy"] = _visual_strategy(
            {"title": "Минималистичный сайт программиста"},
            {"category_name": "Разработка и IT"},
        )
        return "one finished developer website", "test"

    async def fake_generate(path, prompt, *, purpose):
        assert purpose == "cover"
        prompts.append(prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (768, 512), (20, 40, 60)).save(path, "PNG")
        return {
            "requested_model": "gpt-image-2",
            "served_model": "",
            "model_verification": "request_only",
            "size": "1536x1024",
            "quality": "high",
        }

    async def fake_validate(_path, _draft, _request):
        return gates.pop(0)

    monkeypatch.setattr(service, "build_cover_prompt", fake_prompt)
    monkeypatch.setattr(service, "_generate_image_file", fake_generate)
    monkeypatch.setattr(service, "validate_generated_cover", fake_validate)

    result = await service.generate_cover(
        {"title": "Минималистичный сайт программиста", "description": "Портфолио"},
        {"category_name": "Разработка и IT"},
    )

    assert result["status"] == "generated"
    assert result["quality_gate"]["status"] == "passed"
    assert result["prompt_source"] == "test+qa_retry"
    assert len(prompts) == 2
    assert "show one real shipped website" in prompts[1]
    assert result["image_generation"]["qa_retry"] is True


@pytest.mark.asyncio
async def test_generate_cover_does_not_rerender_when_visual_qa_is_unavailable(monkeypatch, tmp_path):
    from PIL import Image

    service = KworkAutopublishService()
    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)
    generated_paths: list[Path] = []

    async def fake_prompt(_draft, _request):
        return "one direct finished website", "test"

    async def fake_generate(path, _prompt, *, purpose):
        assert purpose == "cover"
        generated_paths.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (768, 512), (20, 40, 60)).save(path, "PNG")
        return {
            "requested_model": "gpt-image-2",
            "served_model": "",
            "model_verification": "request_only",
            "size": "1536x1024",
            "quality": "high",
            "visual_pipeline_version": kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION,
        }

    async def fake_validate(_path, _draft, _request):
        return {
            "status": "unavailable",
            "ok": False,
            "score": None,
            "issues": ["visual QA provider is unavailable"],
        }

    monkeypatch.setattr(service, "build_cover_prompt", fake_prompt)
    monkeypatch.setattr(service, "_generate_image_file", fake_generate)
    monkeypatch.setattr(service, "validate_generated_cover", fake_validate)

    result = await service.generate_cover(
        {"title": "Минималистичный сайт"},
        {"category_name": "Разработка и IT"},
    )

    assert result["status"] == "error"
    assert result["code"] == "cover_quality_gate_unavailable"
    assert len(generated_paths) == 1
    assert not generated_paths[0].exists()


@pytest.mark.asyncio
async def test_generate_portfolio_assets_uses_service_specific_gpt_image_prompts(monkeypatch, tmp_path):
    from PIL import Image

    service = KworkAutopublishService()
    monkeypatch.setattr(kwork_autopublish, "PROPOSAL_ASSETS_DIR", tmp_path)
    captured_prompts: list[str] = []
    qa_prompts: list[str] = []
    qa_modes: list[str] = []

    async def fake_generate(path, prompt, *, purpose):
        captured_prompts.append(prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (768, 512), (20, 40, 60)).save(path, "PNG")
        return {
            "requested_model": "gpt-image-2",
            "served_model": "gpt-image-2",
            "model_verification": "response_reported",
            "size": "1536x1024",
            "quality": "high",
        }

    async def fake_validate(_path, _draft, _request, *, purpose="cover", expected_prompt=""):
        assert purpose == "portfolio"
        qa_prompts.append(expected_prompt)
        qa_modes.append(str(_request["_cover_visual_strategy"]["scene_mode"]))
        return {"status": "passed", "ok": True, "score": 9, "issues": []}

    monkeypatch.setattr(service, "_generate_image_file", fake_generate)
    monkeypatch.setattr(service, "validate_generated_cover", fake_validate)
    assets = await service.generate_portfolio_assets(
        {"category_id": 25, "title": "Минималистичный сайт программиста"},
        {"category_name": "Разработка и IT", "service_summary": "сайт-портфолио программиста"},
        cover_prompt="One polished shipped developer portfolio website.",
    )

    assert len(assets) == 5
    assert len(captured_prompts) == 5
    assert len(qa_prompts) == 5
    assert qa_modes == ["artifact", "artifact", "outcome", "process", "artifact"]
    assert all(Path(item["path"]).is_file() for item in assets)
    assert all(item["image_generation"]["requested_model"] == "gpt-image-2" for item in assets)
    assert all(item["quality_gate"]["status"] == "passed" for item in assets)
    assert all(
        item["visual_pipeline_version"] == kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION
        for item in assets
    )
    assert all("Минималистичный сайт программиста" in prompt for prompt in captured_prompts)
    assert all("No presentation board" in prompt for prompt in captured_prompts)
    assert all("Кириллица для логотипа" not in prompt for prompt in captured_prompts)


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
        "cover_image": {
            "path": str(cover),
            "requested_image_model": "gpt-image-2",
            "visual_pipeline_version": kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION,
            "quality_gate": {"status": "passed", "ok": True, "score": 9, "issues": []},
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
    portfolio_assets = []
    for index in range(5):
        path = tmp_path / f"portfolio-{index + 1}.png"
        path.write_bytes(base64.b64decode(_png_b64()))
        portfolio_assets.append(
            {
                "title": f"Работа {index + 1}",
                "path": str(path),
                "image_generation": {"requested_model": "gpt-image-2"},
                "quality_gate": {"status": "passed", "ok": True, "score": 9, "issues": []},
                "visual_pipeline_version": kwork_autopublish.KWORK_VISUAL_PIPELINE_VERSION,
            }
        )

    draft = {
        "category_id": 25,
        "title": "Адаптирую логотип под кириллицу",
        "description": "Описание",
        "price": 1250,
        "work_time": 3,
        "attribute_manifest": {"controls": [{"name": "attribute[1624]"}], "unresolved_required": []},
        "attribute_selection": {"attribute[1624]": 401928},
        "portfolio_assets": portfolio_assets,
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


def test_live_preflight_blocks_legacy_generated_cover(monkeypatch):
    service = KworkAutopublishService()
    monkeypatch.setenv("KWORK_COVER_QA_ENABLED", "true")

    result = service.live_preflight(
        {
            "category_id": 41,
            "title": "Сделаю сайт",
            "description": "Описание",
            "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
            "attribute_selection": {"attribute[208]": 3587},
            "cover_image_path": "C:/tmp/legacy-cover.png",
            "cover_image": {
                "path": "C:/tmp/legacy-cover.png",
                "requested_image_model": "gpt-image-1",
            },
        }
    )

    assert result["ok"] is False
    assert "cover_visual_outdated" in result["missing"]


def test_live_preflight_blocks_path_only_cover_without_provenance(monkeypatch):
    service = KworkAutopublishService()
    monkeypatch.setenv("KWORK_COVER_QA_ENABLED", "true")

    result = service.live_preflight(
        {
            "category_id": 41,
            "title": "Сделаю сайт",
            "description": "Описание",
            "attribute_manifest": {"controls": [{"name": "attribute[208]"}], "unresolved_required": []},
            "attribute_selection": {"attribute[208]": 3587},
            "cover_image_path": "C:/tmp/path-only-cover.png",
        }
    )

    assert result["ok"] is False
    assert "cover_visual_outdated" in result["missing"]


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
