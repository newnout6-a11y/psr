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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.platforms.kwork import KworkService


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
        """close() calls api.close() and sets _api to None."""
        mock_api = AsyncMock()
        service._api = mock_api

        await service.close()

        mock_api.close.assert_called_once()
        assert service._api is None

    @pytest.mark.asyncio
    async def test_close_handles_exception(self, service):
        """close() handles exceptions from api.close() gracefully."""
        mock_api = AsyncMock()
        mock_api.close.side_effect = Exception("Close failed")
        service._api = mock_api

        await service.close()  # Should not raise

        assert service._api is None

    @pytest.mark.asyncio
    async def test_close_when_no_api(self, service):
        """close() is safe to call when no API client exists."""
        assert service._api is None
        await service.close()  # Should not raise
        assert service._api is None


class TestCachedClient:
    """Test that get_api returns cached client on subsequent calls."""

    @pytest.mark.asyncio
    async def test_returns_cached_client(self, service):
        """Second call to get_api returns the cached client without re-auth."""
        mock_api = MagicMock()
        service._api = mock_api

        api = await service.get_api()
        assert api is mock_api
