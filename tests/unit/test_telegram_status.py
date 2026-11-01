import pytest

from src.api.routes import telegram


@pytest.mark.asyncio
async def test_telegram_status_returns_provider_failure_instead_of_crashing(monkeypatch):
    async def unavailable():
        raise RuntimeError("test connection refused")

    monkeypatch.setattr(telegram, "bot_api_get_me", unavailable)
    monkeypatch.setattr(telegram, "mtproto_configured", lambda: False)

    result = await telegram.telegram_status()

    assert result["bot_api"] == {"ok": False, "detail": "test connection refused"}
