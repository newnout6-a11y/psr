import json
import urllib.error

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
