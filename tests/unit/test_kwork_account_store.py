from __future__ import annotations

from pathlib import Path

from src.platforms.kwork_account_store import KworkAccountStore


def test_store_keeps_password_and_session_in_plaintext(tmp_path: Path):
    db_path = tmp_path / "accounts.db"
    store = KworkAccountStore(
        db_path=db_path,
        key_path=tmp_path / "accounts.key",
    )

    saved = store.save(
        registration_id="f" * 32,
        email="account@catchmail.io",
        username="account",
        user_type=1,
        mail_provider="catchmail",
        status="signup_submitted",
        registration_started_at="2026-07-13T12:00:00Z",
        password="PsrServerPasswordA1",
        session={
            "cookies": [
                {
                    "name": "PHPSESSID",
                    "value": "cookie-secret",
                    "domain": "kwork.ru",
                    "path": "/",
                }
            ],
            "proxy_url": "http://proxy.example:8080",
            "auth_data": {"token": "token-secret"},
        },
        signup_ip="203.0.113.10",
    )

    assert saved.password == "PsrServerPasswordA1"
    assert saved.session["cookies"][0]["value"] == "cookie-secret"
    assert saved.public_data()["session_cookie_count"] == 1

    restored = store.get("f" * 32)
    assert restored is not None
    assert restored.email == "account@catchmail.io"
    assert restored.password == "PsrServerPasswordA1"
    assert restored.session["auth_data"]["token"] == "token-secret"

    raw_db = db_path.read_bytes()
    assert b"PsrServerPasswordA1" in raw_db
    assert b"cookie-secret" in raw_db
    assert b"token-secret" in raw_db


def test_store_persists_registration_route_and_stable_market_persona(tmp_path: Path):
    store = KworkAccountStore(
        db_path=tmp_path / "accounts.db",
        key_path=tmp_path / "accounts.key",
    )

    saved = store.save(
        registration_id="a" * 32,
        email="market@catchmail.io",
        username="marketaccount",
        user_type=1,
        mail_provider="catchmail",
        status="activated",
        registration_started_at="2026-07-14T12:00:00Z",
        password="PsrMarketPasswordA1",
        session={"cookies": [{"name": "PHPSESSID", "value": "session"}], "proxy_url": "http://127.0.0.1:17995"},
        signup_ip="203.0.113.15",
        registration_slot=6,
    )

    restored = store.get("a" * 32)

    assert restored is not None
    assert restored.registration_slot == 6
    assert restored.registration_proxy_url == "http://127.0.0.1:17995"
    assert restored.persona == saved.persona
    assert restored.persona["persona_id"].startswith("machine-")
    assert restored.public_data()["registration_slot"] == 6
    assert restored.public_data()["persona_id"] == restored.persona["persona_id"]
