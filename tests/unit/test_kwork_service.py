"""Unit tests for KworkService — session management and authorization.

Tests cover:
- Session Hub priority (Requirement 7.1)
- SESSION_HUB_REQUIRED strict mode (Requirement 7.2)
- Email/password fallback (Requirement 7.3)
- Missing credentials warning (Requirement 7.4)
- Retry with 5s delay on 401 (Requirement 7.5)
- Final failure returns None (Requirement 7.6)
- Max 3 session resets per cycle (Requirement 7.7)
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from yarl import URL

from src.platforms.kwork import (
    KworkService,
    KworkStateDataParser,
    env_kwork_web_cookies,
    manual_kwork_web_cookies,
    parse_cookie_env,
)


@pytest.fixture
def service():
    """Create a fresh KworkService instance for each test."""
    svc = KworkService()
    svc.__post_init__()
    return svc


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure clean environment for each test."""
    monkeypatch.delenv("SESSION_HUB_URL", raising=False)
    monkeypatch.delenv("SESSION_HUB_REQUIRED", raising=False)
    monkeypatch.delenv("KWORK_EMAIL", raising=False)
    monkeypatch.delenv("KWORK_PASSWORD", raising=False)
    monkeypatch.delenv("KWORK_PHONE", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)


class TestGetApiSessionHubPriority:
    """Requirement 7.1: Session Hub is the priority auth source."""

    @pytest.mark.asyncio
    async def test_session_hub_success(self, service, monkeypatch):
        """When Session Hub returns valid cookies, API client is created from them."""
        monkeypatch.setenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")
        monkeypatch.setenv("KWORK_PHONE", "1234")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok",
            "count": 2,
            "cookies": [
                {"name": "PHPSESSID", "value": "abc123"},
                {"name": "rememberMe", "value": "xyz789"},
            ],
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_kwork = MagicMock()
        mock_kwork._session = None
        mock_kwork.session = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", return_value=mock_kwork) as kwork_cls:
                api = await service.get_api()

        assert api is mock_kwork
        assert kwork_cls.call_args.kwargs["phone_last"] == "1234"
        assert "phone" not in kwork_cls.call_args.kwargs

    @pytest.mark.asyncio
    async def test_transport_recovery_rebuilds_cached_email_client_on_live_proxy(self, service, monkeypatch):
        """A dead cached proxy must be replaced from the current VPNTE instance."""
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")

        failed_api = MagicMock()
        failed_api.close = AsyncMock()
        failed_api._psr_auth_mode = "email+password"
        replacement_api = MagicMock()

        rotator = MagicMock()
        rotator.next.return_value = "http://127.0.0.1:18100"
        monkeypatch.setattr("src.platforms.kwork_ext.get_proxy_rotator", lambda: rotator)

        with patch("kwork.Kwork", return_value=replacement_api) as kwork_cls:
            service._api = failed_api
            recovered = await service._recover_transport(failed_api, OSError("[WinError 64] dead proxy"))

        assert recovered is replacement_api
        assert service._api is replacement_api
        assert kwork_cls.call_args.kwargs["proxy"] == "http://127.0.0.1:18100"
        failed_api.close.assert_awaited_once()
        assert replacement_api._psr_proxy_url == "http://127.0.0.1:18100"

    @pytest.mark.asyncio
    async def test_transport_recovery_preserves_session_hub_cookies(self, service, monkeypatch):
        """Cookie-only recovery keeps the live Session Hub cookie jar."""
        monkeypatch.delenv("KWORK_EMAIL", raising=False)
        monkeypatch.delenv("KWORK_PASSWORD", raising=False)
        failed_api = MagicMock()
        failed_api._psr_auth_mode = "cookie-only"
        replacement_api = MagicMock()
        service._api = failed_api
        service._session_hub_cookies = {"PHPSESSID": "cached"}
        service._session_hub_cookies_at = time.monotonic()

        rotator = MagicMock()
        rotator.next.return_value = "http://127.0.0.1:18101"
        monkeypatch.setattr("src.platforms.kwork_ext.get_proxy_rotator", lambda: rotator)
        monkeypatch.setattr(service, "_apply_cookies_to_api", MagicMock(return_value=True))
        monkeypatch.setattr(service, "_fetch_session_hub_cookies", AsyncMock(return_value={"PHPSESSID": "fresh"}))

        with patch("kwork.Kwork", return_value=replacement_api):
            recovered = await service._recover_transport(failed_api)

        assert recovered is replacement_api
        service._apply_cookies_to_api.assert_called_once_with(replacement_api, {"PHPSESSID": "fresh"})
        assert replacement_api._psr_auth_mode == "cookie-only"

    @pytest.mark.asyncio
    async def test_session_hub_cookie_only_success(self, service, monkeypatch):
        """Session Hub cookies are enough even when email/password are absent."""
        monkeypatch.setenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok",
            "count": 1,
            "cookies": [{"name": "PHPSESSID", "value": "abc123"}],
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_kwork = MagicMock()
        mock_kwork._session = None
        mock_kwork.session = None

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", return_value=mock_kwork) as kwork_cls:
                api = await service.get_api()

        assert api is mock_kwork
        assert kwork_cls.call_args.kwargs["login"] == ""
        assert kwork_cls.call_args.kwargs["password"] == ""

    @pytest.mark.asyncio
    async def test_token_api_bypasses_cookie_only_client(self, service, monkeypatch):
        """Token-required endpoints can fall back to email/password when Session Hub is cookie-only."""
        monkeypatch.setenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ok",
            "count": 1,
            "cookies": [{"name": "PHPSESSID", "value": "abc123"}],
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        cookie_api = MagicMock()
        cookie_api._session = None
        cookie_api.session = None
        token_api = MagicMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", side_effect=[cookie_api, token_api]) as kwork_cls:
                api = await service.get_api()
                token = await service.get_token_api()

        assert api is cookie_api
        assert api._psr_auth_mode == "email+cookies"
        assert token is api
        assert kwork_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_token_api_uses_email_password_after_cookie_only_cache(self, service, monkeypatch):
        """A cached cookie-only client is not reused for token-required endpoints."""
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")

        cookie_api = MagicMock()
        cookie_api._psr_auth_mode = "cookie-only"
        service._api = cookie_api
        token_api = MagicMock()

        with patch("kwork.Kwork", return_value=token_api):
            token = await service.get_token_api()

        assert token is token_api
        assert service._api is cookie_api
        assert service._token_api is token_api
        assert token_api._psr_auth_mode == "email+password"

    def test_web_state_project_parser_repairs_cp1251_mojibake(self):
        title = "\u0421\u043a\u0440\u0438\u043f\u0442 \u043d\u0430 n8n, \u043b\u0438\u0431\u043e \u043b\u044e\u0431\u043e\u0439 \u0434\u0440\u0443\u0433\u043e\u0439 \u042f\u041f"
        description = "\u041d\u0443\u0436\u0435\u043d \u0431\u043e\u0442 \u043d\u0430 python"
        state = {
            "pagination": {
                "data": [
                    {
                        "id": 3022438,
                        "name": title.encode("cp1251").decode("latin1"),
                        "description": f"<p>{description.encode('cp1251').decode('latin1')}</p>",
                        "possiblePriceLimit": 500,
                        "kwork_count": 2,
                        "user": {"id": 77, "username": "buyer"},
                    }
                ]
            }
        }

        projects = KworkStateDataParser.projects_from_state(state)

        assert projects[0].title == title
        assert projects[0].description == description

    @pytest.mark.asyncio
    async def test_token_required_project_calls_use_web_state_for_cookie_only_client(self, service, monkeypatch):
        """Project exchange endpoints should use fast web state fallback with cookie-only auth."""
        cookie_api = MagicMock()
        cookie_api._psr_auth_mode = "cookie-only"
        cookie_api.request = AsyncMock()
        service._api = cookie_api
        web_calls: list[dict] = []

        async def fake_web_projects(**kwargs):
            web_calls.append(kwargs)
            return (
                [{"id": "321", "title": "Need bot", "price": 3000, "offers": 0}],
                {"auth_mode": "cookie-only", "source": "web_state", "paging": {"total": 1}},
            )

        monkeypatch.setattr(service, "get_web_raw_projects", fake_web_projects)

        projects, meta = await service.get_raw_projects(categories="all", query="telegram", kworks_filter_to=5)
        count = await service.get_wants_count(categories="all", query="telegram", kworks_filter_to=5)

        assert projects[0]["id"] == "321"
        assert meta["source"] == "web_state"
        assert meta["auth_mode"] == "cookie-only"
        assert count == 1
        assert web_calls[0]["query"] == "telegram"
        assert web_calls[0]["kworks_filter_to"] == 5
        cookie_api.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_hub_cookies_are_applied_to_created_session(self, service):
        """Session Hub cookies must be present in the actual HTTP session jar."""

        class FakeApi:
            def __init__(self):
                self.session = aiohttp.ClientSession()

        api = FakeApi()
        try:
            applied = service._apply_cookies_to_api(api, {"PHPSESSID": "abc123", "rememberMe": "xyz789"})

            assert applied is True
            assert api.session.cookie_jar.filter_cookies(URL("https://kwork.ru/"))["PHPSESSID"].value == "abc123"
            assert api.session.cookie_jar.filter_cookies(URL("https://api.kwork.ru/"))["rememberMe"].value == "xyz789"
        finally:
            await api.session.close()

    def test_web_chat_list_is_normalized(self, service):
        html = (
            '<script>window.chatList=[{"user_id":123,"username":"buyer",'
            '"unread_count":1,"message":"hello","MID":789,"inbox_message_id":456,'
            '"time":1783335549}];window.other={};</script>'
        )

        chats = service._extract_chat_list(html)
        normalized = service._normalize_web_dialog(chats[0])

        assert normalized["user_id"] == 123
        assert normalized["project_id"] == 123
        assert normalized["username"] == "buyer"
        assert normalized["unread"] == 1
        assert normalized["last_message"] == "hello"
        assert normalized["last_message_id"] == 789

    def test_manual_kwork_cookies_reads_saved_verification_file(self, monkeypatch, tmp_path):
        cookie_file = tmp_path / "kwork_manual_cookies.json"
        cookie_file.write_text(
            json.dumps(
                {
                    "saved_at": "2026-07-07T01:30:00Z",
                    "cookies": [
                        {
                            "name": "captcha_ok",
                            "value": "yes",
                            "domain": ".kwork.ru",
                            "expirationDate": time.time() + 3600,
                        },
                        {
                            "name": "expired",
                            "value": "no",
                            "domain": ".kwork.ru",
                            "expirationDate": time.time() - 10,
                        },
                        {"name": "foreign", "value": "skip", "domain": ".example.com"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("src.platforms.kwork.KWORK_MANUAL_COOKIES_FILE", cookie_file)

        assert manual_kwork_web_cookies() == {"captcha_ok": "yes"}

    @pytest.mark.asyncio
    async def test_mark_web_dialog_read_opens_dialog_and_uses_api_fallbacks(self, service, monkeypatch):
        calls: list[tuple[str, int]] = []

        async def fake_cookies():
            return {"PHPSESSID": "abc"}

        async def fake_inbox_read(message_id: int):
            calls.append(("message", message_id))
            return {"success": True}

        async def fake_tracks_read(dialog_id: int):
            calls.append(("dialog", dialog_id))
            return {"success": True}

        class FakeResponse:
            status_code = 200

            def __init__(self, text: str):
                self.text = text

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                if url.endswith("/inbox"):
                    return FakeResponse(
                        '<script>window.chatList=[{"user_id":123,"username":"buyer",'
                        '"dialog_id":456,"unread_count":1,"message":"hello","MID":789,'
                        '"time":1783335549}];</script>'
                    )
                return FakeResponse("<html>dialog</html>")

        monkeypatch.setattr(service, "_fetch_session_hub_cookies", fake_cookies)
        monkeypatch.setattr(service, "inbox_read", fake_inbox_read)
        monkeypatch.setattr(service, "mark_inbox_read", fake_tracks_read)

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await service.mark_web_dialog_read("buyer")

        assert result["ok"] is True
        assert result["web_opened"] is True
        assert calls == [("message", 789), ("dialog", 456)]

    @pytest.mark.asyncio
    async def test_mark_web_dialog_read_opens_username_when_chat_list_misses(self, service, monkeypatch):
        opened: list[str] = []

        async def fake_cookies():
            return {"PHPSESSID": "abc"}

        class FakeResponse:
            status_code = 200

            def __init__(self, text: str):
                self.text = text

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                opened.append(url)
                if url.endswith("/inbox"):
                    return FakeResponse("<html>no chat list</html>")
                return FakeResponse("<html>dialog</html>")

        monkeypatch.setattr(service, "_fetch_session_hub_cookies", fake_cookies)
        monkeypatch.setattr(service, "inbox_read", AsyncMock(return_value=None))
        monkeypatch.setattr(service, "mark_inbox_read", AsyncMock(return_value=None))

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await service.mark_web_dialog_read("buyer")

        assert result["ok"] is True
        assert result["web_opened"] is True
        assert result["username"] == "buyer"
        assert opened[-1].endswith("/inbox/buyer")

    @pytest.mark.asyncio
    async def test_session_hub_unavailable_falls_back_to_email(self, service, monkeypatch):
        """When Session Hub is unavailable, falls back to email/password."""
        monkeypatch.setenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "false")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_kwork = MagicMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", return_value=mock_kwork):
                api = await service.get_api()

        assert api is mock_kwork


class TestSessionHubRequired:
    """Requirement 7.2: If Session Hub unavailable and SESSION_HUB_REQUIRED=true, refuse init."""

    @pytest.mark.asyncio
    async def test_strict_mode_refuses_when_hub_unavailable(self, service, monkeypatch):
        """SESSION_HUB_REQUIRED=true and hub unavailable → return None."""
        monkeypatch.setenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "true")
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            api = await service.get_api()

        assert api is None


class TestEmailPasswordFallback:
    """Requirement 7.3: If Session Hub unavailable and SESSION_HUB_REQUIRED=false, use email/password."""

    @pytest.mark.asyncio
    async def test_email_password_auth_success(self, service, monkeypatch):
        """Direct auth via email/password works when Session Hub is down."""
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "false")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_kwork = MagicMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", return_value=mock_kwork):
                api = await service.get_api()

        assert api is mock_kwork


class TestMissingCredentials:
    """Requirement 7.4: No credentials → log warning, return None."""

    @pytest.mark.asyncio
    async def test_no_credentials_returns_none(self, service, monkeypatch):
        """No email/password and no Session Hub → return None without exception."""
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "false")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            api = await service.get_api()

        assert api is None


class TestRetryOn401:
    """Requirement 7.5: Retry once with 5s delay on auth failure."""

    @pytest.mark.asyncio
    async def test_retry_on_first_failure(self, service, monkeypatch):
        """First auth attempt fails, retry succeeds after 5s delay."""
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "false")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_kwork = MagicMock()
        call_count = 0

        def kwork_factory(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("HTTP 401 Unauthorized")
            return mock_kwork

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", side_effect=kwork_factory):
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    api = await service.get_api()

        assert api is mock_kwork
        assert call_count == 2
        mock_sleep.assert_called_once_with(5)


class TestFinalFailure:
    """Requirement 7.6: If retry also fails, return None without raising."""

    @pytest.mark.asyncio
    async def test_both_attempts_fail_returns_none(self, service, monkeypatch):
        """Both auth attempts fail → return None, no exception raised."""
        monkeypatch.setenv("KWORK_EMAIL", "test@test.com")
        monkeypatch.setenv("KWORK_PASSWORD", "pass123")
        monkeypatch.setenv("SESSION_HUB_REQUIRED", "false")

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("kwork.Kwork", side_effect=Exception("Auth failed")):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    api = await service.get_api()

        assert api is None


class TestSessionResetLimit:
    """Requirement 7.7: Max 3 session resets per parsing cycle."""

    def test_reset_api_increments_counter(self, service):
        """Each reset_api call increments the counter."""
        service._api = MagicMock()
        service.reset_api()
        assert service._reset_count == 1
        assert service._api is None

    def test_reset_api_respects_limit(self, service):
        """After 3 resets, further resets are ignored."""
        service._api = MagicMock()

        # First 3 resets should work
        for i in range(3):
            service._api = MagicMock()
            service.reset_api()
            assert service._reset_count == i + 1

        # 4th reset should be blocked
        service._api = MagicMock()
        original_api = service._api
        service.reset_api()
        assert service._reset_count == 3  # Still 3
        assert service._api is original_api  # API not cleared

    def test_reset_cycle_resets_counter(self, service):
        """reset_cycle() resets the counter to 0."""
        service._api = MagicMock()
        service.reset_api()
        service._api = MagicMock()
        service.reset_api()
        assert service._reset_count == 2

        service.reset_cycle()
        assert service._reset_count == 0

        # Can reset again after cycle reset
        service._api = MagicMock()
        service.reset_api()
        assert service._reset_count == 1


class TestClose:
    """Test close() method."""

    @pytest.mark.asyncio
    async def test_close_calls_api_close(self, service):
        """close() calls api.close() and clears cached clients."""
        mock_api = AsyncMock()
        service._api = mock_api

        await service.close()

        mock_api.close.assert_called_once()
        assert service._api is None
        assert service._token_api is None

    @pytest.mark.asyncio
    async def test_close_calls_token_api_close(self, service):
        """close() also releases a separate token-mode API client."""
        mock_api = AsyncMock()
        token_api = AsyncMock()
        service._api = mock_api
        service._token_api = token_api

        await service.close()

        mock_api.close.assert_called_once()
        token_api.close.assert_called_once()
        assert service._api is None
        assert service._token_api is None

    @pytest.mark.asyncio
    async def test_close_does_not_double_close_shared_token_api(self, service):
        """close() does not double-close when both caches point at one client."""
        mock_api = AsyncMock()
        service._api = mock_api
        service._token_api = mock_api

        await service.close()

        mock_api.close.assert_called_once()
        assert service._api is None
        assert service._token_api is None

    @pytest.mark.asyncio
    async def test_close_handles_exception(self, service):
        """close() handles exceptions from api.close() gracefully."""
        mock_api = AsyncMock()
        mock_api.close.side_effect = Exception("Close failed")
        service._api = mock_api

        await service.close()  # Should not raise

        assert service._api is None
        assert service._token_api is None

    @pytest.mark.asyncio
    async def test_close_when_no_api(self, service):
        """close() is safe to call when no API client exists."""
        assert service._api is None
        await service.close()  # Should not raise
        assert service._api is None
        assert service._token_api is None


class TestCachedClient:
    """Test that get_api returns cached client on subsequent calls."""

    @pytest.mark.asyncio
    async def test_returns_cached_client(self, service):
        """Second call to get_api returns the cached client without re-auth."""
        mock_api = MagicMock()
        service._api = mock_api

        api = await service.get_api()
        assert api is mock_api

    @pytest.mark.asyncio
    async def test_rebuilds_cached_client_when_vpnte_proxy_disappears(self, service, monkeypatch):
        """A stale local proxy must not be reused after it disappears from /instances."""
        monkeypatch.setenv("VPNTE_PROXY_ENABLED", "true")
        stale_api = MagicMock()
        stale_api._psr_proxy_url = "http://127.0.0.1:17991"
        replacement_api = MagicMock()
        service._api = stale_api

        vpnte_client = MagicMock()
        vpnte_client.next_proxy.return_value = "http://127.0.0.1:17992"
        monkeypatch.setattr("src.utils.vpnte_proxy.get_vpnte_proxy_client", lambda: vpnte_client)
        service._try_session_hub = AsyncMock(return_value=replacement_api)

        api = await service.get_api()

        assert api is replacement_api
        stale_api.close.assert_called_once()
        service._try_session_hub.assert_awaited_once()
        vpnte_client.next_proxy.assert_called_once_with(rotate=False)

    @pytest.mark.asyncio
    async def test_keeps_cached_client_when_vpnte_proxy_is_live(self, service, monkeypatch):
        """A cached client remains reusable when its authoritative proxy is still live."""
        monkeypatch.setenv("VPNTE_PROXY_ENABLED", "true")
        cached_api = MagicMock()
        cached_api._psr_proxy_url = "http://127.0.0.1:17992"
        service._api = cached_api
        vpnte_client = MagicMock()
        vpnte_client.next_proxy.return_value = "http://127.0.0.1:17992"
        monkeypatch.setattr("src.utils.vpnte_proxy.get_vpnte_proxy_client", lambda: vpnte_client)
        service._try_session_hub = AsyncMock()

        api = await service.get_api()

        assert api is cached_api
        service._try_session_hub.assert_not_awaited()
        vpnte_client.next_proxy.assert_called_once_with(rotate=False)


class TestEnvWebCookies:
    def test_parse_cookie_env_accepts_raw_cookie_header(self):
        assert parse_cookie_env("slrememberme=a; csrf_user_token=b\nuad=c") == {
            "slrememberme": "a",
            "csrf_user_token": "b",
            "uad": "c",
        }

    def test_parse_cookie_env_accepts_json_dict(self):
        assert parse_cookie_env('{"slrememberme":"a","RORSSQIHEK":"b"}') == {
            "slrememberme": "a",
            "RORSSQIHEK": "b",
        }

    def test_env_kwork_web_cookies_supports_real_kwork_names(self, monkeypatch):
        monkeypatch.setenv("KWORK_COOKIE_SLREMEMBERME", "remember")
        monkeypatch.setenv("KWORK_COOKIE_CSRF_USER_TOKEN", "csrf")
        monkeypatch.setenv("KWORK_COOKIE_UAD", "uad")
        monkeypatch.setenv("KWORK_COOKIE_RORSSQIHEK", "guard")

        cookies = env_kwork_web_cookies()

        assert cookies["slrememberme"] == "remember"
        assert cookies["csrf_user_token"] == "csrf"
        assert cookies["uad"] == "uad"
        assert cookies["RORSSQIHEK"] == "guard"
