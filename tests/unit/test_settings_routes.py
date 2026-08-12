from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.routes import settings


def test_settings_accepts_active_kwork_controls(monkeypatch):
    values = {
        "KWORK_AUTO_REVIEW": "true",
        "KWORK_MARKET_USE_PROXY": "false",
        "KWORK_MARKET_SUPPLY_MIN_CARDS": "500",
        "KWORK_MARKET_ASSISTANT_MAP_CONCURRENCY": "3",
        "KWORK_COVER_IMAGE_MODEL": "gpt-image-2",
        "KWORK_PORTFOLIO_IMAGE_QUALITY": "high",
        "KWORK_PORTFOLIO_IMAGE_CONCURRENCY": "2",
        "KWORK_COVER_QA_PROVIDER": "openai",
        "KWORK_COVER_QA_MODEL": "gpt-5.6-terra",
        "KWORK_COVER_PROMPT_PROVIDER": "openai",
        "KWORK_COVER_PROMPT_MODEL": "gpt-5.6-terra",
        "KWORK_REGISTRATION_BURST_LIMIT": "0",
    }
    saved: dict[str, str] = {}

    for key in values:
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(settings, "_write_env_file", lambda data: saved.update(data))
    monkeypatch.setattr(settings, "_reset_caches", lambda: None)

    result = settings.update_env(settings.EnvUpdateRequest(values=values))

    assert set(result["updated"]) == set(values)
    assert saved == values


def test_settings_rejects_kwork_cover_model_downgrade():
    with pytest.raises(HTTPException) as exc_info:
        settings.update_env(settings.EnvUpdateRequest(values={"KWORK_COVER_IMAGE_MODEL": "gpt-image-1"}))

    assert exc_info.value.status_code == 422


def test_settings_masks_only_actual_secrets():
    assert "KWORK_REGISTRATION_MAIL_TIMEOUT" not in settings._SECRET_KEYS
    assert "KWORK_REGISTRATION_MAIL_POLL_INTERVAL" not in settings._SECRET_KEYS
    assert "KWORK_COVER_IMAGE_API_KEY" in settings._SECRET_KEYS
    assert "KWORK_COVER_IMAGE_CONN" in settings._SECRET_KEYS
