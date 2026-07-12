import json
import urllib.error

import pytest

from src.utils.vpnte_proxy import VpnteProxyClient, clear_vpnte_proxy_cache, effective_proxy_url


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture
def fake_vpnte_control(monkeypatch):
    calls: list[dict] = []
    responses: list[dict] = []

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "token": request.headers.get("X-vpnte-control-token"),
                "timeout": timeout,
            }
        )
        if not responses:
            raise AssertionError(f"Unexpected VPNTE request: {request.full_url}")
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls, responses


def test_effective_proxy_url_is_opt_in(monkeypatch):
    clear_vpnte_proxy_cache()
    monkeypatch.delenv("VPNTE_PROXY_ENABLED", raising=False)
    monkeypatch.delenv("VPNTE_PROXY", raising=False)
    monkeypatch.delenv("VPNTE_EXTERNAL_PROXY", raising=False)

    assert effective_proxy_url(fallback="http://fallback:8080") == "http://fallback:8080"


def test_rotate_reads_vpnte_endpoint_and_token(monkeypatch, tmp_path):
    clear_vpnte_proxy_cache()
    appdata = tmp_path / "AppData" / "Roaming"
    vpnte_dir = appdata / "VPN Tunnel Enforcer"
    vpnte_dir.mkdir(parents=True)
    (vpnte_dir / "external-proxy-control-endpoint.json").write_text(
        json.dumps({"url": "http://127.0.0.1:19001"}),
        encoding="utf-8",
    )
    (vpnte_dir / "external-proxy-control-token").write_text("secret-token\n", encoding="utf-8")

    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["token"] = request.headers.get("X-vpnte-control-token")
        seen["timeout"] = timeout
        return FakeResponse({"running": True, "proxyUrl": "http://127.0.0.1:17990"})

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("VPNTE_PROXY_COUNTRY", "Netherlands")
    monkeypatch.delenv("VPNTE_CONTROL_URL", raising=False)
    monkeypatch.delenv("VPNTE_PROXY_PROFILE_ID", raising=False)
    monkeypatch.delenv("VPNTE_PROXY_PORT", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    status = VpnteProxyClient().rotate()

    assert status["proxyUrl"] == "http://127.0.0.1:17990"
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:19001/rotate?country=Netherlands"
    assert seen["token"] == "secret-token"
    assert seen["timeout"] == 10.0


def test_effective_proxy_url_caches_started_proxy(monkeypatch):
    clear_vpnte_proxy_cache()
    monkeypatch.setenv("VPNTE_PROXY_ENABLED", "true")
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19002")
    monkeypatch.setenv("VPNTE_PROXY_CACHE_TTL", "60")

    seen = {"calls": 0}

    def fake_urlopen(request, timeout):
        seen["calls"] += 1
        return FakeResponse({"running": True, "proxyUrl": "http://127.0.0.1:17990"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert effective_proxy_url(rotate=False, fallback=None) == "http://127.0.0.1:17990"
    assert effective_proxy_url(rotate=False, fallback=None) == "http://127.0.0.1:17990"
    assert seen["calls"] == 1


def test_effective_proxy_url_non_strict_falls_back(monkeypatch):
    clear_vpnte_proxy_cache()
    monkeypatch.setenv("VPNTE_PROXY_ENABLED", "true")
    monkeypatch.setenv("VPNTE_PROXY_STRICT", "false")
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19003")

    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert effective_proxy_url(rotate=False, fallback="http://fallback:8080") == "http://fallback:8080"


def test_instances_normalizes_envelope(monkeypatch, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19004")
    responses.append(
        {
            "maxInstances": 10,
            "instances": [
                {
                    "slot": "3",
                    "running": "true",
                    "host": "127.0.0.1",
                    "port": "17992",
                    "proxy_url": "http://127.0.0.1:17992",
                    "profile_id": "nl-vless-2",
                    "profile_name": "Netherlands 2",
                    "country": "Netherlands",
                    "pid": "1234",
                    "started_at": "2026-07-11T12:00:00Z",
                },
                {"slot": 4, "running": False, "host": "127.0.0.1", "port": None},
            ],
        }
    )

    instances = VpnteProxyClient().instances()

    assert instances[0]["slot"] == 3
    assert instances[0]["running"] is True
    assert instances[0]["port"] == 17992
    assert instances[0]["proxyUrl"] == "http://127.0.0.1:17992"
    assert instances[0]["profileId"] == "nl-vless-2"
    assert instances[0]["profileName"] == "Netherlands 2"
    assert instances[0]["pid"] == 1234
    assert instances[0]["startedAt"] == "2026-07-11T12:00:00Z"
    assert instances[1]["slot"] == 4
    assert calls == [
        {
            "url": "http://127.0.0.1:19004/instances",
            "method": "GET",
            "token": None,
            "timeout": 10.0,
        }
    ]


def test_slot_aware_actions_send_explicit_slot_queries(monkeypatch, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19005")
    monkeypatch.setenv("VPNTE_CONTROL_TOKEN", "fixture-token")
    monkeypatch.setenv("VPNTE_PROXY_COUNTRY", "Legacy Country")
    monkeypatch.setenv("VPNTE_PROXY_PROFILE_ID", "legacy-profile")
    monkeypatch.setenv("VPNTE_PROXY_PORT", "17990")
    responses.extend(
        [
            {"running": True, "proxyUrl": "http://127.0.0.1:17992"},
            {"running": True, "proxyUrl": "http://127.0.0.1:17992"},
            {"running": True, "proxyUrl": "http://127.0.0.1:17992"},
            {"running": False, "proxyUrl": None},
        ]
    )
    client = VpnteProxyClient()

    assert client.status(3)["slot"] == 3
    assert client.start(3, country="Netherlands", profile_id="nl-vless-2")["slot"] == 3
    assert client.rotate(3, country="Germany")["slot"] == 3
    assert client.stop(3)["slot"] == 3

    assert [call["url"] for call in calls] == [
        "http://127.0.0.1:19005/status?slot=3",
        "http://127.0.0.1:19005/start?slot=3&country=Netherlands&profileId=nl-vless-2",
        "http://127.0.0.1:19005/rotate?slot=3&country=Germany",
        "http://127.0.0.1:19005/stop?slot=3",
    ]
    assert [call["method"] for call in calls] == ["GET", "POST", "POST", "POST"]
    assert [call["token"] for call in calls] == [None, "fixture-token", "fixture-token", "fixture-token"]
