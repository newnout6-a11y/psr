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
from typing import Any, Mapping

from loguru import logger


DEFAULT_CONTROL_URL = "http://127.0.0.1:17873"
DEFAULT_PROXY_URL = "http://127.0.0.1:17990"
MIN_PROXY_SLOT = 1
# VPNTE allocates slots dynamically.  Keep the old name for callers that
# imported it, but do not use a synthetic upper bound in validation.
MAX_PROXY_SLOTS: int | None = None
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


def _vpnte_appdata_dirs() -> tuple[Path, ...]:
    """Return both VPNTE AppData registrations, without assuming casing."""

    appdata = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    paths = (appdata / "vpn-tunnel-enforcer", appdata / "VPN Tunnel Enforcer")
    return tuple(dict.fromkeys(paths))


def _endpoint_file_candidates() -> tuple[Path, ...]:
    return tuple(directory / "external-proxy-control-endpoint.json" for directory in _vpnte_appdata_dirs())


def _endpoint_url(data: Mapping[str, Any]) -> str | None:
    url = str(data.get("url") or "").strip()
    if url:
        return url.rstrip("/")
    host = str(data.get("host") or "").strip()
    port = data.get("port")
    if host and port:
        return f"http://{host}:{port}"
    return None


def _read_endpoint_config() -> tuple[dict[str, Any], Path] | None:
    """Read the newest valid control endpoint registration.

    VPNTE has used both ``vpn-tunnel-enforcer`` and ``VPN Tunnel Enforcer``
    directories.  Selecting the newest valid file avoids retaining a stale
    control port after the desktop application restarts.
    """

    candidates: list[tuple[int, int, dict[str, Any], Path]] = []
    for order, path in enumerate(_endpoint_file_candidates()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not _endpoint_url(data):
                continue
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        except json.JSONDecodeError as exc:
            logger.debug(f"VPNTE proxy endpoint file is invalid: {exc}")
            continue
        candidates.append((mtime, -order, data, path))
    if not candidates:
        return None
    _, _, data, path = max(candidates, key=lambda item: (item[0], item[1]))
    return data, path


def _read_endpoint_file() -> str | None:
    endpoint = _read_endpoint_config()
    if endpoint is None:
        return None
    data, _ = endpoint
    return _endpoint_url(data)


def _token_file_candidates() -> tuple[Path, ...]:
    endpoint = _read_endpoint_config()
    paths: list[Path] = []
    if endpoint is not None:
        data, endpoint_path = endpoint
        token_file = data.get("tokenFile") or data.get("token_file")
        if token_file:
            expanded = os.path.expandvars(str(token_file).strip())
            token_path = Path(expanded).expanduser()
            if not token_path.is_absolute():
                token_path = endpoint_path.parent / token_path
            paths.append(token_path)
    paths.extend(directory / "external-proxy-control-token" for directory in _vpnte_appdata_dirs())
    return tuple(dict.fromkeys(paths))


def _read_token_file() -> str | None:
    for path in _token_file_candidates():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if token:
            return token
    return None


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
    endpoint = _read_endpoint_config()
    endpoint_files = _endpoint_file_candidates()
    token_files = _token_file_candidates()
    client = VpnteProxyClient()
    return {
        "enabled": vpnte_proxy_enabled(),
        "rotate_on_next": _truthy("VPNTE_PROXY_ROTATE_ON_NEXT", True),
        "strict": _truthy("VPNTE_PROXY_STRICT", True),
        "country": os.getenv("VPNTE_PROXY_COUNTRY", "").strip(),
        "profile_id": os.getenv("VPNTE_PROXY_PROFILE_ID", "").strip(),
        "slot": os.getenv("VPNTE_PROXY_SLOT", "").strip(),
        "port": os.getenv("VPNTE_PROXY_PORT", "17990").strip() or "17990",
        "control_url": client.control_url,
        "control_url_source": "env" if os.getenv("VPNTE_CONTROL_URL", "").strip() else ("file" if endpoint else "default"),
        "token_configured": bool(client.token),
        "token_source": "env" if os.getenv("VPNTE_CONTROL_TOKEN", "").strip() else ("file" if any(path.exists() for path in token_files) else ""),
        "appdata_dir": str(appdata_dir),
        "endpoint_file_exists": any(path.exists() for path in endpoint_files),
        "token_file_exists": any(path.exists() for path in token_files),
        "fallback_proxy": _configured_fallback_proxy() or "",
        "cached_proxy": _cache_get() or "",
    }


def _normalize_slot(slot: int | str | None) -> int | None:
    if slot is None:
        return None
    if isinstance(slot, bool):
        raise ValueError(f"VPNTE slot must be an integer >= {MIN_PROXY_SLOT}")
    try:
        normalized = int(slot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VPNTE slot must be an integer >= {MIN_PROXY_SLOT}") from exc
    if normalized < MIN_PROXY_SLOT:
        raise ValueError(f"VPNTE slot must be an integer >= {MIN_PROXY_SLOT}")
    return normalized


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_instance(payload: dict[str, Any], *, fallback_slot: int | None = None) -> dict[str, Any]:
    normalized = dict(payload)
    slot = _optional_int(payload.get("slot")) or fallback_slot
    if slot is not None:
        normalized["slot"] = slot

    for canonical, aliases in {
        "proxyUrl": ("proxyUrl", "proxy_url"),
        "profileId": ("profileId", "profile_id"),
        "profileName": ("profileName", "profile_name"),
        "startedAt": ("startedAt", "started_at"),
    }.items():
        for name in aliases:
            if name in payload:
                normalized[canonical] = payload[name]
                break

    for numeric in ("port", "pid"):
        if numeric in payload:
            value = _optional_int(payload[numeric])
            if value is not None:
                normalized[numeric] = value

    running = payload.get("running")
    if isinstance(running, str):
        normalized["running"] = running.strip().lower() in TRUE_VALUES
    return normalized


def _authoritative_instance(
    instances: list[dict[str, Any]],
    *,
    slot: int | None,
) -> dict[str, Any] | None:
    """Select a live instance only from the authoritative ``/instances`` list."""

    selected = VpnteProxyClient._select_instance(instances, slot=slot)
    if selected is None:
        return None
    proxy_url = str(selected.get("proxyUrl") or "").strip()
    return selected if proxy_url else None


def _configured_proxy_slot() -> int | None:
    raw = os.getenv("VPNTE_PROXY_SLOT", "").strip()
    if not raw:
        return None
    return _normalize_slot(raw)


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

    def instances(self) -> list[dict[str, Any]]:
        payload = self._request("/instances")
        rows = payload.get("instances")
        if not isinstance(rows, list):
            rows = payload.get("value")
        # Older VPNTE builds returned one instance directly.  Accept that
        # shape while still making /instances the only discovery request.
        if not isinstance(rows, list) and (
            "proxyUrl" in payload or "proxy_url" in payload or "slot" in payload
        ):
            rows = [payload]
        if not isinstance(rows, list):
            return []
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized = _normalize_instance(row)
            # /instances contains only live processes. Some VPNTE versions
            # omit `running`, but a missing slot is not safely inferable.
            if "running" not in normalized:
                normalized["running"] = True
            if _optional_int(normalized.get("slot")) is None:
                continue
            normalized_rows.append(normalized)
        return normalized_rows

    def status(self, slot: int | str | None = None) -> dict[str, Any]:
        normalized_slot = _normalize_slot(slot)
        payload = self._request("/status", params={"slot": normalized_slot})
        return _normalize_instance(payload, fallback_slot=normalized_slot)

    def list(self, country: str | None = None) -> list[dict[str, Any]]:
        payload = self._request("/list", params={"country": country})
        rows = payload.get("profiles", [])
        return rows if isinstance(rows, list) else []

    def start(
        self,
        slot: int | str | None = None,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        normalized_slot = _normalize_slot(slot)
        params = {
            "slot": normalized_slot,
            "country": country if normalized_slot is not None else (country or os.getenv("VPNTE_PROXY_COUNTRY")),
            "profileId": profile_id
            if normalized_slot is not None
            else (profile_id or os.getenv("VPNTE_PROXY_PROFILE_ID")),
            "port": port if normalized_slot is not None else (port or os.getenv("VPNTE_PROXY_PORT")),
        }
        self._request(
            "/start",
            method="POST",
            auth=True,
            params=params,
        )
        refreshed = self.instances()
        selected = _authoritative_instance(refreshed, slot=normalized_slot)
        if selected is None:
            raise RuntimeError(f"VPNTE /instances did not confirm started slot {normalized_slot}")
        return selected

    def rotate(
        self,
        slot: int | str | None = None,
        *,
        country: str | None = None,
        profile_id: str | None = None,
        port: int | str | None = None,
    ) -> dict[str, Any]:
        normalized_slot = _normalize_slot(slot)
        params = {
            "slot": normalized_slot,
            "country": country if normalized_slot is not None else (country or os.getenv("VPNTE_PROXY_COUNTRY")),
            "profileId": profile_id
            if normalized_slot is not None
            else (profile_id or os.getenv("VPNTE_PROXY_PROFILE_ID")),
            "port": port if normalized_slot is not None else (port or os.getenv("VPNTE_PROXY_PORT")),
        }
        self._request(
            "/rotate",
            method="POST",
            auth=True,
            params=params,
        )
        refreshed = self.instances()
        selected = _authoritative_instance(refreshed, slot=normalized_slot)
        if selected is None:
            raise RuntimeError(f"VPNTE /instances did not confirm rotated slot {normalized_slot}")
        return selected

    def connect(
        self,
        slot: int | str,
        profile_id: str,
    ) -> dict[str, Any]:
        """Connect a slot to an explicit VPN profile and confirm it via instances."""

        normalized_slot = _normalize_slot(slot)
        profile = str(profile_id).strip()
        if not profile:
            raise ValueError("profile_id is required")
        self._request(
            "/connect",
            method="POST",
            auth=True,
            params={"slot": normalized_slot, "id": profile},
        )
        selected = _authoritative_instance(self.instances(), slot=normalized_slot)
        if selected is None:
            raise RuntimeError(f"VPNTE /instances did not confirm connected slot {normalized_slot}")
        return selected

    def trigger(
        self,
        slot: int | str,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Trigger/reconnect a slot, optionally selecting an explicit profile."""

        normalized_slot = _normalize_slot(slot)
        params: dict[str, str | int | None] = {"slot": normalized_slot}
        if profile_id is not None and str(profile_id).strip():
            params["id"] = str(profile_id).strip()
        self._request("/trigger", method="POST", auth=True, params=params)
        selected = _authoritative_instance(self.instances(), slot=normalized_slot)
        if selected is None:
            raise RuntimeError(f"VPNTE /instances did not confirm triggered slot {normalized_slot}")
        return selected

    def stop(self, slot: int | str) -> dict[str, Any]:
        normalized_slot = _normalize_slot(slot)
        self._request(
            "/stop",
            method="POST",
            auth=True,
            params={"slot": normalized_slot},
        )
        refreshed = self.instances()
        selected = next((item for item in refreshed if item.get("slot") == normalized_slot), None)
        if selected is not None:
            return selected
        # A stopped slot is intentionally absent from /instances.
        try:
            status = self.status(normalized_slot)
        except Exception as exc:  # noqa: BLE001 - stop already completed; retain diagnostic context.
            status = {"error": str(exc)}
        return {"slot": normalized_slot, "running": False, "proxyUrl": None, "status": status}

    def ensure_started(self) -> dict[str, Any]:
        instances = self.instances()
        selected = self._select_instance(instances)
        if selected is not None:
            return selected
        slot = _configured_proxy_slot()
        # Probe the configured slot for diagnostics, but never use a proxy URL
        # from /status as traffic routing; /instances remains authoritative.
        try:
            self.status(slot=slot)
        except Exception:
            pass
        return self.start(slot=slot)

    @staticmethod
    def _select_instance(
        instances: list[dict[str, Any]],
        *,
        slot: int | None = None,
    ) -> dict[str, Any] | None:
        target_slot = slot if slot is not None else _configured_proxy_slot()
        candidates = [
            item
            for item in instances
            if item.get("proxyUrl") and item.get("running") is True
        ]
        if target_slot is not None:
            candidates = [item for item in candidates if item.get("slot") == target_slot]
        return min(
            candidates,
            key=lambda item: (_optional_int(item.get("slot")) is None, _optional_int(item.get("slot")) or 0),
            default=None,
        )

    def next_proxy(self, *, rotate: bool) -> str:
        instances = self.instances()
        selected = self._select_instance(instances)
        if selected is None:
            selected = self.ensure_started()
        slot = _optional_int(selected.get("slot"))
        if rotate and slot is not None:
            self.rotate(slot=slot)
            selected = _authoritative_instance(self.instances(), slot=slot)
            if selected is None:
                raise RuntimeError(f"VPNTE /instances did not confirm rotated slot {slot}")
        proxy_url = str(selected.get("proxyUrl") or "").strip()
        if not proxy_url:
            raise RuntimeError("VPNTE /instances returned no running proxyUrl")
        return proxy_url


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
