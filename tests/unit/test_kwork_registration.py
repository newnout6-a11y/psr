from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import pytest

from src.platforms.kwork import KworkRegistrationError, KworkService, _RegistrationRouteProbe
from src.platforms.kwork_ext import get_pacer
from src.platforms.kwork_account_store import KworkAccountStore
from src.utils.catchmail import CatchmailClient


def test_registration_username_is_readable_ascii_and_form_is_email_only():
    username = KworkService._generate_username("Тест.Пользователь@example.com")
    form = KworkService._build_signup_form(
        email="test@example.com",
        username=username,
        password="strong-pass",
        user_type=1,
    )

    assert re.fullmatch(r"[a-z0-9]{4,20}", username)
    assert "-" not in username
    assert "_" not in username
    assert form["signup_mode"] == "email"
    assert form["userType"] == "1"
    assert "user_phone" not in form
    assert "phone_token" not in form
    assert "smart-token" not in form


def test_registration_rejects_known_temporary_mail_domain():
    assert "not accepted" in (KworkService._registration_email_error("user@bekommenmail.com") or "")


def test_registration_defaults_to_catchmail_provider_and_generated_address_is_valid():
    assert KworkService._registration_mail_provider("") == "catchmail"
    assert KworkService._registration_mail_provider("catchmail") == "catchmail"
    assert KworkService._registration_email_error(CatchmailClient.generate_address()) is None


def test_registration_pacing_is_isolated_by_proxy_route(monkeypatch):
    import src.platforms.kwork_ext as kwork_ext

    monkeypatch.setattr(kwork_ext, "_pacer", None)
    monkeypatch.setattr(kwork_ext, "_scoped_pacers", {})

    first = get_pacer(scope="http://127.0.0.1:18012", min_delay=0, max_delay=0)
    second = get_pacer(scope="http://127.0.0.1:18013", min_delay=0, max_delay=0)

    assert first is get_pacer(scope="http://127.0.0.1:18012", min_delay=0, max_delay=0)
    assert first is not second
    assert first.min_delay == 0
    assert first.max_delay == 0


@pytest.mark.asyncio
async def test_registration_pacing_can_disable_the_burst_limit(monkeypatch):
    import src.platforms.kwork_ext as kwork_ext

    monkeypatch.setattr(kwork_ext, "_scoped_pacers", {})
    monkeypatch.setenv("KWORK_BURST_LIMIT", "1")
    monkeypatch.setenv("KWORK_REGISTRATION_BURST_LIMIT", "0")
    service = KworkService()
    pacer = service._registration_pacer("http://127.0.0.1:18012")

    for _ in range(12):
        await pacer.wait()

    assert pacer.burst_limit == 0
    assert pacer._timestamps == []


def test_generated_registration_password_has_required_character_classes():
    password = KworkService._generate_registration_password(forbidden="brightmosaic1234")

    assert len(password) >= 6
    assert any(char.islower() for char in password)
    assert any(char.isupper() for char in password)
    assert any(char.isdigit() for char in password)
    assert any(char in "!@#$%" for char in password)
    assert set(password) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%")
    assert password != "brightmosaic1234"


def test_signup_validation_error_is_not_misclassified_as_captcha():
    payload = {"recaptcha_show": True, "errors": ["Password matches the login"]}

    assert KworkService._signup_requires_captcha(payload) is False
    assert KworkService._signup_error_message(payload) == "Password matches the login"


def test_signup_captcha_without_form_error_is_detected():
    assert KworkService._signup_requires_captcha({"recaptcha_show": True, "errors": []}) is True


def test_vpnte_proxy_url_is_validated_for_registration():
    normalized = KworkService._normalize_registration_proxy("http://127.0.0.1:18012")

    assert normalized == "http://127.0.0.1:18012"
    with pytest.raises(KworkRegistrationError, match="Proxy must be"):
        KworkService._normalize_registration_proxy("not-a-proxy")


def test_registration_resolves_requested_vpnte_slots(monkeypatch):
    service = KworkService()

    def fake_instances(_client):
        return [
            {"slot": 12, "running": True, "proxyUrl": "http://127.0.0.1:18012"},
            {"slot": 13, "running": False, "proxyUrl": "http://127.0.0.1:18013"},
            {"slot": 14, "running": True, "proxyUrl": "http://127.0.0.1:18014"},
        ]

    monkeypatch.setattr("src.utils.vpnte_proxy.VpnteProxyClient.instances", fake_instances)

    assert service._vpnte_registration_proxies([14, 12]) == [
        (14, "http://127.0.0.1:18014"),
        (12, "http://127.0.0.1:18012"),
    ]


@pytest.mark.asyncio
async def test_registration_batch_runs_up_to_five_distinct_vpnte_slots_concurrently(monkeypatch):
    service = KworkService()
    calls: list[dict[str, object]] = []
    active_proxies: set[str] = set()
    max_active = 0

    async def fake_register_account(**kwargs):
        nonlocal max_active
        proxy = str(kwargs["proxy_url"])
        assert proxy not in active_proxies
        active_proxies.add(proxy)
        max_active = max(max_active, len(active_proxies))
        calls.append(kwargs)
        index = len(calls)
        await asyncio.sleep(0.01)
        active_proxies.remove(proxy)
        return {"ok": True, "status": "activated", "registration_id": f"{index}" * 32, "activated": True}

    monkeypatch.setattr(service, "register_account", fake_register_account)
    monkeypatch.setattr(
        service,
        "_vpnte_registration_proxies",
        lambda slots: [
            (12, "http://127.0.0.1:18012"),
            (14, "http://127.0.0.1:18014"),
            (15, "http://127.0.0.1:18015"),
            (16, "http://127.0.0.1:18016"),
            (17, "http://127.0.0.1:18017"),
            (18, "http://127.0.0.1:18018"),
            (19, "http://127.0.0.1:18019"),
        ],
    )
    monkeypatch.setattr(
        service,
        "_preflight_registration_routes",
        AsyncMock(
            side_effect=lambda routes: [
                _RegistrationRouteProbe(
                    slot=slot,
                    proxy=proxy,
                    egress_ip=f"203.0.113.{index}",
                    elapsed_ms=10,
                )
                for index, (slot, proxy) in enumerate(routes, start=1)
            ],
        ),
    )
    monkeypatch.setattr(service, "_registration_used_ips", lambda: set())

    result = await service.register_accounts_batch(
        account_count=7,
        vpnte_slots=[12, 14, 15, 16, 17, 18, 19],
        mail_provider="catchmail",
    )

    assert result["activated_count"] == 7
    assert result["proxy_count"] == 7
    assert result["parallel_limit"] == 5
    assert max_active == 5
    assert result["vpnte_slots"] == [12, 14, 15, 16, 17, 18, 19]
    assert [call["proxy_url"] for call in calls] == [
        "http://127.0.0.1:18012",
        "http://127.0.0.1:18014",
        "http://127.0.0.1:18015",
        "http://127.0.0.1:18016",
        "http://127.0.0.1:18017",
        "http://127.0.0.1:18018",
        "http://127.0.0.1:18019",
    ]
    assert [item["vpnte_slot"] for item in result["results"]] == [12, 14, 15, 16, 17, 18, 19]


@pytest.mark.asyncio
async def test_registration_batch_keeps_healthy_slots_when_one_transport_fails(monkeypatch):
    service = KworkService()

    async def fake_register_account(**kwargs):
        if kwargs["proxy_url"] == "http://127.0.0.1:18013":
            raise RuntimeError("proxy unavailable")
        await asyncio.sleep(0.01)
        return {"ok": True, "status": "activated", "activated": True}

    monkeypatch.setattr(service, "register_account", fake_register_account)
    monkeypatch.setattr(
        service,
        "_vpnte_registration_proxies",
        lambda slots: [
            (12, "http://127.0.0.1:18012"),
            (13, "http://127.0.0.1:18013"),
            (14, "http://127.0.0.1:18014"),
        ],
    )
    monkeypatch.setattr(
        service,
        "_preflight_registration_routes",
        AsyncMock(
            return_value=[
                _RegistrationRouteProbe(12, "http://127.0.0.1:18012", "203.0.113.12", 10),
                _RegistrationRouteProbe(13, "http://127.0.0.1:18013", "203.0.113.13", 10),
                _RegistrationRouteProbe(14, "http://127.0.0.1:18014", "203.0.113.14", 10),
            ],
        ),
    )
    monkeypatch.setattr(service, "_registration_used_ips", lambda: set())

    result = await service.register_accounts_batch(
        account_count=3,
        vpnte_slots=[12, 13, 14],
        mail_provider="catchmail",
    )

    assert result["completed_count"] == 3
    assert result["activated_count"] == 2
    assert result["results"][1]["code"] == "transport_error"
    assert [item["status"] for item in result["results"]] == ["activated", "registration_error", "activated"]


@pytest.mark.asyncio
async def test_registration_batch_uses_late_healthy_routes_as_real_reserve(monkeypatch):
    service = KworkService()
    routes = [
        (12, "http://127.0.0.1:18012"),
        (13, "http://127.0.0.1:18013"),
        (14, "http://127.0.0.1:18014"),
        (15, "http://127.0.0.1:18015"),
        (16, "http://127.0.0.1:18016"),
        (17, "http://127.0.0.1:18017"),
        (18, "http://127.0.0.1:18018"),
    ]
    probes = [
        _RegistrationRouteProbe(12, routes[0][1], "203.0.113.1", 10),
        _RegistrationRouteProbe(13, routes[1][1], None, 10, "ConnectTimeout"),
        _RegistrationRouteProbe(14, routes[2][1], "203.0.113.1", 10),
        _RegistrationRouteProbe(15, routes[3][1], "203.0.113.15", 10),
        _RegistrationRouteProbe(16, routes[4][1], "203.0.113.2", 10),
        _RegistrationRouteProbe(17, routes[5][1], "203.0.113.3", 10),
        _RegistrationRouteProbe(18, routes[6][1], "203.0.113.4", 10),
    ]
    calls: list[int] = []

    async def fake_register_account(**kwargs):
        calls.append(int(kwargs["registration_slot"]))
        return {"ok": True, "status": "activated", "activated": True}

    monkeypatch.setattr(service, "_vpnte_registration_proxies", lambda _slots: routes)
    monkeypatch.setattr(service, "_preflight_registration_routes", AsyncMock(return_value=probes))
    monkeypatch.setattr(service, "_registration_used_ips", lambda: {"203.0.113.15"})
    monkeypatch.setattr(service, "register_account", fake_register_account)

    result = await service.register_accounts_batch(
        account_count=3,
        vpnte_slots=[slot for slot, _proxy in routes],
        mail_provider="catchmail",
        avoid_used_ips=True,
    )

    assert calls == [12, 16, 17]
    assert result["vpnte_slots"] == [12, 16, 17]
    assert result["reserve_vpnte_slots"] == [18]
    assert result["failed_vpnte_slots"] == [13]
    assert result["duplicate_vpnte_slots"] == [14]
    assert result["used_ip_vpnte_slots"] == [15]
    assert result["healthy_proxy_count"] == 4


@pytest.mark.asyncio
async def test_registration_batch_aborts_before_signup_when_unique_capacity_is_insufficient(monkeypatch):
    service = KworkService()
    routes = [
        (12, "http://127.0.0.1:18012"),
        (13, "http://127.0.0.1:18013"),
        (14, "http://127.0.0.1:18014"),
        (15, "http://127.0.0.1:18015"),
    ]
    probes = [
        _RegistrationRouteProbe(12, routes[0][1], "203.0.113.1", 10),
        _RegistrationRouteProbe(13, routes[1][1], "203.0.113.1", 10),
        _RegistrationRouteProbe(14, routes[2][1], None, 10, "ConnectTimeout"),
        _RegistrationRouteProbe(15, routes[3][1], "203.0.113.15", 10),
    ]
    register_account = AsyncMock()
    monkeypatch.setattr(service, "_vpnte_registration_proxies", lambda _slots: routes)
    monkeypatch.setattr(service, "_preflight_registration_routes", AsyncMock(return_value=probes))
    monkeypatch.setattr(service, "_registration_used_ips", lambda: {"203.0.113.15"})
    monkeypatch.setattr(service, "register_account", register_account)

    with pytest.raises(KworkRegistrationError) as exc_info:
        await service.register_accounts_batch(
            account_count=3,
            vpnte_slots=[slot for slot, _proxy in routes],
            mail_provider="catchmail",
            avoid_used_ips=True,
        )

    assert exc_info.value.code == "vpnte_capacity_insufficient"
    assert exc_info.value.payload["unique_available_ip_count"] == 1
    assert exc_info.value.payload["failed_vpnte_slots"] == [14]
    assert exc_info.value.payload["duplicate_vpnte_slots"] == [13]
    assert exc_info.value.payload["used_ip_vpnte_slots"] == [15]
    register_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_suggestion_is_normalized_without_special_characters():
    service = KworkService()
    replies = iter(
        [
            {"success": True, "login": "bright-craft1"},
            {"success": True, "login": "brightcraft1"},
        ]
    )

    class Response:
        def json(self):
            return next(replies)

    class Client:
        async def post(self, *_args, **_kwargs):
            return Response()

    login, _payload = await service._check_login(Client(), "brightcraft1")

    assert login == "brightcraft1"
    assert re.fullmatch(r"[a-z0-9]+", login)


def test_registration_cookie_snapshot_restores_domain_and_value():
    import httpx

    class Client:
        cookies = httpx.Cookies()

    Client.cookies.set("PHPSESSID", "session-value", domain=".kwork.ru", path="/")
    snapshot = KworkService._registration_session_snapshot(Client(), proxy="http://proxy.example:8080")
    restored = KworkService._restore_registration_cookies(snapshot)

    restored_items = {(cookie.name, cookie.value, cookie.domain, cookie.path) for cookie in restored.jar}
    assert ("PHPSESSID", "session-value", ".kwork.ru", "/") in restored_items


def test_registration_account_inventory_is_public_and_can_remove_a_local_record(tmp_path):
    service = KworkService()
    service._registration_account_store = KworkAccountStore(
        db_path=tmp_path / "accounts.db",
        key_path=tmp_path / "accounts.key",
    )
    store = service._registration_account_store
    store.save(
        registration_id="b" * 32,
        email="saved@catchmail.io",
        username="saveduser",
        user_type=1,
        mail_provider="catchmail",
        status="activated",
        registration_started_at="2026-07-14T12:00:00Z",
        password="PsrSavedPasswordA1",
        session={"cookies": [{"name": "PHPSESSID", "value": "saved-session"}]},
        signup_ip="203.0.113.10",
        activation_ip="203.0.113.10",
        activated_at="2026-07-14T12:01:00Z",
    )

    accounts = service.list_registration_accounts()

    assert len(accounts) == 1
    assert accounts[0]["registration_id"] == "b" * 32
    assert accounts[0]["session_cookie_count"] == 1
    assert "password" not in accounts[0]
    assert "session" not in accounts[0]
    assert service.delete_registration_account("b" * 32) == {"ok": True, "registration_id": "b" * 32}
    assert service.list_registration_accounts() == []


@pytest.mark.asyncio
async def test_registration_account_session_check_uses_saved_session(monkeypatch, tmp_path):
    service = KworkService()
    service._registration_account_store = KworkAccountStore(
        db_path=tmp_path / "accounts.db",
        key_path=tmp_path / "accounts.key",
    )
    service._registration_account_store.save(
        registration_id="c" * 32,
        email="saved@catchmail.io",
        username="saveduser",
        user_type=1,
        mail_provider="catchmail",
        status="activated",
        registration_started_at="2026-07-14T12:00:00Z",
        password="PsrSavedPasswordA1",
        session={"cookies": [], "proxy_url": "http://127.0.0.1:18012"},
    )

    class ClientContext:
        async def __aenter__(self):
            return "saved-client"

        async def __aexit__(self, *_args):
            return False

    class Pacer:
        async def wait(self):
            return None

    monkeypatch.setattr(service, "_registration_http_client", lambda **_kwargs: ClientContext())
    monkeypatch.setattr(service, "_registration_pacer", lambda _proxy: Pacer())
    verify = AsyncMock(return_value={"ok": True, "status_code": 200, "auth_mode": "saved_http_session"})
    monkeypatch.setattr(service, "_verify_registration_session", verify)

    result = await service.check_registration_account_session("c" * 32)

    assert result["registration_id"] == "c" * 32
    assert result["session_check"]["ok"] is True
    verify.assert_awaited_once_with("saved-client")


@pytest.mark.asyncio
async def test_activation_http_success_alone_is_not_proof():
    service = KworkService()
    service.__post_init__()

    class Response:
        status_code = 200
        url = "https://kwork.ru/"
        text = "<html><title>Kwork</title><body>Welcome</body></html>"

    class Client:
        get = AsyncMock(return_value=Response())

    result = await service._activate_account("https://kwork.ru/activate?token=x", Client())

    assert result["http_ok"] is True
    assert result["ok"] is False
    assert result["reason"] == "activation_response_unconfirmed"


@pytest.mark.asyncio
async def test_post_activation_actor_proves_account(monkeypatch):
    service = KworkService()
    service.__post_init__()

    class Api:
        async def request(self, method, endpoint, **kwargs):
            assert (method, endpoint) == ("post", "actor")
            assert kwargs == {"use_token": True}
            return {"success": True, "response": {"username": "testuser", "verified": True, "status": "active"}}

        async def close(self):
            return None

    create_api = AsyncMock(return_value=Api())
    monkeypatch.setattr(service, "_create_api_client", create_api)

    result = await service._verify_activated_account(
        "test@example.com",
        "strong-pass",
        proxy="http://127.0.0.1:18012",
    )

    assert result["ok"] is True
    assert result["username"] == "testuser"
    assert result["verified"] is True
    create_api.assert_awaited_once_with(
        "test@example.com",
        "strong-pass",
        proxy="http://127.0.0.1:18012",
    )


@pytest.mark.asyncio
async def test_verify_registration_reuses_saved_cookie_session(monkeypatch, tmp_path):
    service = KworkService()
    service._registration_account_store = KworkAccountStore(
        db_path=tmp_path / "accounts.db",
        key_path=tmp_path / "accounts.key",
    )
    store = service._registration_account_store
    store.save(
        registration_id="a" * 32,
        email="saved@catchmail.io",
        username="saveduser",
        user_type=1,
        mail_provider="catchmail",
        status="signup_submitted",
        registration_started_at="2026-07-13T12:00:00Z",
        password="PsrSavedPasswordA1",
        session={
            "cookies": [
                {
                    "name": "PHPSESSID",
                    "value": "saved-session",
                    "domain": ".kwork.ru",
                    "path": "/",
                    "secure": True,
                    "rest": {"HttpOnly": None},
                }
            ],
            "proxy_url": "",
            "user_agent": "PSR-KworkRegistrationTest/1.0",
            "auth_data": {"token": "saved-token"},
        },
        signup_ip="203.0.113.10",
    )

    async def fake_activate(_link, client):
        assert any(cookie.name == "PHPSESSID" and cookie.value == "saved-session" for cookie in client.cookies.jar)
        return {"http_ok": True, "status_code": 200, "final_url": "https://kwork.ru/", "body_proof": True}

    monkeypatch.setattr(service, "_fetch_verification_link", AsyncMock(return_value="https://kwork.ru/confirmemail?c=abc"))
    monkeypatch.setattr(service, "_activate_account", fake_activate)
    monkeypatch.setattr(service, "_capture_registration_ip", AsyncMock(return_value="203.0.113.10"))
    monkeypatch.setattr(service, "_verify_registration_session", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(service, "_verify_activated_account", AsyncMock(return_value={"ok": True, "verified": True}))

    result = await service.verify_registration_activation(registration_id="a" * 32)

    assert result["activated"] is True
    assert result["session_cookie_count"] == 1
    saved = store.get("a" * 32)
    assert saved is not None
    assert saved.status == "activated"
    assert saved.activation_ip == "203.0.113.10"

    credentials = service.get_registration_credentials("a" * 32)
    assert credentials == {
        "registration_id": "a" * 32,
        "email": "saved@catchmail.io",
        "username": "saveduser",
        "password": "PsrSavedPasswordA1",
    }
