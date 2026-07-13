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

    seen: list[dict[str, object]] = []

    def fake_urlopen(request, timeout):
        seen.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "token": request.headers.get("X-vpnte-control-token"),
                "timeout": timeout,
            }
        )
        return FakeResponse(
            {
                "instances": [
                    {"slot": 1, "running": True, "proxyUrl": "http://127.0.0.1:17990"},
                ]
            }
            if request.get_method() == "GET"
            else {"ok": True}
        )

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("VPNTE_PROXY_COUNTRY", "Netherlands")
    monkeypatch.delenv("VPNTE_CONTROL_URL", raising=False)
    monkeypatch.delenv("VPNTE_PROXY_PROFILE_ID", raising=False)
    monkeypatch.delenv("VPNTE_PROXY_PORT", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    status = VpnteProxyClient().rotate()

    assert status["proxyUrl"] == "http://127.0.0.1:17990"
    assert seen[0]["method"] == "POST"
    assert seen[0]["url"] == "http://127.0.0.1:19001/rotate?country=Netherlands"
    assert seen[0]["token"] == "secret-token"
    assert seen[0]["timeout"] == 10.0
    assert seen[1]["url"] == "http://127.0.0.1:19001/instances"


def test_endpoint_file_supports_lowercase_registration_and_token_file(monkeypatch, tmp_path, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    appdata = tmp_path / "AppData" / "Roaming"
    vpnte_dir = appdata / "vpn-tunnel-enforcer"
    vpnte_dir.mkdir(parents=True)
    token_file = tmp_path / "control" / "external-proxy-control-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("endpoint-token\n", encoding="utf-8")
    (vpnte_dir / "external-proxy-control-endpoint.json").write_text(
        json.dumps({"url": "http://127.0.0.1:19006", "tokenFile": str(token_file)}),
        encoding="utf-8",
    )
    responses.extend(
        [
            {"ok": True},
            {"instances": [{"running": True, "proxyUrl": "http://127.0.0.1:18100", "slot": 111}]},
        ]
    )

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("VPNTE_CONTROL_URL", raising=False)
    monkeypatch.delenv("VPNTE_CONTROL_TOKEN", raising=False)

    status = VpnteProxyClient().rotate(slot=111)

    assert status["slot"] == 111
    assert calls[0]["url"] == "http://127.0.0.1:19006/rotate?slot=111"
    assert calls[0]["token"] == "endpoint-token"


def test_effective_proxy_url_caches_started_proxy(monkeypatch):
    clear_vpnte_proxy_cache()
    monkeypatch.setenv("VPNTE_PROXY_ENABLED", "true")
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19002")
    monkeypatch.setenv("VPNTE_PROXY_CACHE_TTL", "60")

    seen = {"calls": 0}

    def fake_urlopen(request, timeout):
        seen["calls"] += 1
        return FakeResponse(
            {
                "instances": [
                    {"slot": 1, "running": True, "proxyUrl": "http://127.0.0.1:17990"},
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert effective_proxy_url(rotate=False, fallback=None) == "http://127.0.0.1:17990"
    assert effective_proxy_url(rotate=False, fallback=None) == "http://127.0.0.1:17990"
    assert seen["calls"] == 2


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


def test_instances_preserves_dynamic_slots_and_authoritative_proxy_urls(monkeypatch, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19007")
    responses.append(
        {
            "instances": [
                {"slot": 111, "running": True, "proxyUrl": "http://127.0.0.1:18100"},
                {"slot": 300, "running": True, "proxyUrl": "http://127.0.0.1:18289"},
            ]
        }
    )

    instances = VpnteProxyClient().instances()

    assert [(item["slot"], item["proxyUrl"]) for item in instances] == [
        (111, "http://127.0.0.1:18100"),
        (300, "http://127.0.0.1:18289"),
    ]
    assert calls[0]["url"] == "http://127.0.0.1:19007/instances"


def test_instances_discards_rows_without_real_slot_or_proxy_url(monkeypatch, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19008")
    responses.append(
        {
            "instances": [
                {"running": True, "host": "127.0.0.1", "port": 17990},
                {"slot": 111, "running": True, "host": "127.0.0.1", "port": 18100},
            ]
        }
    )

    instances = VpnteProxyClient().instances()

    assert instances == [{"slot": 111, "running": True, "host": "127.0.0.1", "port": 18100}]
    assert calls[0]["url"] == "http://127.0.0.1:19008/instances"


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
            {"ok": True},
            {"instances": [{"slot": 3, "running": True, "proxyUrl": "http://127.0.0.1:17992"}]},
            {"ok": True},
            {"instances": [{"slot": 3, "running": True, "proxyUrl": "http://127.0.0.1:17992"}]},
            {"ok": True},
            {"instances": []},
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
        "http://127.0.0.1:19005/instances",
        "http://127.0.0.1:19005/rotate?slot=3&country=Germany",
        "http://127.0.0.1:19005/instances",
        "http://127.0.0.1:19005/stop?slot=3",
        "http://127.0.0.1:19005/instances",
        "http://127.0.0.1:19005/status?slot=3",
    ]
    assert [call["method"] for call in calls] == ["GET", "POST", "GET", "POST", "GET", "POST", "GET", "GET"]
    assert [call["token"] for call in calls] == [None, "fixture-token", None, "fixture-token", None, "fixture-token", None, None]


def test_connect_and_trigger_use_explicit_slot_and_profile(monkeypatch, fake_vpnte_control):
    calls, responses = fake_vpnte_control
    monkeypatch.setenv("VPNTE_CONTROL_URL", "http://127.0.0.1:19009")
    monkeypatch.setenv("VPNTE_CONTROL_TOKEN", "fixture-token")
    responses.extend(
        [
            {"ok": True},
            {"instances": [{"slot": 111, "running": True, "proxyUrl": "http://127.0.0.1:18100"}]},
            {"ok": True},
            {"instances": [{"slot": 111, "running": True, "proxyUrl": "http://127.0.0.1:18100"}]},
        ]
    )

    client = VpnteProxyClient()
    assert client.connect(111, "pl-vless-1")["proxyUrl"] == "http://127.0.0.1:18100"
    assert client.trigger(111, "pl-vless-2")["proxyUrl"] == "http://127.0.0.1:18100"

    assert [call["url"] for call in calls] == [
        "http://127.0.0.1:19009/connect?slot=111&id=pl-vless-1",
        "http://127.0.0.1:19009/instances",
        "http://127.0.0.1:19009/trigger?slot=111&id=pl-vless-2",
        "http://127.0.0.1:19009/instances",
    ]
    assert [call["method"] for call in calls] == ["POST", "GET", "POST", "GET"]
    assert [call["token"] for call in calls] == ["fixture-token", None, "fixture-token", None]
