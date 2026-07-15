from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.platforms.kwork_listing import (
    KworkWebListingClient,
    _CLASSIFICATION_CACHE,
    _CLASSIFICATION_INFLIGHT,
    form_pairs_to_structured_payload,
    is_kwork_manual_verification_page,
    manual_verification_response,
    parse_classification_html,
    parse_new_form_snapshot,
    parse_save_kwork_response,
)


def test_parse_classification_html_groups_radio_and_checkbox_controls():
    html = """
    <div>
      <input type="radio" name="attribute[208]" id="a3587" value="3587">
      <label for="a3587">Чат-боты</label>
      <input type="radio" name="attribute[208]" id="a7352" value="7352">
      <label for="a7352">Скрипты</label>
      <input type="hidden" name="new_custom_attribute[208]" value="">
      <label><input type="checkbox" name="attribute[3610][]" value="3612">Telegram</label>
      <label><input type="checkbox" name="attribute[3610][]" value="5273361">MAX</label>
    </div>
    """

    controls = parse_classification_html(html)

    by_name = {item["name"]: item for item in controls}
    assert by_name["attribute[208]"]["type"] == "radio"
    assert by_name["attribute[208]"]["multiple"] is False
    assert by_name["attribute[208]"]["custom_name"] == "new_custom_attribute[208]"
    assert [item["label"] for item in by_name["attribute[208]"]["options"]] == ["Чат-боты", "Скрипты"]
    assert by_name["attribute[3610][]"]["type"] == "checkbox"
    assert by_name["attribute[3610][]"]["multiple"] is True
    assert [item["id"] for item in by_name["attribute[3610][]"]["options"]] == [3612, 5273361]
    assert by_name["new_custom_attribute[208]"]["type"] == "custom_text"
    assert by_name["new_custom_attribute[208]"]["options"] == []


def test_parse_classification_html_keeps_text_controls():
    html = """
    <div>
      <input type="text" name="attribute[900]" value="FastAPI" placeholder="Framework" required>
      <textarea name="attribute[901]" placeholder="Details">API bot</textarea>
    </div>
    """

    controls = parse_classification_html(html)

    by_name = {item["name"]: item for item in controls}
    assert by_name["attribute[900]"]["type"] == "text"
    assert by_name["attribute[900]"]["value"] == "FastAPI"
    assert by_name["attribute[900]"]["placeholder"] == "Framework"
    assert by_name["attribute[900]"]["required"] is True
    assert by_name["attribute[901]"]["type"] == "textarea"
    assert by_name["attribute[901]"]["value"] == "API bot"


def test_parse_classification_html_does_not_use_option_as_question():
    html = """
    <div>
      <input type="radio" name="attribute[27]" id="office" value="712">
      <label for="office">Макросы для Office</label>
    </div>
    """

    controls = parse_classification_html(html)

    control = controls[0]
    assert control["options"][0]["label"] == "Макросы для Office"
    assert control["question"] != "Макросы для Office"


def test_parse_classification_html_uses_real_group_question():
    html = """
    <div>
      <div class="field-label">Тип услуги</div>
      <input type="radio" name="attribute[27]" id="office" value="712">
      <label for="office">Макросы для Office</label>
    </div>
    """

    controls = parse_classification_html(html)

    assert controls[0]["question"] == "Тип услуги"
    assert controls[0]["options"][0]["label"] == "Макросы для Office"


def test_parse_new_form_snapshot_extracts_hidden_fields():
    html = """
    <form class="js-kwork-save-form" action="/save_kwork" method="post">
      <input type="hidden" name="csrftoken" value="csrf-1">
      <input type="hidden" name="draft_id" value="777">
      <input type="hidden" name="lang" value="ru">
    </form>
    <script>window.draftId=777;</script>
    """

    snapshot = parse_new_form_snapshot(html, "https://kwork.ru/new")

    assert snapshot["ok"] is True
    assert snapshot["form_action"] == "https://kwork.ru/save_kwork"
    assert snapshot["csrftoken"] == "csrf-1"
    assert snapshot["draft_id"] == "777"
    assert snapshot["hidden_fields"]["lang"] == "ru"


@pytest.mark.asyncio
async def test_open_new_retries_transport_failure(monkeypatch):
    attempts = 0

    class FakeResponse:
        status_code = 200
        url = "https://kwork.ru/new"
        text = (
            '<form class="js-kwork-save-form" action="/save_kwork">'
            '<input type="hidden" name="csrftoken" value="csrf-1">'
            '<input type="hidden" name="draft_id" value="777">'
            "</form>"
        )

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError(
                    "server disconnected",
                    request=httpx.Request("GET", "https://kwork.ru/new"),
                )
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("src.platforms.kwork_listing.kwork_http_proxy_url", lambda **kwargs: None)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).open_new()

    assert result["ok"] is True
    assert attempts == 2


def test_parse_save_kwork_response_maps_success_and_errors():
    success = parse_save_kwork_response(200, '{"result":"success","redirectUrl":"/manage_kworks"}')
    failed = parse_save_kwork_response(200, '{"code":202,"errors":["title required"]}')
    html = parse_save_kwork_response(200, "<html>login</html>", "https://kwork.ru/login")

    assert success["ok"] is True
    assert success["redirect_url"] == "/manage_kworks"
    assert failed["ok"] is False
    assert failed["code"] == "202"
    assert failed["errors"] == ["title required"]
    assert html["code"] == "login_required"


def test_kwork_manual_verification_page_detects_smartcaptcha():
    html = """
    <html>
      <script src="https://smartcaptcha.cloud.yandex.ru/captcha.js"></script>
      <div class="smart-captcha"></div>
      <p>Подтвердите, что вы не робот</p>
    </html>
    """

    assert is_kwork_manual_verification_page(html, "https://kwork.ru/")
    result = parse_save_kwork_response(403, html, "https://kwork.ru/")
    assert result["ok"] is False
    assert result["code"] == "manual_verification_required"


def test_kwork_manual_verification_detects_real_russian_robot_text():
    html = """
    <html>
      <script src="https://smartcaptcha.cloud.yandex.ru/captcha.js"></script>
      <p>Подтвердите, что вы не робот</p>
    </html>
    """

    result = manual_verification_response(403, html, "https://kwork.ru/not_access.php")

    assert result is not None
    assert result["code"] == "manual_verification_required"
    assert "Подтвердите, что вы не робот" in result["evidence"]["weak_matches"]


def test_kwork_manual_verification_ignores_weak_text_on_normal_new_form():
    html = """
    <html>
      <body>
        <form class="js-kwork-save-form" action="/save_kwork">
          <input type="hidden" name="csrftoken" value="csrf-1">
          <input type="hidden" name="draft_id" value="777">
          <input name="title" value="">
          <textarea name="description"></textarea>
        </form>
        <p>Мы ограничиваем автоматические скрипты при большой нагрузке.</p>
      </body>
    </html>
    """

    assert not is_kwork_manual_verification_page(html, "https://kwork.ru/new")
    assert manual_verification_response(200, html, "https://kwork.ru/new") is None


def test_kwork_manual_verification_ignores_global_smartcaptcha_script_on_normal_page():
    html = """
    <html>
      <head>
        <script>
          window.appData = {
            isYandexSmartCaptcha: true,
            yandexSmartCaptchaToken: "smart-token",
            yandexSmartCaptchaPublicKey: "key"
          };
        </script>
        <script src="https://smartcaptcha.yandexcloud.net/captcha.js?render=onload"></script>
      </head>
      <body>
        <h1>Предложить услугу</h1>
        <div id="app"></div>
      </body>
    </html>
    """

    assert not is_kwork_manual_verification_page(html, "https://kwork.ru/new_offer?project=3202311")
    assert manual_verification_response(200, html, "https://kwork.ru/new_offer?project=3202311") is None


def test_kwork_manual_verification_detects_not_access_weak_challenge():
    html = """
    <html>
      <body>
        <p>Подтвердите, что вы не робот.</p>
        <p>Запросы похожи на автоматические скрипты при большой нагрузке.</p>
      </body>
    </html>
    """

    result = manual_verification_response(403, html, "https://kwork.ru/not_access.php")

    assert result is not None
    assert result["code"] == "manual_verification_required"
    assert result["evidence"]["challenge_url"] is True
    assert result["evidence"]["challenge_status"] is True


def test_form_pairs_to_structured_payload_matches_kwork_save_shape():
    payload = form_pairs_to_structured_payload(
        [
            ("title", "Сделаю бота"),
            ("attribute[208]", "3587"),
            ("attribute[3610][]", "3612"),
            ("attribute[3610][]", "5273361"),
            ("bundle_extra_standard_value[category][190]", "1"),
            ("bundle_extra_standard_value[category][191]", "1"),
            ("faq[0][question]", "Что нужно?"),
            ("faq[0][answer]", "ТЗ и доступы."),
            ("first_photo_json", '{"success":true,"data":{"src":"https://cdn/cover.png","hash":"h1"}}'),
            ("first_photo_path", "https://cdn/cover.png"),
            ("first-kwork-photo", "null"),
            ("first-kwork-photo-size[]", '{"x":0,"y":0,"w":1200,"h":800}'),
            (
                "portfolio",
                '[{"draftHash":1,"cover":{"idPortfolioMedia":77,"type":"image"},'
                '"items":[{"id":77,"portfolio_type":"photo","position":0}]}]',
            ),
        ]
    )

    assert payload["title"] == "Сделаю бота"
    assert payload["attribute"]["208"] == "3587"
    assert payload["attribute"]["3610"] == ["3612", "5273361"]
    assert payload["bundle_extra_standard_value"]["category"] == {"190": "1", "191": "1"}
    assert payload["faq"][0]["question"] == "Что нужно?"
    assert payload["first_photo_json"]["data"]["src"] == "https://cdn/cover.png"
    assert payload["first_photo_path"] == "https://cdn/cover.png"
    assert payload["first-kwork-photo"] is None
    assert payload["first-kwork-photo-size"]["w"] == 1200
    assert payload["portfolio"][0]["cover"]["idPortfolioMedia"] == 77


@pytest.mark.asyncio
async def test_build_attribute_manifest_loads_selected_children(monkeypatch):
    calls: list[int | None] = []

    async def fake_load(self, category_id, attribute_id=None, lang="ru"):
        calls.append(attribute_id)
        if attribute_id is None:
            controls = parse_classification_html(
                '<input type="radio" name="attribute[208]" id="bot" value="3587" data-has-child="1">'
                '<label for="bot">Чат-боты</label>'
            )
        elif attribute_id == 3587:
            controls = parse_classification_html(
                '<label><input type="checkbox" name="attribute[3610][]" value="3612">Telegram</label>'
            )
        else:
            controls = []
        return {
            "success": True,
            "category_id": category_id,
            "attribute_id": attribute_id,
            "controls": controls,
            "count": len(controls),
            "selectedCount": 0,
            "raw_keys": [],
        }

    monkeypatch.setattr(KworkWebListingClient, "load_classification", fake_load)

    manifest = await KworkWebListingClient().build_attribute_manifest(
        41,
        classifier_id=3587,
        selection={"attribute[208]": 3587},
    )

    assert calls == [None, 3587]
    assert [item["name"] for item in manifest["controls"]] == ["attribute[208]", "attribute[3610][]"]
    assert manifest["selected"] == {"attribute[208]": 3587}


@pytest.mark.asyncio
async def test_build_attribute_manifest_drops_stale_child_selection_after_parent_change(monkeypatch):
    calls: list[int | None] = []

    async def fake_load(self, category_id, attribute_id=None, lang="ru"):
        calls.append(attribute_id)
        if attribute_id is None:
            controls = parse_classification_html(
                '<input type="radio" name="attribute[208]" id="bot" value="3587">'
                '<label for="bot">Bots</label>'
                '<input type="radio" name="attribute[208]" id="other" value="9999">'
                '<label for="other">Other</label>'
            )
        elif attribute_id == 3587:
            controls = parse_classification_html(
                '<label><input type="checkbox" name="attribute[3610][]" value="3612">Telegram</label>'
            )
        else:
            controls = []
        return {
            "success": True,
            "category_id": category_id,
            "attribute_id": attribute_id,
            "controls": controls,
            "count": len(controls),
            "selectedCount": 0,
            "raw_keys": [],
        }

    monkeypatch.setattr(KworkWebListingClient, "load_classification", fake_load)

    manifest = await KworkWebListingClient().build_attribute_manifest(
        41,
        selection={"attribute[208]": 9999, "attribute[3610][]": [3612]},
    )

    assert calls == [None, 9999]
    assert manifest["selected"] == {"attribute[208]": 9999}
    assert "attribute[3610][]" in manifest["selection_validation"]["issues"]["unknown_fields"]
    assert manifest["manifest_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_build_attribute_manifest_probes_selected_option_without_has_child_marker(monkeypatch):
    calls: list[int | None] = []

    async def fake_load(self, category_id, attribute_id=None, lang="ru"):
        calls.append(attribute_id)
        if attribute_id is None:
            controls = parse_classification_html(
                '<input type="radio" name="attribute[1624]" id="branding" value="401976">'
                '<label for="branding">Фирменный стиль</label>'
            )
        else:
            controls = parse_classification_html(
                '<input type="radio" name="attribute[401977]" id="guide" value="401989">'
                '<label for="guide">Гайдлайн</label>'
            )
        return {
            "success": True,
            "category_id": category_id,
            "attribute_id": attribute_id,
            "controls": controls,
            "count": len(controls),
            "selectedCount": 0,
            "raw_keys": [],
        }

    monkeypatch.setattr(KworkWebListingClient, "load_classification", fake_load)

    manifest = await KworkWebListingClient().build_attribute_manifest(
        25,
        selection={"attribute[1624]": 401976},
    )

    assert calls == [None, 401976]
    assert [item["name"] for item in manifest["controls"]] == ["attribute[1624]", "attribute[401977]"]


@pytest.mark.asyncio
async def test_build_attribute_manifest_applies_disable_ids(monkeypatch):
    async def fake_load(self, category_id, attribute_id=None, lang="ru"):
        controls = parse_classification_html(
            '<label><input type="checkbox" name="attribute[3610][]" value="3612">Telegram</label>'
            '<label><input type="checkbox" name="attribute[3610][]" value="5273361">MAX</label>'
        )
        return {
            "success": True,
            "category_id": category_id,
            "attribute_id": attribute_id,
            "controls": controls,
            "disableIds": {"3610": [5273361]},
            "selectedChilds": {"3610": [3612]},
            "attributeResolutions": {"3610": {"max": 1}},
            "count": len(controls),
            "selectedCount": 0,
            "raw_keys": [],
        }

    monkeypatch.setattr(KworkWebListingClient, "load_classification", fake_load)

    manifest = await KworkWebListingClient().build_attribute_manifest(41)

    control = manifest["controls"][0]
    by_id = {item["id"]: item for item in control["options"]}
    assert by_id[3612]["disabled"] is False
    assert by_id[5273361]["disabled"] is True
    assert control["disabled"] is False
    assert manifest["metadata"]["disableIds"] == [3610, 5273361]
    assert manifest["metadata"]["selectedChilds"] == {"3610": [3612]}


@pytest.mark.asyncio
async def test_load_classification_returns_structured_non_json_error(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "<html><form action='/login'>login</form></html>"
        content = text.encode("utf-8")
        url = "https://kwork.ru/login"

        def raise_for_status(self):
            return None

        def json(self):
            raise json.JSONDecodeError("not json", self.text, 0)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"slrememberme": "cookie"}).load_classification(41)

    assert result["success"] is False
    assert result["ok"] is False
    assert result["code"] == "login_required"
    assert result["controls"] == []


@pytest.mark.asyncio
async def test_load_classification_deduplicates_inflight_and_caches_success(monkeypatch):
    _CLASSIFICATION_CACHE.clear()
    _CLASSIFICATION_INFLIGHT.clear()
    calls = 0

    async def fake_load(self, category_id, attribute_id=None, lang="ru"):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {
            "success": True,
            "category_id": category_id,
            "attribute_id": attribute_id,
            "lang": lang,
            "controls": [{"name": "attribute[1]", "options": []}],
            "raw_keys": [],
        }

    monkeypatch.setattr(KworkWebListingClient, "_load_classification_uncached", fake_load)
    first_client = KworkWebListingClient()
    second_client = KworkWebListingClient()

    first, second = await asyncio.gather(
        first_client.load_classification(25, attribute_id=401928),
        second_client.load_classification(25, attribute_id=401928),
    )
    cached = await first_client.load_classification(25, attribute_id=401928)

    assert calls == 1
    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "miss"
    assert cached["cache_status"] == "hit"
    assert cached["controls"][0]["name"] == "attribute[1]"


@pytest.mark.asyncio
async def test_save_kwork_encodes_ordered_pairs(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"result":"success","redirectUrl":"/manage_kworks"}'
        url = "https://kwork.ru/save_kwork"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["content"] = kwargs.get("content")
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).save_kwork(
        [("attribute[3610][]", "3612"), ("attribute[3610][]", "5273361")]
    )

    assert result["ok"] is True
    assert captured["url"] == "/save_kwork"
    assert captured["content"].count(b"attribute%5B3610%5D%5B%5D=") == 2


@pytest.mark.asyncio
async def test_save_kwork_json_posts_structured_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"result":"success","redirectUrl":"/manage_kworks"}'
        url = "https://kwork.ru/save_kwork"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["headers"] = kwargs.get("headers")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).save_kwork_json(
        [
            ("attribute[3610][]", "3612"),
            ("attribute[3610][]", "5273361"),
            ("first-kwork-photo", "null"),
        ]
    )

    assert result["ok"] is True
    assert captured["url"] == "/save_kwork"
    assert captured["json"]["attribute"]["3610"] == ["3612", "5273361"]
    assert captured["json"]["first-kwork-photo"] is None
    assert "Content-Type" not in captured["headers"]


@pytest.mark.asyncio
async def test_upload_cover_requires_returned_image_path(monkeypatch, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    class FakeResponse:
        status_code = 200
        url = "https://kwork.ru/temp-image-upload"

        def raise_for_status(self):
            return None

        def json(self):
            return {"hash": "h1"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).upload_cover(cover, category_id=41)

    assert result["ok"] is False
    assert result["code"] == "cover_upload_missing_path"
    assert result["first_photo_hash"] == "h1"


@pytest.mark.asyncio
async def test_upload_cover_accepts_nested_kwork_data_response(monkeypatch, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    class FakeResponse:
        status_code = 200
        url = "https://kwork.ru/temp-image-upload"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {"id": 123, "name": "52/123.png", "src": "https://cdn/52/123.png", "hash": "h1"},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).upload_cover(cover, category_id=41)

    assert result["ok"] is True
    assert result["first_photo_path"] == "https://cdn/52/123.png"
    assert result["first_photo_json"]["data"]["src"] == "https://cdn/52/123.png"
    assert result["first_photo_hash"] == "h1"


@pytest.mark.asyncio
async def test_upload_cover_retries_transport_failure(monkeypatch, tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    attempts = 0

    class FakeResponse:
        status_code = 200
        url = "https://kwork.ru/temp-image-upload"

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True, "data": {"src": "https://cdn/cover.png", "hash": "h1"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ReadError("connection dropped", request=httpx.Request("POST", self_url))
            return FakeResponse()

    self_url = "https://kwork.ru/temp-image-upload"
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("src.platforms.kwork_listing.kwork_http_proxy_url", lambda **kwargs: None)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).upload_cover(cover, category_id=41)

    assert result["ok"] is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_upload_portfolio_image_returns_media_contract(monkeypatch, tmp_path):
    image = tmp_path / "work.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    captured = {}

    class FakeResponse:
        status_code = 200
        url = "https://kwork.ru/portfolio/upload_image"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "id": 77,
                    "hash": "portfolio-hash",
                    "url": "https://cdn/work.jpg",
                    "urlBig": "https://cdn/work-big.jpg",
                    "type": "jpg",
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["data"] = kwargs["data"]
            captured["files"] = kwargs["files"]
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).upload_portfolio_image(
        image,
        known_hashes=["cover-hash"],
    )

    assert result == {
        "ok": True,
        "raw": FakeResponse().json(),
        "id": 77,
        "hash": "portfolio-hash",
        "url": "https://cdn/work.jpg",
        "urlBig": "https://cdn/work-big.jpg",
        "type": "jpg",
        "crop": None,
    }
    assert captured["url"] == "/portfolio/upload_image"
    assert captured["data"] == {"hashes[0]": "cover-hash"}
    assert captured["files"]["file"][0] == "work.png"


@pytest.mark.asyncio
async def test_verify_saved_kwork_checks_redirect_and_manage_pages(monkeypatch):
    checked_urls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, url, text):
            self.url = f"https://kwork.ru{url}" if url.startswith("/") else url
            self.text = text

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            checked_urls.append(url)
            text = "empty"
            if url == "/manage_kworks?group=moderated":
                text = '<div class="card"><a href="/new?draft_id=777">Создам чат бота для бизнеса</a></div>'
            return FakeResponse(url, text)

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).verify_saved_kwork(
        {"ok": True, "redirect_url": "/new?success=1"},
        {"title": "Создам чат бота для бизнеса", "draft_id": "777"},
    )

    assert result["ok"] is True
    assert result["code"] == "verified"
    assert result["status_group"] == "moderated"
    assert result["matched_by"] == ["draft_id", "title"]
    assert checked_urls == ["/new?success=1", "/manage_kworks?group=moderated"]


@pytest.mark.asyncio
async def test_verify_saved_kwork_requires_same_card_match(monkeypatch):
    class FakeResponse:
        status_code = 200

        def __init__(self, url, text):
            self.url = f"https://kwork.ru{url}" if url.startswith("/") else url
            self.text = text

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(
                url,
                '<div class="card"><a href="/new?draft_id=777">Чужой кворк</a></div>'
                '<div class="card">Создам чат бота для бизнеса</div>',
            )

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    result = await KworkWebListingClient({"PHPSESSID": "abc"}).verify_saved_kwork(
        {"ok": True, "redirect_url": "/manage_kworks"},
        {"title": "Создам чат бота для бизнеса", "draft_id": "777"},
    )

    assert result["ok"] is False
    assert result["code"] == "not_found_after_save"
