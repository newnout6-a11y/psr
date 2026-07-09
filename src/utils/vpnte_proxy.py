"""Client for VPN Tunnel Enforcer external proxy control API.

The data path is stable: local clients keep using one HTTP/SOCKS proxy URL,
while the VPNTE control API rotates the VPN profile behind that port.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger


DEFAULT_CONTROL_URL = "http://127.0.0.1:17873"
DEFAULT_PROXY_URL = "http://127.0.0.1:17990"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
_PROXY_CACHE_LOCK = RLock()
_PROXY_CACHE: dict[str, Any] = {"url": None, "created": 0.0}


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def vpnte_proxy_enabled() -> bool:
    return (
        _truthy("VPNTE_PROXY_ENABLED")
        or _truthy("VPNTE_PROXY")
        or _truthy("VPNTE_EXTERNAL_PROXY")
    )


def _vpnte_appdata_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "VPN Tunnel Enforcer"
    return Path.home() / "AppData" / "Roaming" / "VPN Tunnel Enforcer"


def _read_endpoint_file() -> str | None:
    path = _vpnte_appdata_dir() / "external-proxy-control-endpoint.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError as exc:
        logger.debug(f"VPNTE proxy endpoint file is invalid: {exc}")
        return None

    url = str(data.get("url") or "").strip()
    if url:
        return url.rstrip("/")
    host = str(data.get("host") or "").strip()
    port = data.get("port")
    if host and port:
        return f"http://{host}:{port}"
    return None


def _read_token_file() -> str | None:
    path = _vpnte_appdata_dir() / "external-proxy-control-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _proxy_cache_ttl() -> float:
    try:
        return max(0.0, float(os.getenv("VPNTE_PROXY_CACHE_TTL", "60") or "60"))
    except (TypeError, ValueError):
        return 60.0


def _configured_fallback_proxy() -> str | None:
    proxy = os.getenv("PROXY_URL", "").strip()
    if proxy:
        return proxy
    proxy_list = os.getenv("KWORK_PROXY_LIST", "")
    for item in proxy_list.split(","):
        item = item.strip()
        if item:
            return item
    return None


def _cache_get() -> str | None:
    ttl = _proxy_cache_ttl()
    if ttl <= 0:
        return None
    import time

    with _PROXY_CACHE_LOCK:
        url = _PROXY_CACHE.get("url")
        created = float(_PROXY_CACHE.get("created") or 0.0)
    if url and time.monotonic() - created <= ttl:
        return str(url)
    return None


def _cache_set(proxy_url: str | None) -> None:
    import time

    with _PROXY_CACHE_LOCK:
        _PROXY_CACHE["url"] = proxy_url or None
        _PROXY_CACHE["created"] = time.monotonic() if proxy_url else 0.0


def clear_vpnte_proxy_cache() -> None:
    _cache_set(None)


def vpnte_config_snapshot() -> dict[str, Any]:
    appdata_dir = _vpnte_appdata_dir()
    endpoint_file = appdata_dir / "external-proxy-control-endpoint.json"
    token_file = appdata_dir / "external-proxy-control-token"
    client = VpnteProxyClient()
    return {
        "enabled": vpnte_proxy_enabled(),
        "rotate_on_next": _truthy("VPNTE_PROXY_ROTATE_ON_NEXT", True),
        "strict": _truthy("VPNTE_PROXY_STRICT", True),
        "country": os.getenv("VPNTE_PROXY_COUNTRY", "").strip(),
        "profile_id": os.getenv("VPNTE_PROXY_PROFILE_ID", "").strip(),
        "port": os.getenv("VPNTE_PROXY_PORT", "17990").strip() or "17990",
        "control_url": client.control_url,
        "control_url_source": "env" if os.getenv("VPNTE_CONTROL_URL", "").strip() else ("file" if endpoint_file.exists() else "default"),
        "token_configured": bool(client.token),
        "token_source": "env" if os.getenv("VPNTE_CONTROL_TOKEN", "").strip() else ("file" if token_file.exists() else ""),
        "appdata_dir": str(appdata_dir),
        "endpoint_file_exists": endpoint_file.exists(),
        "token_file_exists": token_file.exists(),
        "fallback_proxy": _configured_fallback_proxy() or "",
        "cached_proxy": _cache_get() or "",
    }


class VpnteProxyClient:
    def __init__(self) -> None:
        self.timeout = float(os.getenv("VPNTE_PROXY_TIMEOUT", "10") or "10")

    @property
    def control_url(self) -> str:
        return (
            os.getenv("VPNTE_CONTROL_URL", "").strip().rstrip("/")
            or _read_endpoint_file()
            or DEFAULT_CONTROL_URL
        )

    @property
    def token(self) -> str | None:
        return os.getenv("VPNTE_CONTROL_TOKEN", "").strip() or _read_token_file()

    def _query(self, params: dict[str, str | int | None]) -> str:
        clean = {k: str(v) for k, v in params.items() if v is not None and str(v).strip()}
        return urllib.parse.urlencode(clean)

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, str | int | None] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        query = self._query(params or {})
        url = f"{self.control_url}{path}"
        if query:
            url += f"?{query}"

        headers: dict[str, str] = {"Accept": "application/json"}
        data: bytes | None = None
        if method.upper() != "GET":
            token = self.token
            if auth and not token:
                raise RuntimeError("VPNTE control token not found")
            if token:
                headers["X-VPNTE-Control-Token"] = token
            headers["Content-Type"] = "application/json"
            data = b"{}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"VPNTE proxy API HTTP {exc.code}: {body or exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"VPNTE proxy API unavailable at {self.control_url}: {exc}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def status(self) -> dict[str, Any]:
        return self._request("/status")

    def list(self, country: str | None = None) -> list[dict[str, Any]]:
        payload = self._request("/list", params={"country": country})
        rows = payload.get("profiles", [])
        return rows if isinstance(rows, list) else []

    def start(self) -> dict[str, Any]:
        return self._request(
            "/start",
            method="POST",
            auth=True,
            params={
                "country": os.getenv("VPNTE_PROXY_COUNTRY"),
                "profileId": os.getenv("VPNTE_PROXY_PROFILE_ID"),
                "port": os.getenv("VPNTE_PROXY_PORT"),
            },
        )

    def rotate(self) -> dict[str, Any]:
        return self._request(
            "/rotate",
            method="POST",
            auth=True,
            params={
                "country": os.getenv("VPNTE_PROXY_COUNTRY"),
                "profileId": os.getenv("VPNTE_PROXY_PROFILE_ID"),
                "port": os.getenv("VPNTE_PROXY_PORT"),
            },
        )

    def ensure_started(self) -> dict[str, Any]:
        status = self.status()
        if status.get("running") and status.get("proxyUrl"):
            return status
        return self.start()

    def next_proxy(self, *, rotate: bool) -> str:
        status = self.rotate() if rotate else self.ensure_started()
        proxy_url = str(status.get("proxyUrl") or "").strip()
        if proxy_url:
            return proxy_url
        if status.get("running") and status.get("host") and status.get("port"):
            return f"http://{status['host']}:{status['port']}"
        return os.getenv("VPNTE_PROXY_URL", "").strip() or DEFAULT_PROXY_URL


_client: VpnteProxyClient | None = None


def get_vpnte_proxy_client() -> VpnteProxyClient:
    global _client
    if _client is None:
        _client = VpnteProxyClient()
    return _client


def effective_proxy_url(*, rotate: bool = False, fallback: str | None = None) -> str | None:
    if not vpnte_proxy_enabled():
        return fallback
    try:
        if not rotate:
            cached = _cache_get()
            if cached:
                return cached
        proxy_url = get_vpnte_proxy_client().next_proxy(rotate=rotate)
        _cache_set(proxy_url)
        logger.info(f"VPNTE proxy active: {proxy_url}")
        return proxy_url
    except Exception as exc:
        if _truthy("VPNTE_PROXY_STRICT", True):
            raise
        logger.warning(f"VPNTE proxy unavailable, falling back to configured proxy: {exc}")
        return fallback


def rotate_vpnte_proxy_if_enabled() -> str | None:
    if not vpnte_proxy_enabled():
        return None
    return effective_proxy_url(rotate=True)


def kwork_http_proxy_url(*, rotate: bool = False) -> str | None:
    fallback = _configured_fallback_proxy()
    if vpnte_proxy_enabled():
        return effective_proxy_url(rotate=rotate, fallback=fallback)
    return fallback
