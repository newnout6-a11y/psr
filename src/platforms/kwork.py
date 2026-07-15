"""Kwork integration primitives.

Kwork pages expose a large `window.stateData` payload. Parsing that payload is
more stable than relying only on CSS classes, and it gives us fields that are
not always present in the rendered card text: buyer hiring percent, files,
dates, category ids, price limits and raw user metadata.
"""

from __future__ import annotations

import asyncio
import ipaddress
import inspect
import json
import os
import re
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import Cookie
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from loguru import logger
from yarl import URL

from src.paths import KWORK_MANUAL_COOKIES_FILE
from src.platforms.kwork_account_store import KworkAccountStore, KworkAccountStoreError, StoredKworkAccount
from src.platforms.kwork_ext import KworkExtensions, apply_kwork_patches
from src.platforms.kwork_ext import get_pacer
from src.utils.vpnte_proxy import kwork_http_proxy_url

apply_kwork_patches()

from src.parsers.base_parser import ProjectItem


KWORK_BASE_URL = "https://kwork.ru"
KWORK_REGISTRATION_USER_AGENT = "Mozilla/5.0 PSR-KworkRegistration/1.0"
STATE_MARKER = "window.stateData="

_KWORK_USERNAME_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)
_KWORK_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_KWORK_USERNAME_PREFIXES = ("bright", "calm", "clear", "north", "silver", "smart", "steady", "swift")
_KWORK_USERNAME_WORDS = ("bridge", "canvas", "craft", "forge", "frame", "mosaic", "orbit", "pixel")
_KWORK_DEFAULT_BLOCKED_DOMAINS = {
    "10minutemail.com",
    "dispostable.com",
    "guerrillamail.com",
    "mailinator.com",
    "tempmail.com",
    "yopmail.com",
    "bekommenmail.com",
}


class KworkRegistrationError(ValueError):
    """A user-correctable registration preflight or signup failure."""

    def __init__(self, code: str, message: str, *, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = dict(payload or {})


@dataclass(frozen=True, slots=True)
class _RegistrationRouteProbe:
    slot: int
    proxy: str
    egress_ip: str | None
    elapsed_ms: int
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.egress_ip is not None and self.error is None


class KworkAPIResponseError(RuntimeError):
    """Raised when Kwork mobile API returns an unexpected projects payload."""

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value).replace(" ", "").replace("\xa0", "")))
    except Exception:
        return default


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).replace(" ", "").replace("\xa0", ""))
    except Exception:
        return None


def _repair_text_encoding(text: str) -> str:
    bad_sequences = (
        "\u0420\u040e",
        "\u0420\u00a0",
        "\u0420\u0454",
        "\u0420\u00b0",
        "\u0420\u00b5",
        "\u0421\u0402",
        "\u0421\u201a",
        "\u0421\u0403",
        "\u0421\u0152",
        "\u0432\u0402",
        "\u0412\xa0",
        "\u0420\u0403",
        "\u0421\u2018",
        "\u00d0",
        "\u00d1",
        "\u00f0",
        "\u00f2",
        "\u00e5",
        "\u00eb",
        "\u00e8",
        "\u00e0",
        "\u00ea",
    )
    if not text:
        return text

    def _bad_score(value: str) -> int:
        high_latin = sum(1 for char in value if "\u00c0" <= char <= "\u00ff")
        return high_latin + sum(value.count(marker) * 4 for marker in bad_sequences)

    def _cyrillic_score(value: str) -> int:
        return sum(1 for char in value if "\u0400" <= char <= "\u04ff")

    current = text
    for _ in range(3):
        current_bad_score = _bad_score(current)
        current_cyrillic_score = _cyrillic_score(current)
        if current_bad_score <= 0:
            break
        best = current
        best_score = 0
        for source_encoding, target_encoding in (("latin1", "utf-8"), ("cp1251", "utf-8"), ("latin1", "cp1251")):
            try:
                candidate = current.encode(source_encoding).decode(target_encoding)
            except UnicodeError:
                continue
            candidate_bad_score = _bad_score(candidate)
            candidate_cyrillic_score = _cyrillic_score(candidate)
            if candidate_cyrillic_score <= 0:
                continue
            score = ((current_bad_score - candidate_bad_score) * 3) + (
                (candidate_cyrillic_score - current_cyrillic_score) * 2
            )
            if score > best_score and candidate != current:
                best = candidate
                best_score = score
        if best == current:
            break
        current = best
    return current


def _strip_html(value: Any) -> str:
    text = str(value or "")
    if "<" not in text and "&" not in text:
        return _repair_text_encoding(text).strip()
    try:
        from bs4 import BeautifulSoup

        return _repair_text_encoding(BeautifulSoup(text, "lxml").get_text(" ", strip=True))
    except Exception:
        return _repair_text_encoding(text).strip()


def parse_cookie_env(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(key): str(value) for key, value in parsed.items() if key and value not in (None, "")}
        if isinstance(parsed, list):
            result: dict[str, str] = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name and value:
                    result[name] = value
            return result
    except json.JSONDecodeError:
        pass

    result: dict[str, str] = {}
    for part in re.split(r"[;\n]+", raw):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name and value:
            result[name] = value
    return result


def env_kwork_web_cookies() -> dict[str, str]:
    cookies: dict[str, str] = {}
    for key in ("KWORK_WEB_COOKIES_JSON", "KWORK_WEB_COOKIES_RAW", "KWORK_COOKIES_JSON", "KWORK_COOKIES_RAW"):
        cookies.update(parse_cookie_env(os.getenv(key, "")))

    env_map = {
        "slrememberme": ("KWORK_COOKIE_SLREMEMBERME", "KWORK_COOKIE_REMEMBERME"),
        "userId": ("KWORK_COOKIE_USERID", "KWORK_COOKIE_USER_ID"),
        "uad": ("KWORK_COOKIE_UAD",),
        "csrf_user_token": ("KWORK_COOKIE_CSRF_USER_TOKEN", "KWORK_CSRF_USER_TOKEN", "KWORK_CSRF_TOKEN"),
        "RORSSQIHEK": ("KWORK_COOKIE_RORSSQIHEK",),
        "_kmid": ("KWORK_COOKIE_KMID",),
        "_kmfvt": ("KWORK_COOKIE_KMFVT",),
        "_kmwl": ("KWORK_COOKIE_KMWL",),
        "PHPSESSID": ("KWORK_COOKIE_PHPSESSID",),
    }
    for cookie_name, env_names in env_map.items():
        for env_name in env_names:
            value = os.getenv(env_name, "").strip()
            if value:
                cookies[cookie_name] = value
                break
    return {name: value for name, value in cookies.items() if name and value}


def manual_kwork_web_cookies() -> dict[str, str]:
    """Cookies saved by the in-app manual Kwork verification window."""
    try:
        if not KWORK_MANUAL_COOKIES_FILE.exists():
            return {}
        data = json.loads(KWORK_MANUAL_COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

    items = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}

    now = time.time()
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if "kwork.ru" not in domain:
            continue
        expires = item.get("expirationDate")
        try:
            if expires and float(expires) < now:
                continue
        except (TypeError, ValueError):
            pass
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            result[name] = value
    return result


def merge_manual_kwork_cookies(cookies: dict[str, str]) -> dict[str, str]:
    merged = dict(cookies or {})
    merged.update(manual_kwork_web_cookies())
    return merged


def _extract_json_object_after_marker(html: str, marker: str = STATE_MARKER) -> str | None:
    start = html.find(marker)
    if start < 0:
        return None

    index = html.find("{", start + len(marker))
    if index < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    quote = ""

    for pos in range(index, len(html)):
        char = html[pos]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[index : pos + 1]

    return None


class KworkStateDataParser:
    """Parser for Kwork's public `window.stateData` JSON payload."""

    @staticmethod
    def extract(html: str) -> dict[str, Any] | None:
        raw = _extract_json_object_after_marker(html)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.debug(f"Kwork stateData JSON parse failed: {e}")
            return None

    @classmethod
    def projects_from_html(cls, html: str) -> list[ProjectItem]:
        state = cls.extract(html)
        return cls.projects_from_state(state or {})

    @classmethod
    def project_from_html(cls, html: str, project_id: str | None = None) -> ProjectItem | None:
        state = cls.extract(html)
        if not state:
            return None
        return cls.project_from_state(state, project_id=project_id)

    @classmethod
    def projects_from_state(cls, state: dict[str, Any]) -> list[ProjectItem]:
        wants = state.get("wants")
        if isinstance(wants, dict):
            wants = wants.get("data") or wants.get("items") or wants.get("wants")
        if not wants:
            pagination = state.get("pagination") or {}
            wants = pagination.get("data")
        if not isinstance(wants, list):
            return []
        projects = []
        for want in wants:
            if isinstance(want, dict):
                project = cls._project_from_want(want)
                if project:
                    projects.append(project)
        return projects

    @classmethod
    def project_from_state(cls, state: dict[str, Any], project_id: str | None = None) -> ProjectItem | None:
        want = state.get("wantData")
        if isinstance(want, dict):
            return cls._project_from_want(want)

        projects = cls.projects_from_state(state)
        if project_id:
            for project in projects:
                if str(project.id) == str(project_id):
                    return project
        return projects[0] if projects else None

    @staticmethod
    def _user_payload(want: dict[str, Any]) -> dict[str, Any]:
        user = want.get("user") or {}
        return user if isinstance(user, dict) else {}

    @classmethod
    def _client_hired_percent(cls, want: dict[str, Any]) -> int:
        user = cls._user_payload(want)
        data = user.get("data") or {}
        return _as_int(
            want.get("user_hired_percent")
            or want.get("wants_hired_percent")
            or data.get("wants_hired_percent")
            or data.get("order_done_repeat_persent")
        )

    @classmethod
    def _project_from_want(cls, want: dict[str, Any]) -> ProjectItem | None:
        project_id = str(want.get("id") or "").strip()
        if not project_id:
            return None

        user = cls._user_payload(want)
        title = _strip_html(want.get("name") or want.get("title") or "")
        description = _strip_html(want.get("description") or "")
        _price = _as_float(want.get("priceLimit"))
        budget = _price if _price is not None and _price > 0 else _as_float(want.get("possiblePriceLimit"))
        _offers = _as_int(want.get("kwork_count"))
        offers_count = _offers if _offers is not None and _offers >= 0 else _as_int(want.get("offers"))
        client_user_id = str(user.get("USERID") or user.get("id") or want.get("user_id") or "").strip() or None
        client_hired_percent = cls._client_hired_percent(want)
        files = want.get("files") if isinstance(want.get("files"), list) else []

        platform_data = {
            "source": "kwork_state_data",
            "category_id": want.get("category_id"),
            "classification_id": want.get("classificationId") or want.get("classification_id"),
            "parent_category_id": want.get("parentCategoryId") or want.get("parent_category_id"),
            "possible_price_limit": want.get("possiblePriceLimit"),
            "is_higher_price": want.get("isHigherPrice"),
            "allow_higher_price": want.get("allowHigherPrice") or want.get("allow_higher_price"),
            "already_work": want.get("alreadyWork") or want.get("already_work"),
            "available_durations": want.get("availableDurations") or [],
            "max_days": want.get("max_days"),
            "date_active": want.get("date_active"),
            "date_expire": want.get("date_expire"),
            "date_create": want.get("date_create") or want.get("dateCreate"),
            "views_dirty": want.get("views_dirty"),
            "user_need_portfolio": want.get("userNeedPortfolio"),
            "user_active_projects_count": want.get("userActiveProjectsCount"),
            "user_projects_count": want.get("userProjectsCount"),
            "achievements_list": want.get("achievementsList") or want.get("achievements_list"),
            "files": files,
            "user": {
                "id": client_user_id,
                "username": user.get("username"),
                "badges": user.get("badges") or [],
                "data": user.get("data") or {},
                "profile_url": want.get("wantUserGetProfileUrl"),
            },
        }

        return ProjectItem(
            id=project_id,
            title=title,
            description=description,
            budget=budget,
            currency="RUB",
            skills=[],
            created_at=str(want.get("date_create") or want.get("date_active") or ""),
            url=f"{KWORK_BASE_URL}/projects/{project_id}",
            platform="kwork",
            client_user_id=client_user_id,
            offers_count=offers_count,
            client_hired_percent=client_hired_percent,
            platform_data=platform_data,
        )


@dataclass
class KworkService:
    """Shared Kwork API/session service.

    Authorization priority:
    1. Session Hub (HTTP GET, timeout 10s) — primary source of fresh cookies
    2. KWORK_EMAIL/KWORK_PASSWORD — fallback direct auth via mobile API
    3. Retry once with 5s delay on HTTP 401
    4. Max 3 session resets per parsing cycle (_reset_count)
    """

    timeout: float = 30.0
    retry_max_attempts: int = 2
    _MAX_RESETS_PER_CYCLE: int = 3

    def __post_init__(self) -> None:
        self._api: Any = None
        self._token_api: Any = None
        self._reset_count: int = 0
        self._session_hub_cookies: dict[str, str] = {}
        self._session_hub_cookies_at: float = 0.0
        self._session_hub_fetch_task: asyncio.Task[dict[str, str]] | None = None
        self._transport_recovery_lock = asyncio.Lock()
        self._registration_account_store: KworkAccountStore | None = None

    def _registration_store(self) -> KworkAccountStore:
        if self._registration_account_store is None:
            self._registration_account_store = KworkAccountStore()
        return self._registration_account_store

    def reset_cycle(self) -> None:
        """Reset per-cycle counters. Call at the start of each parsing cycle."""
        self._reset_count = 0

    async def get_api(self) -> Any | None:
        """Return an authorized API client or None.

        Priority:
        1. Return cached client if available
        2. Try Session Hub cookies
        3. Fallback to email/password auth
        4. Retry once with 5s delay on 401
        5. Return None without raising on final failure
        """
        if self._api is not None:
            if await self._cached_api_proxy_is_live(self._api):
                return self._api
            await self._invalidate_cached_api(self._api)

        # --- Step 1: Try Session Hub ---
        api = await self._try_session_hub()
        if api is not None:
            self._api = api
            return self._api

        # Check if Session Hub is required (strict mode)
        session_hub_required = os.getenv("SESSION_HUB_REQUIRED", "false").lower() == "true"
        if session_hub_required:
            logger.error(
                "KworkService: Session Hub недоступен, а SESSION_HUB_REQUIRED=true → отказ в инициализации. "
                "Запусти Session Hub: python scripts/session_hub/session_hub_manual.py"
            )
            return None

        # --- Step 2: Fallback to email/password ---
        api = await self._try_email_password()
        if api is not None:
            self._api = api
            return self._api

        # No credentials available
        return None

    async def _try_session_hub(self) -> Any | None:
        """Attempt to get cookies from Session Hub and create API client."""
        import httpx

        hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        email = os.getenv("KWORK_EMAIL", "")
        password = os.getenv("KWORK_PASSWORD", "")

        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(
                    f"{hub_url}?domain=kwork.ru",
                    timeout=10.0,
                )
            if resp.status_code != 200:
                logger.debug(f"KworkService: Session Hub вернул HTTP {resp.status_code}")
                cookies = []
            else:
                data = resp.json()
                cookies = data.get("cookies", [])
                if data.get("status") != "ok" and not cookies:
                    logger.debug(f"KworkService: Session Hub статус: {data.get('status')}")

            if not cookies:
                manual_cookies = manual_kwork_web_cookies()
                if manual_cookies:
                    cookies = [{"name": name, "value": value} for name, value in manual_cookies.items()]
                else:
                    logger.debug("KworkService: Session Hub вернул пустой список кук")
                    return None

            # Build cookie dict for kwork library
            cookie_dict: dict[str, str] = {}
            for c in cookies:
                name = c.get("name", "")
                value = c.get("value", "")
                if name and value:
                    cookie_dict[name] = value

            if not cookie_dict:
                logger.debug("KworkService: Session Hub куки пустые после фильтрации")
                return None

            cookie_dict = merge_manual_kwork_cookies(cookie_dict)
            self._session_hub_cookies = cookie_dict
            self._session_hub_cookies_at = time.monotonic()

            # Create Kwork client with cookies from Session Hub
            from kwork import Kwork
            from src.platforms.kwork_ext import get_proxy_rotator
            from src.utils.vpnte_proxy import vpnte_proxy_enabled

            phone = os.getenv("KWORK_PHONE", "")
            proxy = (
                get_proxy_rotator().next(rotate=False)
                if os.getenv("KWORK_PROXY_LIST") or vpnte_proxy_enabled()
                else (os.getenv("PROXY_URL") or None)
            )
            api = Kwork(
                login=email or "",
                password=password or "",
                phone_last=phone if phone else None,
                timeout=self.timeout,
                retry_max_attempts=max(1, self.retry_max_attempts),
                proxy=proxy,
            )
            api._psr_proxy_url = proxy
            # Force-create the underlying HTTP session before injecting cookies.
            self._apply_cookies_to_api(api, cookie_dict)
            auth_mode = "email+cookies" if email and password else "cookie-only"
            api._psr_auth_mode = auth_mode
            api._psr_recover_transport = self._recover_transport

            logger.info(f"KworkService: API-клиент инициализирован через Session Hub ({len(cookie_dict)} кук)")
            logger.debug(f"KworkService: Session Hub auth mode: {auth_mode}")
            return api

        except Exception as e:
            logger.debug(
                f"KworkService: не удалось создать API-клиент из Session Hub cookies "
                f"({type(e).__name__}: {e})"
            )
            return None

    async def _fetch_session_hub_cookies(self) -> dict[str, str]:
        ttl = 90.0
        try:
            ttl = max(5.0, float(os.getenv("KWORK_SESSION_HUB_COOKIE_TTL", "90")))
        except (TypeError, ValueError):
            ttl = 90.0
        if self._session_hub_cookies and time.monotonic() - self._session_hub_cookies_at < ttl:
            return dict(self._session_hub_cookies)
        if self._session_hub_fetch_task and not self._session_hub_fetch_task.done():
            return await self._session_hub_fetch_task
        self._session_hub_fetch_task = asyncio.create_task(self._fetch_session_hub_cookies_uncached())
        try:
            return await self._session_hub_fetch_task
        finally:
            if self._session_hub_fetch_task.done():
                self._session_hub_fetch_task = None

    async def _fetch_session_hub_cookies_uncached(self) -> dict[str, str]:
        """Fetch the latest Kwork cookies from Session Hub as a plain dict."""
        import httpx

        hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(f"{hub_url}?domain=kwork.ru", timeout=10.0)
            if resp.status_code != 200:
                logger.debug(f"KworkService: Session Hub returned HTTP {resp.status_code}")
                return {}

            data = resp.json()
            if data.get("status") != "ok" and not data.get("cookies"):
                logger.debug(f"KworkService: Session Hub status: {data.get('status')}")
                fallback = merge_manual_kwork_cookies(env_kwork_web_cookies())
                if fallback:
                    self._session_hub_cookies = fallback
                    self._session_hub_cookies_at = time.monotonic()
                return fallback

            cookie_dict: dict[str, str] = {}
            for c in data.get("cookies", []):
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name", "") or "")
                value = str(c.get("value", "") or "")
                if name and value:
                    cookie_dict[name] = value

            cookie_dict = merge_manual_kwork_cookies(cookie_dict)
            if cookie_dict:
                self._session_hub_cookies = cookie_dict
                self._session_hub_cookies_at = time.monotonic()
            return cookie_dict
        except Exception as e:
            logger.debug(f"KworkService: Session Hub unavailable ({e})")
            fallback = merge_manual_kwork_cookies(env_kwork_web_cookies())
            if fallback:
                self._session_hub_cookies = fallback
                self._session_hub_cookies_at = time.monotonic()
            return fallback

    @staticmethod
    def _web_cookie_dict(api: Any) -> dict[str, str]:
        try:
            jar = api.session.cookie_jar
        except Exception:
            return {}
        result: dict[str, str] = {}
        try:
            for item in jar:
                name = str(getattr(item, "key", "") or "").strip()
                value = str(getattr(item, "value", "") or "").strip()
                try:
                    domain = str(item["domain"] or "")
                except Exception:
                    domain = ""
                if name and value and (not domain or "kwork.ru" in domain):
                    result[name] = value
        except Exception as exc:
            logger.debug(f"KworkService: failed to export refreshed web cookies: {exc}")
        return result

    @staticmethod
    def _persist_manual_web_cookies(cookies: dict[str, str]) -> None:
        if not cookies:
            return
        payload = {
            "saved_at": datetime.now(UTC).isoformat(),
            "source": "mobile_web_auth_token",
            "cookies": [
                {
                    "name": name,
                    "value": value,
                    "domain": ".kwork.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
                for name, value in sorted(cookies.items())
            ],
        }
        try:
            KWORK_MANUAL_COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = KWORK_MANUAL_COOKIES_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(KWORK_MANUAL_COOKIES_FILE)
        except Exception as exc:
            logger.debug(f"KworkService: could not persist refreshed web cookies locally: {exc}")

    async def _persist_session_hub_cookies(self, cookies: dict[str, str]) -> None:
        if not cookies:
            return
        import httpx

        hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies").strip()
        update_url = f"{hub_url.rsplit('/', 1)[0]}/update"
        payload = {
            "domain": "kwork.ru",
            "cookies": [
                {"name": name, "value": value, "domain": "kwork.ru", "path": "/"}
                for name, value in sorted(cookies.items())
            ],
        }
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(update_url, json=payload, timeout=10.0)
            if response.status_code >= 400:
                logger.debug(f"KworkService: Session Hub cookie update returned HTTP {response.status_code}")
        except Exception as exc:
            logger.debug(f"KworkService: could not persist refreshed web cookies in Session Hub: {exc}")
        self._persist_manual_web_cookies(cookies)

    async def refresh_web_session_cookies(self, *, url_to_redirect: str = "/new") -> dict[str, str]:
        """Create a fresh kwork.ru web session through the official mobile web-auth flow."""

        api = await self.get_token_api()
        if api is None or not getattr(api, "web", None):
            return {}
        try:
            result = await api.web.login_via_mobile_web_auth_token(
                url_to_redirect=url_to_redirect,
                user_agent=os.getenv("KWORK_WEB_USER_AGENT", "Mozilla/5.0 PSR-KworkListing/1.0"),
            )
        except Exception as exc:
            logger.warning(f"KworkService: web auth token flow failed: {type(exc).__name__}: {exc}")
            return {}

        fresh = self._web_cookie_dict(api)
        if not fresh:
            logger.warning("KworkService: web auth token flow returned no kwork.ru cookies")
            return {}
        merged = merge_manual_kwork_cookies(
            {
                **env_kwork_web_cookies(),
                **self._session_hub_cookies,
            }
        )
        merged.update(fresh)
        self._session_hub_cookies = merged
        self._session_hub_cookies_at = time.monotonic()
        await self._persist_session_hub_cookies(merged)
        logger.info(
            "KworkService: refreshed web session through mobile auth token "
            f"({len(merged)} cookies, status={getattr(result, 'status', 'unknown')}, "
            f"url={getattr(result, 'final_url', '')})"
        )
        return dict(merged)

    def _apply_cookies_to_api(self, api: Any, cookies: dict[str, str] | None = None) -> bool:
        """Apply Session Hub cookies to the underlying Kwork HTTP session."""
        cookie_dict = cookies or self._session_hub_cookies
        if not cookie_dict:
            return False

        try:
            session = api.session
        except Exception as e:
            logger.debug(f"KworkService: failed to create session for cookies: {e}")
            return False

        try:
            if hasattr(session, "cookie_jar"):
                session.cookie_jar.update_cookies(cookie_dict, response_url=URL("https://kwork.ru/"))
                session.cookie_jar.update_cookies(cookie_dict, response_url=URL("https://api.kwork.ru/"))
                return True
            if hasattr(session, "cookies"):
                session.cookies.update(cookie_dict)
                return True
        except TypeError:
            try:
                session.cookie_jar.update_cookies(cookie_dict)
                return True
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"KworkService: failed to apply Session Hub cookies: {e}")
        return False

    async def _sync_session_hub_cookies(self, api: Any) -> None:
        """Refresh cookies for cached clients before cookie-sensitive operations."""
        cookie_dict = await self._fetch_session_hub_cookies()
        if cookie_dict:
            self._apply_cookies_to_api(api, cookie_dict)

    @staticmethod
    def _extract_chat_list(html: str) -> list[dict[str, Any]]:
        marker = "window.chatList="
        idx = html.find(marker)
        if idx < 0:
            return []
        raw = html[idx + len(marker) :].lstrip()
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
        except Exception as e:
            logger.debug(f"KworkService: failed to parse web chat list: {e}")
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _normalize_web_dialog(item: dict[str, Any]) -> dict[str, Any]:
        from datetime import datetime

        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        last_message = item.get("lastMessage") if isinstance(item.get("lastMessage"), dict) else {}
        username = str(item.get("username") or author.get("username") or "")
        user_id = item.get("user_id") or author.get("USERID") or author.get("user_id") or username
        message_id = (
            last_message.get("MID")
            or item.get("MID")
            or last_message.get("inbox_message_id")
            or item.get("inbox_message_id")
            or item.get("message_id")
            or user_id
        )
        timestamp = last_message.get("time") or item.get("time") or item.get("created_at") or item.get("updated_at")
        if isinstance(timestamp, (int, float)) or (isinstance(timestamp, str) and timestamp.isdigit()):
            last_message_at = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_message_at = str(timestamp or "")
        sender_id = last_message.get("MSGFROM") or item.get("MSGFROM")
        actor_id = item.get("member_id")
        sender = "freelancer" if actor_id and sender_id and str(sender_id) == str(actor_id) else "customer"

        return {
            "id": message_id,
            "dialog_id": item.get("dialog_id") or item.get("MID") or user_id,
            "user_id": user_id,
            "username": username,
            "project_id": item.get("project_id") or item.get("want_id") or item.get("order_id") or user_id,
            "project_name": item.get("project_name") or username or "Kwork dialog",
            "unread": item.get("unread") or item.get("unread_count") or 0,
            "unread_count": item.get("unread_count") or item.get("unread") or 0,
            "last_message": str(last_message.get("message") or item.get("message") or item.get("last_message") or ""),
            "last_message_id": message_id,
            "last_message_at": last_message_at,
            "updated_at": last_message_at,
            "sender": sender,
        }

    async def get_web_dialogs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read Kwork inbox dialogs from the authenticated web page using Session Hub cookies."""
        import httpx

        cookie_dict = await self._fetch_session_hub_cookies()
        if not cookie_dict:
            return []

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                    ),
                    "Referer": "https://kwork.ru/inbox",
                },
                timeout=6.0,
                proxy=kwork_http_proxy_url(rotate=False),
                trust_env=False,
            ) as client:
                resp = await client.get(f"{KWORK_BASE_URL}/inbox", cookies=cookie_dict)
            if resp.status_code != 200:
                logger.debug(f"KworkService: web inbox returned HTTP {resp.status_code}")
                return []
            chats = self._extract_chat_list(resp.text)
            return [self._normalize_web_dialog(item) for item in chats[:limit] if isinstance(item, dict)]
        except Exception as e:
            logger.debug(f"KworkService: failed to read web dialogs: {e}")
            return []

    async def send_web_message(self, recipient: str | int, text: str) -> dict[str, Any] | None:
        """Send a Kwork inbox message through the authenticated web form."""
        import httpx

        message = text.strip()
        if not message:
            return None

        cookie_dict = await self._fetch_session_hub_cookies()
        if not cookie_dict:
            return None

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                    ),
                    "Origin": KWORK_BASE_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30.0,
                proxy=kwork_http_proxy_url(rotate=False),
                trust_env=False,
            ) as client:
                inbox = await client.get(f"{KWORK_BASE_URL}/inbox", cookies=cookie_dict)
                chats = self._extract_chat_list(inbox.text)
                recipient_key = str(recipient).lower()
                chat = next(
                    (
                        item
                        for item in chats
                        if str(item.get("user_id") or item.get("USERID") or "").lower() == recipient_key
                        or str(item.get("username") or "").lower() == recipient_key
                    ),
                    None,
                )
                if not chat:
                    logger.debug(f"KworkService: web recipient not found in chat list: {recipient}")
                    return None

                username = str(chat.get("username") or "")
                user_id = str(chat.get("user_id") or chat.get("USERID") or recipient)
                page_url = f"{KWORK_BASE_URL}/inbox/{username}" if username else f"{KWORK_BASE_URL}/inbox"
                page = await client.get(page_url, cookies=cookie_dict)
                csrf_match = re.search(r'name="csrftoken"\s+value="([^"]+)"', page.text)
                user_match = re.search(r"window\.conversationUserId\s*=\s*(\d+)", page.text)
                csrf = csrf_match.group(1) if csrf_match else ""
                msgto = user_match.group(1) if user_match else user_id
                if not csrf or not msgto:
                    logger.debug("KworkService: web send form is missing csrf or recipient")
                    return None

                files = {
                    "csrftoken": (None, csrf),
                    "submg": (None, "1"),
                    "msgto": (None, msgto),
                    "message_body": (None, message),
                    "message_type": (None, ""),
                    "allowDialog": (None, "1"),
                    "quoteId": (None, ""),
                }
                resp = await client.post(
                    f"{KWORK_BASE_URL}/sendmessage",
                    cookies=cookie_dict,
                    headers={"Referer": page_url},
                    files=files,
                )
                if resp.status_code != 200:
                    logger.debug(f"KworkService: web send returned HTTP {resp.status_code}")
                    return None
                data = resp.json()
                if isinstance(data, dict) and data.get("MID"):
                    logger.info(f"KworkService: web message sent to {username or user_id}")
                    return data
                logger.debug(f"KworkService: unexpected web send response: {data}")
                return None
        except Exception as e:
            logger.debug(f"KworkService: failed to send web message: {e}")
            return None

    async def mark_web_dialog_read(self, recipient: str | int) -> dict[str, Any]:
        """Best-effort mark a Kwork web dialog as read through the live web session."""
        import httpx

        result: dict[str, Any] = {"ok": False, "web_opened": False, "api_read": False, "api_tracks_read": False}
        cookie_dict = await self._fetch_session_hub_cookies()
        chat: dict[str, Any] | None = None
        recipient_key = str(recipient).lower()
        if not cookie_dict:
            result["reason"] = "session_hub_cookies_missing"

        if cookie_dict:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                        ),
                        "Referer": "https://kwork.ru/inbox",
                    },
                    timeout=6.0,
                    proxy=kwork_http_proxy_url(rotate=False),
                    trust_env=False,
                ) as client:
                    inbox = await client.get(f"{KWORK_BASE_URL}/inbox", cookies=cookie_dict)
                    chats = self._extract_chat_list(inbox.text)
                    chat = next(
                        (
                            item
                            for item in chats
                            if str(item.get("user_id") or item.get("USERID") or "").lower() == recipient_key
                            or str(item.get("username") or "").lower() == recipient_key
                        ),
                        None,
                    )
                    username = str((chat or {}).get("username") or "")
                    if not username and not str(recipient).isdigit():
                        username = str(recipient)
                    page_url = f"{KWORK_BASE_URL}/inbox/{username}" if username else f"{KWORK_BASE_URL}/inbox"
                    if chat or username:
                        page = await client.get(page_url, cookies=cookie_dict)
                        result["web_opened"] = page.status_code == 200
                        if username:
                            result["username"] = username
            except Exception as e:
                logger.debug(f"KworkService: failed to open web dialog for read mark: {e}")

        # Opening /inbox/{username} is the browser-equivalent read action.
        # The legacy API fallbacks require an undocumented payload shape and
        # currently respond with "not enough parameters". Do not turn a
        # successful web read into two failing API calls.
        if not result["web_opened"]:
            result["reason"] = result.get("reason") or "web_dialog_unavailable"
        result["ok"] = bool(result["web_opened"])
        return result

    async def _try_email_password(self) -> Any | None:
        """Attempt direct auth via email/password with retry on 401."""
        import asyncio

        email = os.getenv("KWORK_EMAIL")
        password = os.getenv("KWORK_PASSWORD")
        if not email or not password:
            logger.warning("KworkService: нет KWORK_EMAIL/KWORK_PASSWORD, авторизация невозможна")
            return None

        # First attempt
        api = await self._create_api_client(email, password)
        if api is not None:
            return api

        # Retry once with 5s delay after client initialization failed.
        logger.info("KworkService: повторная попытка создать API-клиент через 5с...")
        await asyncio.sleep(5)

        api = await self._create_api_client(email, password)
        if api is not None:
            logger.info("KworkService: API-клиент успешно создан со второй попытки")
            return api

        # Both attempts failed (Requirement 7.6)
        logger.error("KworkService: не удалось создать рабочий API-клиент после повторной попытки")
        return None

    async def _create_api_client(self, email: str, password: str, *, proxy: str | None = None) -> Any | None:
        """Create and authenticate a Kwork API client. Returns None on failure."""
        try:
            from src.platforms.kwork_ext import get_proxy_rotator

            proxy = proxy or get_proxy_rotator().next(rotate=False)
        except Exception as e:
            logger.warning(
                f"KworkService: не удалось выбрать healthy VPNTE маршрут до email/password "
                f"({type(e).__name__}: {e})"
            )
            return None

        try:
            from kwork import Kwork

            api = Kwork(
                login=email,
                password=password,
                timeout=self.timeout,
                retry_max_attempts=max(1, self.retry_max_attempts),
                proxy=proxy,
                relogin_on_auth_error=True,
            )
            api._psr_proxy_url = proxy
            api._psr_auth_mode = "email+password"
            api._psr_recover_transport = self._recover_transport
            logger.info(
                f"KworkService: API-клиент инициализирован через email/password (proxy={'yes' if proxy else 'no'})"
            )
            return api
        except Exception as e:
            logger.warning(f"KworkService: ошибка создания email/password API-клиента: {type(e).__name__}: {e}")
            return None

    async def _recover_transport(self, failed_api: Any, error: BaseException | None = None) -> Any | None:
        """Rebuild a cached Kwork client after its local VPNTE proxy dies."""

        async with self._transport_recovery_lock:
            if self._api is not failed_api and self._token_api is not failed_api:
                return self._api or self._token_api

            try:
                from src.platforms.kwork_ext import get_proxy_rotator

                proxy = get_proxy_rotator().next(rotate=False)
            except Exception as exc:
                logger.warning(f"KworkService: VPNTE proxy recovery failed to discover live proxy: {exc}")
                return None
            if not proxy:
                logger.warning("KworkService: VPNTE proxy recovery found no live proxy")
                return None

            auth_mode = str(getattr(failed_api, "_psr_auth_mode", "") or "")
            replacement: Any | None = None
            try:
                from kwork import Kwork

                if auth_mode in {"cookie-only", "email+cookies"}:
                    cookie_dict = await self._fetch_session_hub_cookies()
                    if not cookie_dict:
                        cookie_dict = dict(self._session_hub_cookies)
                    if not cookie_dict:
                        logger.warning("KworkService: VPNTE recovery cannot rebuild cookie client without Session Hub cookies")
                        return None
                    email = os.getenv("KWORK_EMAIL", "")
                    password = os.getenv("KWORK_PASSWORD", "")
                    phone = os.getenv("KWORK_PHONE", "")
                    replacement = Kwork(
                        login=email or "",
                        password=password or "",
                        phone_last=phone if phone else None,
                        timeout=self.timeout,
                        retry_max_attempts=max(1, self.retry_max_attempts),
                        proxy=proxy,
                    )
                    replacement._psr_proxy_url = proxy
                    self._apply_cookies_to_api(replacement, cookie_dict)
                    replacement._psr_auth_mode = auth_mode
                elif auth_mode == "email+password" or (os.getenv("KWORK_EMAIL") and os.getenv("KWORK_PASSWORD")):
                    replacement = Kwork(
                        login=os.getenv("KWORK_EMAIL", ""),
                        password=os.getenv("KWORK_PASSWORD", ""),
                        timeout=self.timeout,
                        retry_max_attempts=max(1, self.retry_max_attempts),
                        proxy=proxy,
                        relogin_on_auth_error=True,
                    )
                    replacement._psr_proxy_url = proxy
                    replacement._psr_auth_mode = "email+password"
                else:
                    logger.warning(f"KworkService: unsupported auth mode for VPNTE recovery: {auth_mode or 'unknown'}")
                    return None

                replacement._psr_recover_transport = self._recover_transport
                if self._api is failed_api:
                    self._api = replacement
                if self._token_api is failed_api:
                    self._token_api = replacement
                try:
                    await failed_api.close()
                except Exception:
                    pass
                logger.info(f"KworkService: cached API client rebuilt through live VPNTE proxy {proxy}")
                return replacement
            except Exception as exc:
                if replacement is not None:
                    try:
                        await replacement.close()
                    except Exception:
                        pass
                logger.warning(f"KworkService: VPNTE transport recovery failed: {exc}")
                return None

    async def get_token_api(self) -> Any | None:
        """Return an API client suitable for token-required mobile endpoints."""
        if self._token_api is not None:
            if await self._cached_api_proxy_is_live(self._token_api):
                return self._token_api
            await self._invalidate_cached_api(self._token_api)
        if self._api is not None:
            if not await self._cached_api_proxy_is_live(self._api):
                await self._invalidate_cached_api(self._api)
            elif getattr(self._api, "_psr_auth_mode", "") != "cookie-only":
                return self._api
            elif not (os.getenv("KWORK_EMAIL") and os.getenv("KWORK_PASSWORD")):
                return self._api

        api = await self._try_email_password()
        if api is not None:
            self._token_api = api
            return self._token_api

        return await self.get_api()

    @staticmethod
    def _generate_username(email: str) -> str:
        """Generate a readable, random Kwork login without special characters."""

        del email
        candidate = (
            f"{secrets.choice(_KWORK_USERNAME_PREFIXES)}"
            f"{secrets.choice(_KWORK_USERNAME_WORDS)}"
            f"{secrets.randbelow(9000) + 1000}"
        )
        if not re.fullmatch(r"[a-z0-9]{4,20}", candidate):
            raise KworkRegistrationError("invalid_username", "Could not generate a valid Kwork login")
        return candidate

    @staticmethod
    def _registration_email_error(email: str) -> str | None:
        normalized = email.strip()
        if not normalized:
            return "Email is required"
        if len(normalized) > 90 or not _KWORK_EMAIL_RE.fullmatch(normalized):
            return "Email has an invalid format or is too long"
        domain = normalized.rsplit("@", 1)[-1].lower()
        blocked = {
            item.strip().lower()
            for item in os.getenv("KWORK_BLOCKED_EMAIL_DOMAINS", "").split(",")
            if item.strip()
        }
        if domain in (_KWORK_DEFAULT_BLOCKED_DOMAINS | blocked):
            return f"Email domain is not accepted by Kwork: {domain}"
        return None

    @staticmethod
    def _registration_mail_provider(value: str) -> str:
        provider = str(value or "catchmail").strip().lower()
        if provider not in {"catchmail", "firstmail"}:
            raise KworkRegistrationError("invalid_mail_provider", "mail_provider must be catchmail or firstmail")
        return provider

    @staticmethod
    def _normalize_registration_proxy(value: str) -> str | None:
        """Validate a VPNTE proxy URL and return an HTTPX-compatible URL."""

        raw = str(value or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            parts = raw.split(":", 3)
            if len(parts) == 4:
                host, port, username, password = parts
                raw = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
            else:
                raw = f"http://{raw}"
        parsed = urlparse(raw)
        try:
            port = parsed.port
        except ValueError:
            port = None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not port:
            raise KworkRegistrationError("invalid_proxy", "Proxy must be host:port, host:port:login:password, or an HTTP(S) URL")
        return raw

    @staticmethod
    def _registration_pacer(proxy: str | None):
        try:
            min_delay = float(os.getenv("KWORK_REGISTRATION_PACE_MIN", "0"))
        except (TypeError, ValueError):
            min_delay = 0.0
        try:
            max_delay = float(os.getenv("KWORK_REGISTRATION_PACE_MAX", "0"))
        except (TypeError, ValueError):
            max_delay = min_delay
        try:
            burst_limit = int(os.getenv("KWORK_REGISTRATION_BURST_LIMIT", "0"))
        except (TypeError, ValueError):
            burst_limit = 0
        return get_pacer(
            scope=f"registration:{proxy or 'direct'}",
            min_delay=max(0.0, min_delay),
            max_delay=max(0.0, max_delay),
            burst_limit=max(0, burst_limit),
        )

    def _vpnte_registration_proxies(self, slots: list[int]) -> list[tuple[int, str]]:
        """Resolve active VPNTE slots from the local Control API into proxy URLs."""

        from src.utils.vpnte_proxy import VpnteProxyClient

        requested_slots: list[int] = []
        for raw_slot in slots:
            if isinstance(raw_slot, bool):
                raise KworkRegistrationError("vpnte_slot_unavailable", "VPNTE slot must be an integer greater than zero")
            try:
                slot = int(raw_slot)
            except (TypeError, ValueError) as exc:
                raise KworkRegistrationError("vpnte_slot_unavailable", "VPNTE slot must be an integer greater than zero") from exc
            if slot < 1:
                raise KworkRegistrationError("vpnte_slot_unavailable", "VPNTE slot must be an integer greater than zero")
            if slot not in requested_slots:
                requested_slots.append(slot)
        try:
            instances = VpnteProxyClient().instances()
        except Exception as exc:
            raise KworkRegistrationError("vpnte_proxy_unavailable", f"VPNTE Control API is unavailable: {exc}") from exc

        active: dict[int, str] = {}
        for instance in instances:
            try:
                slot = int(instance.get("slot"))
            except (TypeError, ValueError):
                continue
            proxy_url = str(instance.get("proxyUrl") or "").strip()
            if instance.get("running") is True and proxy_url:
                normalized = self._normalize_registration_proxy(proxy_url)
                if normalized:
                    active[slot] = normalized

        selected_slots = requested_slots or sorted(active)
        missing = [slot for slot in selected_slots if slot not in active]
        if missing:
            formatted = ", ".join(str(slot) for slot in missing)
            raise KworkRegistrationError("vpnte_slot_unavailable", f"VPNTE slots are not running or have no proxyUrl: {formatted}")
        if not selected_slots:
            raise KworkRegistrationError("vpnte_proxy_unavailable", "VPNTE Control API returned no active proxy instances")
        return [(slot, active[slot]) for slot in selected_slots]

    @staticmethod
    def _registration_route_timeout() -> float:
        raw = os.getenv("KWORK_REGISTRATION_ROUTE_TIMEOUT", os.getenv("VPNTE_PROXY_TIMEOUT", "10"))
        try:
            return min(30.0, max(2.0, float(raw)))
        except (TypeError, ValueError):
            return 10.0

    @staticmethod
    def _registration_route_concurrency() -> int:
        try:
            return min(32, max(1, int(os.getenv("KWORK_REGISTRATION_ROUTE_CONCURRENCY", "12"))))
        except (TypeError, ValueError):
            return 12

    def _registration_used_ips(self) -> set[str]:
        used: set[str] = set()
        try:
            records = self._registration_store().list()
        except KworkAccountStoreError as exc:
            logger.warning("Kwork registration: cannot read stored account IPs: {}", exc)
            return used
        for record in records:
            for candidate in (record.signup_ip, record.activation_ip):
                if not candidate:
                    continue
                try:
                    used.add(str(ipaddress.ip_address(candidate)))
                except ValueError:
                    continue
        return used

    async def _probe_registration_route(self, slot: int, proxy: str) -> _RegistrationRouteProbe:
        import httpx

        started = time.monotonic()
        timeout = self._registration_route_timeout()
        try:
            async with httpx.AsyncClient(
                base_url=KWORK_BASE_URL,
                follow_redirects=True,
                headers={"User-Agent": KWORK_REGISTRATION_USER_AGENT},
                proxy=proxy,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
            ) as client:
                await self._check_registration_gate(client)
                egress_ip: str | None = None
                last_ip_error = "egress_ip_unavailable"
                for url, json_response in (
                    ("https://api.ipify.org?format=json", True),
                    ("https://checkip.amazonaws.com", False),
                ):
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        if json_response:
                            payload = response.json()
                            candidate = str(payload.get("ip") or "") if isinstance(payload, Mapping) else ""
                        else:
                            candidate = str(response.text or "").strip()
                        egress_ip = str(ipaddress.ip_address(candidate))
                        break
                    except Exception as exc:
                        last_ip_error = type(exc).__name__
                if egress_ip is None:
                    raise KworkRegistrationError(last_ip_error, "Could not determine route egress IP")
            return _RegistrationRouteProbe(
                slot=slot,
                proxy=proxy,
                egress_ip=egress_ip,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except KworkRegistrationError as exc:
            logger.debug("Kwork registration route preflight: slot {} failed with {}", slot, exc.code)
            return _RegistrationRouteProbe(
                slot=slot,
                proxy=proxy,
                egress_ip=None,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                error=exc.code,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Kwork registration route preflight: slot {} failed with {}: {}",
                slot,
                type(exc).__name__,
                exc,
            )
            return _RegistrationRouteProbe(
                slot=slot,
                proxy=proxy,
                egress_ip=None,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                error=type(exc).__name__,
            )

    async def _preflight_registration_routes(
        self,
        routes: list[tuple[int, str]],
    ) -> list[_RegistrationRouteProbe]:
        concurrency = min(self._registration_route_concurrency(), len(routes))
        timeout = self._registration_route_timeout()
        semaphore = asyncio.Semaphore(concurrency)
        completed_count = 0
        healthy_count = 0
        healthy_ips: set[str] = set()
        progress_step = 10 if len(routes) > 10 else len(routes)
        logger.info(
            "Kwork registration route preflight started: {} route(s), concurrency {}, timeout {:.1f}s",
            len(routes),
            concurrency,
            timeout,
        )

        async def probe(slot: int, proxy: str) -> _RegistrationRouteProbe:
            nonlocal completed_count, healthy_count
            async with semaphore:
                result = await self._probe_registration_route(slot, proxy)
            completed_count += 1
            if result.healthy:
                healthy_count += 1
                healthy_ips.add(str(result.egress_ip))
            if completed_count == len(routes) or completed_count % progress_step == 0:
                logger.info(
                    "Kwork registration route preflight: {}/{} checked, {} reachable, {} unique IP(s)",
                    completed_count,
                    len(routes),
                    healthy_count,
                    len(healthy_ips),
                )
            return result

        return list(await asyncio.gather(*(probe(slot, proxy) for slot, proxy in routes)))

    @staticmethod
    def _json_response(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {"success": False, "error": str(getattr(response, "text", ""))[:500]}
        return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def _payload_success(payload: Mapping[str, Any]) -> bool:
        """Accept the boolean spellings/envelopes used by Kwork responses."""

        for candidate in (payload.get("success"), payload.get("ok")):
            if candidate is True or str(candidate).strip().lower() in {"1", "true", "yes"}:
                return True
        nested = payload.get("data")
        return isinstance(nested, Mapping) and KworkService._payload_success(nested)

    @staticmethod
    def _signup_requires_captcha(payload: Mapping[str, Any]) -> bool:
        """Treat a CAPTCHA flag as actionable only when form validation did not fail first."""

        if payload.get("captcha_required") is True:
            return True
        if not payload.get("recaptcha_show"):
            return False
        return not bool(payload.get("error") or payload.get("errors"))

    @staticmethod
    def _signup_error_message(payload: Mapping[str, Any]) -> str:
        raw = payload.get("error") or payload.get("errors")
        if isinstance(raw, Mapping):
            parts = [str(value).strip() for value in raw.values() if str(value).strip()]
            return "; ".join(parts) or "Kwork signup failed"
        if isinstance(raw, list):
            parts = [str(value).strip() for value in raw if str(value).strip()]
            return "; ".join(parts) or "Kwork signup failed"
        return str(raw).strip() or "Kwork signup failed"

    async def _check_registration_gate(self, client: Any) -> dict[str, Any]:
        response = await client.get("/api/ban/disallowfreeregister")
        status_code = int(getattr(response, "status_code", 200) or 200)
        if not 200 <= status_code < 300:
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            raise RuntimeError(f"Kwork registration gate returned HTTP {status_code}")
        payload = self._json_response(response)
        raw: Any = payload
        if isinstance(payload, dict):
            for key in ("disallow", "disallowfreeregister", "data", "value"):
                if key in payload:
                    raw = payload[key]
                    break
        if raw is True or str(raw).strip().lower() in {"true", "1", "yes", "on"}:
            raise KworkRegistrationError("registration_disabled", "Kwork currently disallows free registration", payload=payload)
        return payload

    async def _check_email(self, client: Any, email: str) -> dict[str, Any]:
        response = await client.get("/api/user/checkemail", params={"email": email})
        payload = self._json_response(response)
        if bool(payload.get("in_stop_list")):
            raise KworkRegistrationError("email_stop_list", "Email belongs to Kwork stop-list", payload=payload)
        if self._payload_success(payload):
            raise KworkRegistrationError("email_exists", "Email is already registered on Kwork", payload=payload)
        if payload.get("error"):
            raise KworkRegistrationError("email_rejected", str(payload["error"]), payload=payload)
        return payload

    async def _check_login(self, client: Any, login: str, *, force_generate: bool = False) -> tuple[str, dict[str, Any]]:
        candidate = login
        for attempt in range(3):
            form: dict[str, str] = {"login": candidate, "getFreeLogin": "true", "jsub": "1"}
            if force_generate:
                form["forceGenerate"] = "true"
            response = await client.post("/api/user/checklogin", data=form)
            payload = self._json_response(response)
            if self._payload_success(payload):
                resolved = str(payload.get("login") or candidate).strip()
                if 4 <= len(resolved) <= 20 and re.fullmatch(r"[a-z0-9]+", resolved):
                    if "kwork" not in resolved and "support" not in resolved:
                        return resolved, payload
            suggestion = str(payload.get("login") or payload.get("username") or "").strip().lower()
            if suggestion and suggestion != candidate:
                candidate = re.sub(r"[^a-z0-9]+", "", suggestion)[:20]
                continue
            candidate = f"{candidate[:17]}{attempt + 1}"[:20]
        raise KworkRegistrationError("login_unavailable", "Could not obtain a free Kwork login")

    @staticmethod
    def _build_signup_form(
        *,
        email: str,
        username: str,
        password: str,
        user_type: int,
        promo: str = "",
        track_client_id: str = "",
        action_after: str = "",
        timezone: str | None = None,
        is_subscribed: bool = False,
        captcha_token: str = "",
        captcha_field: str = "smart-token",
    ) -> dict[str, str]:
        form = {
            "track_client_id": track_client_id or str(uuid.uuid4()),
            "userType": str(user_type),
            "user_email": email,
            "user_username": username,
            "user_password": password,
            "user_promo": promo,
            "jsub": "1",
            "tz": timezone or os.getenv("TIMEZONE_REGION", "Europe/Moscow"),
            "signup_mode": "email",
        }
        if action_after:
            form["action_after"] = action_after
        if is_subscribed:
            form["is_subscribed"] = "1"
        if captcha_token.strip():
            form[captcha_field] = captcha_token.strip()
        return form

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or "")
        return "*" * max(8, len(value)) if value else ""

    @staticmethod
    def _generate_registration_password(*, forbidden: str = "") -> str:
        """Generate an ASCII password that cannot equal a Kwork login."""

        special_characters = "!@#$%"
        alphabet = string.ascii_letters + string.digits + special_characters
        for _ in range(3):
            password = "".join(
                (
                    secrets.choice(string.ascii_uppercase),
                    secrets.choice(string.ascii_lowercase),
                    secrets.choice(string.digits),
                    secrets.choice(special_characters),
                    "".join(secrets.choice(alphabet) for _ in range(16)),
                )
            )
            if password.casefold() != forbidden.strip().casefold():
                return password
        raise KworkRegistrationError("password_generation_failed", "Could not generate a unique Kwork password")

    @staticmethod
    def _serialize_registration_cookies(client: Any) -> list[dict[str, Any]]:
        cookies = getattr(client, "cookies", None)
        jar = getattr(cookies, "jar", None)
        if jar is None:
            return []

        serialized: list[dict[str, Any]] = []
        for cookie in jar:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            if not name or not value:
                continue
            rest = getattr(cookie, "_rest", {}) or {}
            serialized.append(
                {
                    "version": int(getattr(cookie, "version", 0) or 0),
                    "name": name,
                    "value": value,
                    "port": getattr(cookie, "port", None),
                    "port_specified": bool(getattr(cookie, "port_specified", False)),
                    "domain": str(getattr(cookie, "domain", "") or "kwork.ru"),
                    "domain_specified": bool(getattr(cookie, "domain_specified", True)),
                    "domain_initial_dot": bool(getattr(cookie, "domain_initial_dot", False)),
                    "path": str(getattr(cookie, "path", "") or "/"),
                    "path_specified": bool(getattr(cookie, "path_specified", True)),
                    "expires": getattr(cookie, "expires", None),
                    "secure": bool(getattr(cookie, "secure", False)),
                    "discard": bool(getattr(cookie, "discard", False)),
                    "comment": getattr(cookie, "comment", None),
                    "comment_url": getattr(cookie, "comment_url", None),
                    "rest": {str(key): None if value is None else str(value) for key, value in dict(rest).items()},
                    "rfc2109": bool(getattr(cookie, "rfc2109", False)),
                }
            )
        return serialized

    @staticmethod
    def _restore_registration_cookies(session: Mapping[str, Any] | None) -> Any:
        import httpx

        cookies = httpx.Cookies()
        records = session.get("cookies", []) if isinstance(session, Mapping) else []
        if not isinstance(records, list):
            return cookies
        for record in records:
            if not isinstance(record, Mapping):
                continue
            name = str(record.get("name") or "").strip()
            value = str(record.get("value") or "")
            if not name or not value:
                continue
            domain = str(record.get("domain") or "kwork.ru").strip() or "kwork.ru"
            path = str(record.get("path") or "/").strip() or "/"
            expires = record.get("expires")
            try:
                expires = int(expires) if expires not in (None, "") else None
            except (TypeError, ValueError):
                expires = None
            raw_rest = record.get("rest")
            rest = dict(raw_rest) if isinstance(raw_rest, Mapping) else {}
            try:
                cookies.jar.set_cookie(
                    Cookie(
                        version=int(record.get("version") or 0),
                        name=name,
                        value=value,
                        port=str(record.get("port")) if record.get("port") else None,
                        port_specified=bool(record.get("port_specified")),
                        domain=domain,
                        domain_specified=bool(record.get("domain_specified", True)),
                        domain_initial_dot=bool(record.get("domain_initial_dot", domain.startswith("."))),
                        path=path,
                        path_specified=bool(record.get("path_specified", True)),
                        secure=bool(record.get("secure")),
                        expires=expires,
                        discard=bool(record.get("discard", expires is None)),
                        comment=str(record.get("comment")) if record.get("comment") else None,
                        comment_url=str(record.get("comment_url")) if record.get("comment_url") else None,
                        rest=rest,
                        rfc2109=bool(record.get("rfc2109")),
                    )
                )
            except (TypeError, ValueError):
                continue
        return cookies

    @classmethod
    def _registration_session_snapshot(
        cls,
        client: Any,
        *,
        proxy: str | None,
        auth_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "cookies": cls._serialize_registration_cookies(client),
            "proxy_url": str(proxy or ""),
            "user_agent": KWORK_REGISTRATION_USER_AGENT,
            "auth_data": dict(auth_data or {}),
        }

    def _registration_http_client(
        self,
        *,
        proxy: str | None,
        session: Mapping[str, Any] | None = None,
    ) -> Any:
        import httpx

        stored_user_agent = str((session or {}).get("user_agent") or KWORK_REGISTRATION_USER_AGENT)
        return httpx.AsyncClient(
            base_url=KWORK_BASE_URL,
            follow_redirects=True,
            headers={
                "User-Agent": stored_user_agent,
                "Referer": f"{KWORK_BASE_URL}/signup",
            },
            cookies=self._restore_registration_cookies(session),
            timeout=self.timeout,
            proxy=proxy or None,
            trust_env=False,
        )

    @staticmethod
    async def _capture_registration_ip(client: Any) -> str | None:
        """Read the public egress IP using the exact HTTP transport of the signup session."""

        try:
            response = await client.get("https://api.ipify.org", params={"format": "json"})
            if not (200 <= int(response.status_code) < 300):
                return None
            payload = response.json()
            candidate = str(payload.get("ip") or "") if isinstance(payload, Mapping) else ""
            return str(ipaddress.ip_address(candidate)) if candidate else None
        except Exception as exc:
            logger.debug("Kwork registration: cannot capture egress IP ({})", type(exc).__name__)
            return None

    def _save_registration_state(
        self,
        result: dict[str, Any],
        *,
        password: str,
        client: Any,
        proxy: str | None,
        auth_data: Mapping[str, Any] | None = None,
        signup_ip: str | None = None,
        activation_ip: str | None = None,
        activated_at: str | None = None,
        last_error: str | None = None,
    ) -> StoredKworkAccount:
        try:
            record = self._registration_store().save(
                registration_id=str(result["registration_id"]),
                email=str(result["email"]),
                username=str(result["username"]),
                user_type=int(result["user_type"]),
                mail_provider=str(result["mail_provider"]),
                status=str(result["status"]),
                registration_started_at=str(result["registration_started_at"]),
                password=password,
                session=self._registration_session_snapshot(client, proxy=proxy, auth_data=auth_data),
                signup_ip=signup_ip,
                activation_ip=activation_ip,
                registration_slot=(
                    int(result["registration_slot"])
                    if isinstance(result.get("registration_slot"), int) and result["registration_slot"] > 0
                    else None
                ),
                registration_proxy_url=proxy,
                activated_at=activated_at,
                last_error=last_error,
            )
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not save Kwork account session") from exc
        result.update(record.public_data())
        return record

    async def _fetch_verification_link(
        self,
        email: str,
        mail_password: str,
        api_key: str | None = None,
        *,
        mail_provider: str = "catchmail",
        after: datetime | None = None,
        initial_delay: float = 0.0,
        proxy: str | None = None,
    ) -> str | None:
        timeout = float(os.getenv("KWORK_REGISTRATION_MAIL_TIMEOUT", "120"))
        poll_interval = float(os.getenv("KWORK_REGISTRATION_MAIL_POLL_INTERVAL", "1.1"))
        if mail_provider == "catchmail":
            from src.utils.catchmail import CatchmailClient

            delay = min(10.0, max(0.0, float(initial_delay)))
            client = CatchmailClient(
                email=email,
                base_url=os.getenv("CATCHMAIL_API_BASE_URL", "https://api.catchmail.io/api/v1"),
                proxy=proxy or "",
            )
            return await client.wait_for_kwork_link(
                timeout=timeout,
                poll_interval=poll_interval,
                after=after,
                initial_delay=delay,
            )

        from src.utils.firstmail import FirstmailClient

        client = FirstmailClient(
            email=email,
            password=mail_password,
            api_key=api_key or os.getenv("FIRSTMAIL_API_KEY"),
            proxy=proxy or kwork_http_proxy_url(rotate=False),
        )
        return await client.wait_for_kwork_link(
            timeout=timeout,
            poll_interval=poll_interval,
            after=after,
        )

    async def _activate_account(self, link: str, client: Any) -> dict[str, Any]:
        parsed = URL(link)
        if parsed.scheme != "https" or (parsed.host or "").lower() not in {"kwork.ru", "www.kwork.ru"}:
            raise KworkRegistrationError("unsafe_activation_link", "Activation link host is not allowlisted")
        if not any(marker in (parsed.path or "").lower() for marker in ("activ", "confirm", "verify", "email")):
            raise KworkRegistrationError("unsafe_activation_link", "Activation link path is not an activation endpoint")
        response = await client.get(link)
        final_url = str(getattr(response, "url", link))
        final_host = (URL(final_url).host or "").lower()
        body = str(getattr(response, "text", "") or "").lower()
        challenge = "/not_access.php" in final_url.lower() or "smart-captcha" in body or "data-sitekey" in body
        if final_host not in {"kwork.ru", "www.kwork.ru"}:
            return {
                "ok": False,
                "status_code": response.status_code,
                "final_url": final_url,
                "reason": "unsafe_redirect",
            }
        if challenge:
            return {
                "ok": False,
                "status_code": response.status_code,
                "final_url": final_url,
                "reason": "captcha_required",
            }
        body_markers = (
            "account activated",
            "activation successful",
            "email confirmed",
            "email подтвержден",
            "аккаунт активирован",
            "регистрация завершена",
            "добро пожаловать",
        )
        body_proof = any(marker in body for marker in body_markers)
        return {
            "ok": 200 <= response.status_code < 400 and body_proof,
            "http_ok": 200 <= response.status_code < 400,
            "status_code": response.status_code,
            "final_url": final_url,
            "body_proof": body_proof,
            "reason": None if body_proof else "activation_response_unconfirmed",
        }

    async def _verify_activated_account(
        self,
        email: str,
        password: str,
        *,
        proxy: str | None = None,
    ) -> dict[str, Any]:
        """Prove activation with a read-only authenticated actor request."""

        api = await self._create_api_client(email, password, proxy=proxy)
        if api is None:
            return {"ok": False, "reason": "post_activation_login_failed"}
        try:
            payload = await api.request("post", "actor", use_token=True)
            actor = payload.get("response") if isinstance(payload, dict) else None
            success = isinstance(payload, dict) and payload.get("success") is True and isinstance(actor, dict)
            if not success:
                return {"ok": False, "reason": "post_activation_actor_failed"}
            return {
                "ok": True,
                "auth_mode": "email+password",
                "username": actor.get("username", ""),
                "verified": actor.get("verified"),
                "actor_status": actor.get("status", ""),
            }
        except Exception as exc:
            return {"ok": False, "reason": f"post_activation_actor_failed: {type(exc).__name__}: {exc}"}
        finally:
            close = getattr(api, "close", None)
            if close:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass

    async def _verify_registration_session(self, client: Any) -> dict[str, Any]:
        """Check the persisted browser-like session without submitting a password."""

        try:
            response = await client.get("/inbox")
            final_url = str(getattr(response, "url", f"{KWORK_BASE_URL}/inbox"))
            body = str(getattr(response, "text", "") or "")
            status_code = int(getattr(response, "status_code", 0) or 0)
            redirected_to_login = "/login" in final_url.lower() or "/signin" in final_url.lower()
            has_session_marker = "window.chatList=" in body or "logout" in body.lower()
            return {
                "ok": 200 <= status_code < 400 and not redirected_to_login and has_session_marker,
                "auth_mode": "saved_http_session",
                "status_code": status_code,
                "final_url": final_url,
                "reason": (
                    "session_redirected_to_login"
                    if redirected_to_login
                    else None
                    if has_session_marker
                    else "session_response_unconfirmed"
                ),
            }
        except Exception as exc:
            return {"ok": False, "auth_mode": "saved_http_session", "reason": f"session_check_failed: {type(exc).__name__}"}

    async def register_account(
        self,
        email: str,
        mail_password: str,
        user_type: int = 1,
        promo: str = "",
        use_simple: bool = False,
        *,
        track_client_id: str = "",
        action_after: str = "",
        is_subscribed: bool = False,
        captcha_token: str = "",
        captcha_field: str = "smart-token",
        firstmail_api_key: str = "",
        mail_provider: str = "catchmail",
        dry_run: bool = False,
        proxy_url: str = "",
        registration_slot: int | None = None,
    ) -> dict[str, Any]:
        """Run the email-only Kwork signup flow with explicit intermediate status."""

        provider = self._registration_mail_provider(mail_provider)
        normalized_email = email.strip().lower()
        generated_mailbox = provider == "catchmail" and not normalized_email
        if generated_mailbox:
            from src.utils.catchmail import CatchmailClient, CatchmailError

            try:
                normalized_email = CatchmailClient.generate_address(
                    domain=os.getenv("CATCHMAIL_DOMAIN", "catchmail.io"),
                )
            except CatchmailError as exc:
                raise KworkRegistrationError("invalid_email", str(exc)) from exc
        validation_error = self._registration_email_error(normalized_email)
        if validation_error:
            raise KworkRegistrationError("invalid_email", validation_error)
        if provider == "firstmail" and not mail_password.strip():
            raise KworkRegistrationError("invalid_mail_credentials", "Firstmail requires mailbox credentials")
        if user_type not in {1, 2}:
            raise KworkRegistrationError("invalid_user_type", "user_type must be 1 or 2")
        if registration_slot is not None and (isinstance(registration_slot, bool) or registration_slot < 1):
            raise KworkRegistrationError("vpnte_slot_unavailable", "VPNTE slot must be an integer greater than zero")
        username = self._generate_username(normalized_email)
        mail_started_at = datetime.now(UTC)
        result: dict[str, Any] = {
            "ok": False,
            "status": "preflight",
            "registration_id": uuid.uuid4().hex,
            "email": normalized_email,
            "username": username,
            "mail_provider": provider,
            "registration_started_at": mail_started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "password_generated": True,
            "user_type": user_type,
            "activated": False,
            "captcha_required": False,
            "phone_fields_sent": False,
        }
        if registration_slot is not None:
            result["registration_slot"] = int(registration_slot)
        proxy = self._normalize_registration_proxy(proxy_url) if proxy_url.strip() else kwork_http_proxy_url(rotate=False)
        pacer = self._registration_pacer(proxy)

        def public_result() -> dict[str, Any]:
            for key in ("password", "password_masked", "csrftoken", "token", "activation_link"):
                result.pop(key, None)
            result["ok"] = result["status"] in {"activated", "preflight_ok"}
            return result

        async with self._registration_http_client(proxy=proxy) as client:
            await pacer.wait()
            await self._check_registration_gate(client)
            for email_attempt in range(3):
                await pacer.wait()
                try:
                    await self._check_email(client, normalized_email)
                    break
                except KworkRegistrationError as exc:
                    if not (generated_mailbox and exc.code == "email_exists" and email_attempt < 2):
                        raise
                    from src.utils.catchmail import CatchmailClient, CatchmailError

                    try:
                        normalized_email = CatchmailClient.generate_address(
                            domain=os.getenv("CATCHMAIL_DOMAIN", "catchmail.io"),
                        )
                    except CatchmailError as generation_exc:
                        raise KworkRegistrationError("invalid_email", str(generation_exc)) from generation_exc
                    result["email"] = normalized_email
                    result["username"] = self._generate_username(normalized_email)
            await pacer.wait()
            username, login_payload = await self._check_login(client, str(result["username"]))
            result["username"] = username
            result["login_check"] = login_payload
            kwork_password = self._generate_registration_password(forbidden=username)
            result["password_masked"] = self._mask_secret(kwork_password)
            form = self._build_signup_form(
                email=normalized_email,
                username=username,
                password=kwork_password,
                user_type=user_type,
                promo=promo,
                track_client_id=track_client_id,
                action_after=action_after,
                is_subscribed=is_subscribed,
                captcha_token=captcha_token,
                captcha_field=captcha_field,
            )
            result["signup_fields"] = sorted(key for key in form if "password" not in key)
            if dry_run:
                result["status"] = "preflight_ok"
                return public_result()
            endpoint = "/api/user/simplesignup" if use_simple else "/api/user/signup"
            signup_ip = await self._capture_registration_ip(client)
            await pacer.wait()
            response = await client.post(endpoint, files={key: (None, value) for key, value in form.items()})
            signup_payload = self._json_response(response)
            result["signup_http_status"] = response.status_code
            result["signup"] = {
                key: signup_payload.get(key)
                for key in ("success", "errors", "error", "recaptcha_show", "captcha_required", "action_after", "redirect")
                if key in signup_payload
            }
            if self._signup_requires_captcha(signup_payload):
                result.update(status="captcha_required", captcha_required=True, message="Kwork requires manual CAPTCHA")
                return public_result()
            if not use_simple and not self._payload_success(signup_payload) and (
                response.status_code in {404, 405} or self._signup_endpoint_missing(signup_payload)
            ):
                logger.info("Kwork registration: ordinary signup endpoint rejected, retrying simplesignup")
                await pacer.wait()
                response = await client.post(
                    "/api/user/simplesignup",
                    files={key: (None, value) for key, value in form.items()},
                )
                signup_payload = self._json_response(response)
                result["signup_fallback_http_status"] = response.status_code
                result["signup_fallback"] = {
                    key: signup_payload.get(key)
                    for key in ("success", "errors", "error", "recaptcha_show", "captcha_required", "action_after", "redirect")
                    if key in signup_payload
                }
                if self._signup_requires_captcha(signup_payload):
                    result.update(status="captcha_required", captcha_required=True, message="Kwork requires manual CAPTCHA")
                    return public_result()
            if not self._payload_success(signup_payload):
                result.update(status="signup_failed", message=self._signup_error_message(signup_payload))
                return public_result()
            result["status"] = "signup_submitted"
            auth_data = {
                key: signup_payload.get(key)
                for key in ("csrftoken", "token")
                if signup_payload.get(key) not in (None, "")
            }
            self._save_registration_state(
                result,
                password=kwork_password,
                client=client,
                proxy=proxy,
                auth_data=auth_data,
                signup_ip=signup_ip,
            )
            if provider == "catchmail" or mail_password:
                try:
                    link = await self._fetch_verification_link(
                        normalized_email,
                        mail_password,
                        firstmail_api_key or os.getenv("FIRSTMAIL_API_KEY"),
                        mail_provider=provider,
                        after=mail_started_at,
                        proxy=proxy,
                        initial_delay=max(0.0, float(os.getenv("KWORK_REGISTRATION_MAIL_INITIAL_DELAY", "0"))),
                    )
                except Exception as exc:
                    message = f"Signup succeeded; mailbox polling failed: {type(exc).__name__}: {exc}"
                    result.update(status="activation_pending", message=message)
                    self._save_registration_state(
                        result,
                        password=kwork_password,
                        client=client,
                        proxy=proxy,
                        auth_data=auth_data,
                        signup_ip=signup_ip,
                        last_error=message,
                    )
                    return public_result()
                if link:
                    await pacer.wait()
                    activation = await self._activate_account(link, client)
                    result.update(activation, activation_link_found=True)
                    activation_ip = await self._capture_registration_ip(client)
                    session_proof = await self._verify_registration_session(client) if activation.get("http_ok") else {
                        "ok": False,
                        "auth_mode": "saved_http_session",
                        "reason": "activation_http_failed",
                    }
                    result["session_proof"] = session_proof
                    if activation.get("http_ok"):
                        proof = await self._verify_activated_account(normalized_email, kwork_password, proxy=proxy)
                        result["post_activation"] = proof
                        result["activated"] = bool(session_proof.get("ok") or proof.get("ok"))
                    result["status"] = "activated" if result["activated"] else "activation_failed"
                    self._save_registration_state(
                        result,
                        password=kwork_password,
                        client=client,
                        proxy=proxy,
                        auth_data=auth_data,
                        signup_ip=signup_ip,
                        activation_ip=activation_ip,
                        activated_at=(
                            datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                            if result["activated"]
                            else None
                        ),
                        last_error=None if result["activated"] else str(result.get("reason") or "activation_failed"),
                    )
                else:
                    result.update(status="activation_pending", message="Signup succeeded; activation link was not found")
                    self._save_registration_state(
                        result,
                        password=kwork_password,
                        client=client,
                        proxy=proxy,
                        auth_data=auth_data,
                        signup_ip=signup_ip,
                        last_error="activation_link_not_found",
                    )
            else:
                result.update(status="signup_submitted", message="Signup succeeded; mailbox credentials were not supplied")
                self._save_registration_state(
                    result,
                    password=kwork_password,
                    client=client,
                    proxy=proxy,
                    auth_data=auth_data,
                    signup_ip=signup_ip,
                )
        return public_result()

    async def register_accounts_batch(
        self,
        *,
        account_count: int,
        vpnte_slots: list[int],
        email: str = "",
        mail_password: str = "",
        user_type: int = 1,
        promo: str = "",
        use_simple: bool = False,
        track_client_id: str = "",
        action_after: str = "",
        is_subscribed: bool = False,
        captcha_token: str = "",
        captcha_field: str = "smart-token",
        firstmail_api_key: str = "",
        mail_provider: str = "catchmail",
        avoid_used_ips: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create accounts through selected VPNTE slots, up to five distinct routes at once."""

        provider = self._registration_mail_provider(mail_provider)
        if account_count < 1:
            raise KworkRegistrationError("invalid_account_count", "account_count must be at least 1")
        if account_count > 1 and provider != "catchmail":
            raise KworkRegistrationError("batch_mail_provider_not_supported", "Batch registration requires CatchMail")
        if account_count > 1 and email.strip():
            raise KworkRegistrationError("batch_email_not_supported", "Batch registration requires an empty email field")

        vpnte_proxies = self._vpnte_registration_proxies(vpnte_slots)
        selected_slots = [slot for slot, _proxy in vpnte_proxies]
        if len(vpnte_proxies) < account_count:
            raise KworkRegistrationError(
                "vpnte_capacity_insufficient",
                (
                    f"Для {account_count} аккаунтов нужно не меньше {account_count} отдельных VPNTE-слотов; "
                    f"выбрано {len(vpnte_proxies)}. Регистрация не запущена."
                ),
                payload={
                    "requested_count": account_count,
                    "selected_route_count": len(vpnte_proxies),
                    "selected_vpnte_slots": selected_slots,
                    "unique_available_ip_count": 0,
                    "avoid_used_ips": avoid_used_ips,
                },
            )

        route_probes = await self._preflight_registration_routes(vpnte_proxies)
        used_ips = self._registration_used_ips() if avoid_used_ips else set()
        failed_probes: list[_RegistrationRouteProbe] = []
        duplicate_probes: list[_RegistrationRouteProbe] = []
        used_ip_probes: list[_RegistrationRouteProbe] = []
        available_probes: list[_RegistrationRouteProbe] = []
        available_ips: set[str] = set()
        for probe in route_probes:
            if not probe.healthy or not probe.egress_ip:
                failed_probes.append(probe)
            elif probe.egress_ip in used_ips:
                used_ip_probes.append(probe)
            elif probe.egress_ip in available_ips:
                duplicate_probes.append(probe)
            else:
                available_ips.add(probe.egress_ip)
                available_probes.append(probe)

        logger.info(
            (
                "Kwork registration route preflight completed: {} selected, {} reachable, "
                "{} unique available, {} failed, {} duplicate, {} already used"
            ),
            len(vpnte_proxies),
            sum(1 for probe in route_probes if probe.healthy),
            len(available_probes),
            len(failed_probes),
            len(duplicate_probes),
            len(used_ip_probes),
        )
        if len(available_probes) < account_count:
            payload = {
                "requested_count": account_count,
                "selected_route_count": len(vpnte_proxies),
                "reachable_route_count": sum(1 for probe in route_probes if probe.healthy),
                "unique_available_ip_count": len(available_probes),
                "failed_route_count": len(failed_probes),
                "duplicate_route_count": len(duplicate_probes),
                "used_ip_route_count": len(used_ip_probes),
                "avoid_used_ips": avoid_used_ips,
                "selected_vpnte_slots": selected_slots,
                "available_vpnte_slots": [probe.slot for probe in available_probes],
                "failed_vpnte_slots": [probe.slot for probe in failed_probes],
                "duplicate_vpnte_slots": [probe.slot for probe in duplicate_probes],
                "used_ip_vpnte_slots": [probe.slot for probe in used_ip_probes],
                "failed_route_errors": [
                    {"slot": probe.slot, "error": probe.error or "unavailable", "elapsed_ms": probe.elapsed_ms}
                    for probe in failed_probes
                ],
            }
            raise KworkRegistrationError(
                "vpnte_capacity_insufficient",
                (
                    f"Рабочих уникальных VPNTE IP недостаточно: найдено {len(available_probes)}, "
                    f"требуется {account_count}. Регистрация не запущена."
                ),
                payload=payload,
            )

        assigned_probes = available_probes[:account_count]
        reserve_probes = available_probes[account_count:]
        parallel_limit = min(5, len(assigned_probes))
        logger.info(
            (
                "Kwork registration batch started: {} account(s), {} assigned route(s), "
                "{} healthy reserve route(s), parallel limit {}"
            ),
            account_count,
            len(assigned_probes),
            len(reserve_probes),
            parallel_limit,
        )

        async def register_one(
            *,
            batch_index: int,
            proxy_index: int,
            vpnte_slot: int,
            proxy: str,
            preflight_ip: str,
        ) -> tuple[int, dict[str, Any]]:
            try:
                logger.info(
                    "Kwork registration {}/{} started through VPNTE slot {}",
                    batch_index,
                    account_count,
                    vpnte_slot,
                )
                item = await self.register_account(
                    email=email if account_count == 1 else "",
                    mail_password=mail_password,
                    user_type=user_type,
                    promo=promo,
                    use_simple=use_simple,
                    track_client_id=track_client_id,
                    action_after=action_after,
                    is_subscribed=is_subscribed,
                    captcha_token=captcha_token,
                    captcha_field=captcha_field,
                    firstmail_api_key=firstmail_api_key,
                    mail_provider=provider,
                    dry_run=dry_run,
                    proxy_url=proxy,
                    registration_slot=vpnte_slot,
                )
                item["batch_index"] = batch_index
                item["proxy_index"] = proxy_index
                item["vpnte_slot"] = vpnte_slot
                item["proxy_configured"] = True
                item["preflight_ip"] = preflight_ip
                logger.info(
                    "Kwork registration {}/{} finished with status {} through VPNTE slot {}",
                    batch_index,
                    account_count,
                    item.get("status") or "unknown",
                    vpnte_slot,
                )
                return batch_index, item
            except KworkRegistrationError as exc:
                logger.warning(
                    "Kwork registration {}/{} failed with {} through VPNTE slot {}",
                    batch_index,
                    account_count,
                    exc.code,
                    vpnte_slot,
                )
                return batch_index, {
                    "ok": False,
                    "status": "registration_error",
                    "batch_index": batch_index,
                    "proxy_index": proxy_index,
                    "vpnte_slot": vpnte_slot,
                    "proxy_configured": True,
                    "preflight_ip": preflight_ip,
                    "code": exc.code,
                    "message": str(exc),
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Kwork registration {}/{} failed with {} through VPNTE slot {}",
                    batch_index,
                    account_count,
                    type(exc).__name__,
                    vpnte_slot,
                )
                return batch_index, {
                    "ok": False,
                    "status": "registration_error",
                    "batch_index": batch_index,
                    "proxy_index": proxy_index,
                    "vpnte_slot": vpnte_slot,
                    "proxy_configured": True,
                    "preflight_ip": preflight_ip,
                    "code": "transport_error",
                    "message": f"{type(exc).__name__}: {exc}",
                }

        # Every account receives its own preflight-confirmed egress IP. Extra
        # healthy routes stay unused as reserve instead of hiding bad early slots.
        pending = [
            (index + 1, index + 1, probe.slot, probe.proxy, str(probe.egress_ip))
            for index, probe in enumerate(assigned_probes)
        ]
        results_by_index: dict[int, dict[str, Any]] = {}
        while pending:
            wave: list[tuple[int, int, int, str, str]] = []
            deferred: list[tuple[int, int, int, str, str]] = []
            wave_proxies: set[str] = set()
            for item in pending:
                _batch_index, _proxy_index, _vpnte_slot, proxy, _preflight_ip = item
                if len(wave) < 5 and proxy not in wave_proxies:
                    wave.append(item)
                    wave_proxies.add(proxy)
                else:
                    deferred.append(item)

            completed = await asyncio.gather(
                *(
                    register_one(
                        batch_index=batch_index,
                        proxy_index=proxy_index,
                        vpnte_slot=vpnte_slot,
                        proxy=proxy,
                        preflight_ip=preflight_ip,
                    )
                    for batch_index, proxy_index, vpnte_slot, proxy, preflight_ip in wave
                )
            )
            results_by_index.update(completed)
            pending = deferred

        results = [results_by_index[index] for index in range(1, account_count + 1)]

        activated_count = sum(1 for item in results if item.get("activated"))
        logger.info(
            "Kwork registration batch completed: {}/{} account(s) activated",
            activated_count,
            account_count,
        )
        return {
            "ok": activated_count == account_count,
            "requested_count": account_count,
            "completed_count": len(results),
            "activated_count": activated_count,
            "proxy_count": len(assigned_probes),
            "selected_proxy_count": len(vpnte_proxies),
            "reachable_proxy_count": sum(1 for probe in route_probes if probe.healthy),
            "healthy_proxy_count": len(available_probes),
            "reserve_proxy_count": len(reserve_probes),
            "failed_proxy_count": len(failed_probes),
            "duplicate_proxy_count": len(duplicate_probes),
            "used_ip_proxy_count": len(used_ip_probes),
            "avoid_used_ips": avoid_used_ips,
            "parallel_limit": parallel_limit,
            "vpnte_slots": [probe.slot for probe in assigned_probes],
            "selected_vpnte_slots": selected_slots,
            "reserve_vpnte_slots": [probe.slot for probe in reserve_probes],
            "failed_vpnte_slots": [probe.slot for probe in failed_probes],
            "duplicate_vpnte_slots": [probe.slot for probe in duplicate_probes],
            "used_ip_vpnte_slots": [probe.slot for probe in used_ip_probes],
            "results": results,
        }

    async def verify_registration_activation(
        self,
        *,
        registration_id: str,
        mail_password: str = "",
        firstmail_api_key: str = "",
    ) -> dict[str, Any]:
        """Resume activation in the encrypted HTTP session created by signup."""

        clean_registration_id = registration_id.strip()
        if not clean_registration_id:
            raise KworkRegistrationError("registration_id_required", "registration_id is required to restore the signup session")
        try:
            record = self._registration_store().get(clean_registration_id)
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not restore Kwork account session") from exc
        if record is None:
            raise KworkRegistrationError("registration_not_found", "Saved Kwork registration was not found")
        if record.status == "activated":
            result = record.public_data()
            result.update(
                ok=True,
                activated=True,
                captcha_required=False,
                phone_fields_sent=False,
                message="Account was already activated in the saved session",
            )
            return result

        logger.info(
            "Kwork activation retry started for {} through saved VPNTE slot {}",
            record.username,
            record.registration_slot or "unknown",
        )
        provider = self._registration_mail_provider(record.mail_provider)
        if provider == "firstmail" and not mail_password.strip():
            raise KworkRegistrationError("invalid_mail_credentials", "Firstmail requires mailbox credentials")
        try:
            after = datetime.fromisoformat(record.registration_started_at.replace("Z", "+00:00"))
            if after.tzinfo is None:
                after = after.replace(tzinfo=UTC)
            else:
                after = after.astimezone(UTC)
        except ValueError as exc:
            raise KworkRegistrationError("invalid_registration_timestamp", "Saved registration timestamp is invalid") from exc

        auth_data = record.session.get("auth_data")
        if not isinstance(auth_data, Mapping):
            auth_data = {}
        proxy = str(record.session.get("proxy_url") or "") or None
        result: dict[str, Any] = {
            **record.public_data(),
            "ok": False,
            "status": "activation_pending",
            "activated": False,
            "captcha_required": False,
            "phone_fields_sent": False,
        }

        async with self._registration_http_client(proxy=proxy, session=record.session) as client:
            try:
                link = await self._fetch_verification_link(
                    record.email,
                    mail_password,
                    firstmail_api_key or os.getenv("FIRSTMAIL_API_KEY"),
                    mail_provider=provider,
                    after=after,
                    proxy=proxy,
                )
            except Exception as exc:
                message = f"Mailbox polling failed: {type(exc).__name__}: {exc}"
                logger.warning("Kwork activation retry for {} failed during mailbox polling: {}", record.username, message)
                result["message"] = message
                self._save_registration_state(
                    result,
                    password=record.password,
                    client=client,
                    proxy=proxy,
                    auth_data=auth_data,
                    last_error=message,
                )
                return result

            if not link:
                logger.info("Kwork activation retry for {} is still pending: activation link not found", record.username)
                result["message"] = "Activation link was not found yet"
                self._save_registration_state(
                    result,
                    password=record.password,
                    client=client,
                    proxy=proxy,
                    auth_data=auth_data,
                    last_error="activation_link_not_found",
                )
                return result

            await self._registration_pacer(proxy).wait()
            activation = await self._activate_account(link, client)
            result.update(activation, activation_link_found=True)
            activation_ip = await self._capture_registration_ip(client)
            session_proof = await self._verify_registration_session(client) if activation.get("http_ok") else {
                "ok": False,
                "auth_mode": "saved_http_session",
                "reason": "activation_http_failed",
            }
            result["session_proof"] = session_proof
            if activation.get("http_ok"):
                proof = await self._verify_activated_account(record.email, record.password, proxy=proxy)
                result["post_activation"] = proof
                result["activated"] = bool(session_proof.get("ok") or proof.get("ok"))
            result["status"] = "activated" if result["activated"] else "activation_failed"
            result["ok"] = bool(result["activated"])
            self._save_registration_state(
                result,
                password=record.password,
                client=client,
                proxy=proxy,
                auth_data=auth_data,
                activation_ip=activation_ip,
                activated_at=(
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    if result["activated"]
                    else None
                ),
                last_error=None if result["activated"] else str(result.get("reason") or "activation_failed"),
            )
            logger.info(
                "Kwork activation retry for {} finished with status {}",
                record.username,
                result["status"],
            )
        return result

    def list_registration_accounts(self) -> list[dict[str, Any]]:
        """Return the locally saved registration inventory without credentials or cookies."""

        try:
            records = self._registration_store().list()
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not read saved Kwork registrations") from exc
        return [record.public_data() for record in records]

    async def check_registration_account_session(self, registration_id: str) -> dict[str, Any]:
        """Check whether one saved HTTP session is still authenticated on Kwork."""

        clean_registration_id = registration_id.strip()
        if not clean_registration_id:
            raise KworkRegistrationError("registration_id_required", "registration_id is required")
        try:
            record = self._registration_store().get(clean_registration_id)
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not restore Kwork account session") from exc
        if record is None:
            raise KworkRegistrationError("registration_not_found", "Saved Kwork registration was not found")

        proxy = str(record.session.get("proxy_url") or "") or None
        async with self._registration_http_client(proxy=proxy, session=record.session) as client:
            await self._registration_pacer(proxy).wait()
            session_check = await self._verify_registration_session(client)
        return {**record.public_data(), "session_check": session_check}

    def delete_registration_account(self, registration_id: str) -> dict[str, Any]:
        """Remove one locally saved account record without deleting the remote Kwork account."""

        clean_registration_id = registration_id.strip()
        if not clean_registration_id:
            raise KworkRegistrationError("registration_id_required", "registration_id is required")
        try:
            deleted = self._registration_store().delete(clean_registration_id)
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not delete saved Kwork registration") from exc
        if not deleted:
            raise KworkRegistrationError("registration_not_found", "Saved Kwork registration was not found")
        return {"ok": True, "registration_id": clean_registration_id}

    def get_registration_credentials(self, registration_id: str) -> dict[str, Any]:
        """Return the generated credentials for a locally stored registration."""

        clean_registration_id = registration_id.strip()
        if not clean_registration_id:
            raise KworkRegistrationError("registration_id_required", "registration_id is required")
        try:
            record = self._registration_store().get(clean_registration_id)
        except KworkAccountStoreError as exc:
            raise KworkRegistrationError("account_persistence_failed", "Could not restore Kwork account credentials") from exc
        if record is None:
            raise KworkRegistrationError("registration_not_found", "Saved Kwork registration was not found")
        return {
            "registration_id": record.registration_id,
            "email": record.email,
            "username": record.username,
            "password": record.password,
        }

    @staticmethod
    def _signup_endpoint_missing(payload: Mapping[str, Any]) -> bool:
        text = json.dumps(payload, ensure_ascii=False).lower()
        return any(marker in text for marker in ("not found", "unknown endpoint", "method not allowed", "simplesignup"))

    def reset_api(self) -> None:
        """Reset current client for re-authorization.

        Respects the per-cycle limit of 3 resets (Requirement 7.7).
        """
        if self._reset_count >= self._MAX_RESETS_PER_CYCLE:
            logger.warning(
                f"KworkService: достигнут лимит сбросов сессии ({self._MAX_RESETS_PER_CYCLE}) "
                f"за текущий цикл, сброс пропущен"
            )
            return
        self._api = None
        self._reset_count += 1
        logger.info(
            f"KworkService: API-клиент сброшен для реавторизации "
            f"(сброс {self._reset_count}/{self._MAX_RESETS_PER_CYCLE})"
        )

    @staticmethod
    def is_auth_error(error: BaseException) -> bool:
        status = getattr(error, "status", None)
        if status in {401, 403}:
            return True

        details = str(error).lower()
        response_json = getattr(error, "response_json", None)
        if isinstance(response_json, dict):
            details += " " + json.dumps(response_json, ensure_ascii=False).lower()
        payload = getattr(error, "payload", None)
        if isinstance(payload, dict):
            details += " " + json.dumps(payload, ensure_ascii=False).lower()

        markers = (
            "unauthorized",
            "forbidden",
            "invalid token",
            "invalid session",
            "session expired",
            "auth failed",
            "not authenticated",
        )
        return any(marker in details for marker in markers)

    @staticmethod
    def _payload_summary(data: dict[str, Any]) -> str:
        keys = sorted(str(key) for key in data.keys())
        error = data.get("error") or data.get("message") or data.get("errors")
        return f"keys={keys}, success={data.get('success')!r}, error={error!r}"

    async def close(self) -> None:
        """Close resources and release API clients."""
        clients = []
        if self._api is not None:
            clients.append(("_api", self._api))
        if self._token_api is not None and self._token_api is not self._api:
            clients.append(("_token_api", self._token_api))
        for attr, api in clients:
            try:
                await api.close()
            except Exception:
                pass
            setattr(self, attr, None)
        self._api = None
        self._token_api = None

    async def get_projects(
        self,
        *,
        categories_ids: list[int],
        page: int,
        query: str,
        price_from: int | None = None,
        price_to: int | None = None,
        hiring_from: int | None = None,
        kworks_filter_from: int | None = None,
        kworks_filter_to: int | None = None,
    ) -> list[Any]:
        """Call API with response validation."""
        api = await self.get_api()
        if not api:
            return []

        from kwork.schema.project import WantWorker

        categories = ",".join(str(category_id) for category_id in categories_ids) if categories_ids else "all"
        data = await api.request(
            "post",
            "projects",
            use_token=True,
            categories=categories,
            page=page,
            query=query,
            price_from=price_from,
            price_to=price_to,
            hiring_from=hiring_from,
            kworks_filter_from=kworks_filter_from,
            kworks_filter_to=kworks_filter_to,
        )
        response = data.get("response") if isinstance(data, dict) else None
        if response is None:
            if isinstance(data, dict) and data.get("success") is True and "paging" in data:
                logger.debug(
                    f"Kwork /projects returned no response list; treating as empty ({self._payload_summary(data)})"
                )
                return []
            summary = self._payload_summary(data) if isinstance(data, dict) else f"type={type(data).__name__}"
            payload = data if isinstance(data, dict) else None
            raise KworkAPIResponseError(f"Kwork /projects response missing project list ({summary})", payload)

        if isinstance(response, dict):
            response = (
                response.get("data") or response.get("items") or response.get("wants") or response.get("projects")
            )
        if not isinstance(response, list):
            summary = self._payload_summary(data) if isinstance(data, dict) else f"type={type(data).__name__}"
            payload = data if isinstance(data, dict) else None
            raise KworkAPIResponseError(f"Kwork /projects response has unexpected shape ({summary})", payload)

        result = []
        for item in response:
            if not isinstance(item, dict):
                continue
            try:
                result.append(WantWorker(**item))
            except Exception as e:
                logger.debug(f"KworkService: skip malformed project item: {e}")
        return result

    async def fetch_client_data(self, project_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None

        try:
            if not user_id:
                result = await api.project(project_id=int(project_id))
                response = result.get("response", {}) if isinstance(result, dict) else {}
                user_id = response.get("user_id")
            if not user_id:
                return None

            user = await api.get_user(user_id=int(user_id))
            if hasattr(user, "model_dump"):
                data = user.model_dump()
            elif isinstance(user, dict):
                data = user
            else:
                data = {}

            return {
                "rating": data.get("rating", 0),
                "rating_count": data.get("rating_count", 0),
                "good_reviews": data.get("good_reviews", 0),
                "bad_reviews": data.get("bad_reviews", 0),
                "completed_orders_count": data.get("completed_orders_count", 0),
                "order_done_persent": data.get("order_done_persent", 0),
                "order_done_intime_persent": data.get("order_done_intime_persent", 0),
                "order_done_repeat_persent": data.get("order_done_repeat_persent", 0),
                "reg_date": data.get("addtime", ""),
                "online": data.get("online", False),
                "location": data.get("location", ""),
                "username": data.get("username", ""),
                "achievments_count": len(data.get("achievments_list") or []),
                "blocked_by_user": data.get("blocked_by_user", False),
                "allowed_dialog": data.get("allowed_dialog", False),
                "kworks_count": data.get("kworks_count", 0),
                "raw": data,
            }
        except Exception as e:
            logger.debug(f"KworkService: не удалось получить данные заказчика: {e}")
            return None

    async def get_all_categories(self) -> list[dict[str, Any]]:
        """Получить полное дерево категорий Kwork (динамически, не хардкод)."""
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_categories_raw(api)

    async def get_all_category_ids(self) -> list[int]:
        """Получить все ID категорий для поиска по всем рубрикам."""
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_all_category_ids(api)

    async def get_project_details_raw(self, project_id: str | int) -> dict[str, Any] | None:
        """Получить детали проекта через API (skills, files, dates)."""
        api = await self.get_token_api()
        if not api:
            return None
        return await KworkExtensions.get_project_details(api, project_id)

    async def send_message(self, user_id: int, text: str) -> dict[str, Any] | None:
        """Отправить сообщение клиенту через Kwork чат."""
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.send_message_to_client(api, user_id, text)

    async def get_dialog_history(self, username: str) -> list[dict[str, Any]]:
        """Получить полную историю диалога с клиентом."""
        api = await self.get_api()
        if not api:
            return []
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_dialog_history(api, username)

    async def get_worker_orders(self, status_filter: str = "all") -> list[dict[str, Any]]:
        """Получить заказы фрилансера для трекинга доставки."""
        api = await self.get_api()
        if not api:
            return []
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_worker_orders(api, status_filter)

    async def get_notifications(self) -> list[dict[str, Any]]:
        """Получить уведомления (новые заказы, отзывы, статусы)."""
        api = await self.get_api()
        if not api:
            return []
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_notifications(api)

    async def get_connects_info(self) -> dict[str, Any]:
        """Получить баланс connects (лимит откликов)."""
        api = await self.get_api()
        if not api:
            return {}
        return await KworkExtensions.get_connects_info(api)

    async def check_connects(self) -> dict[str, Any]:
        """Проверить баланс connects с кэшированием и предупреждениями."""
        api = await self.get_api()
        if not api:
            return {}
        from src.platforms.kwork_ext import get_connects_monitor

        return await get_connects_monitor().check(api)

    async def check_success_rate(self) -> dict[str, Any]:
        """Проверить success rate фрилансера (completed/cancelled ratio)."""
        api = await self.get_api()
        if not api:
            return {}
        from src.platforms.kwork_ext import get_success_rate_monitor

        return await get_success_rate_monitor().check(api)

    def can_send_proposal(self) -> bool:
        """Проверить, достаточно ли connects и success rate для отправки отклика."""
        from src.platforms.kwork_ext import get_connects_monitor, get_success_rate_monitor

        return get_connects_monitor().can_send() and get_success_rate_monitor().can_send()

    @staticmethod
    def _web_projects_filter_id(kworks_filter_from: int | None, kworks_filter_to: int | None) -> str | None:
        if kworks_filter_from in (None, 0) and kworks_filter_to is not None and kworks_filter_to <= 5:
            return "0"
        if kworks_filter_from == 5 and kworks_filter_to == 10:
            return "1"
        if kworks_filter_from == 10 and kworks_filter_to == 15:
            return "2"
        if kworks_filter_from == 15 and kworks_filter_to == 20:
            return "3"
        if kworks_filter_from is not None and kworks_filter_from >= 20 and kworks_filter_to is None:
            return "4"
        return None

    @staticmethod
    def _api_proxy_url(api: Any) -> str | None:
        """Return the proxy URL captured when a Kwork client was created."""

        for name in ("_psr_proxy_url", "_proxy"):
            value = getattr(api, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    async def _cached_api_proxy_is_live(self, api: Any) -> bool:
        """Avoid reusing a Kwork session after its local VPNTE proxy disappeared."""

        from src.utils.vpnte_proxy import get_vpnte_proxy_client, vpnte_proxy_enabled

        if not vpnte_proxy_enabled():
            return True
        proxy_url = self._api_proxy_url(api)
        if not proxy_url:
            # Clients created without a proxy cannot be validated through VPNTE.
            return True
        try:
            live_proxy = get_vpnte_proxy_client().next_proxy(rotate=False)
        except Exception as exc:
            logger.debug(f"KworkService: VPNTE live-proxy check failed: {exc}")
            # A temporary control API failure should not destroy a usable cache.
            return True
        if live_proxy == proxy_url:
            return True
        logger.warning(
            f"KworkService: cached proxy is stale ({proxy_url}); "
            f"current VPNTE proxy is {live_proxy or 'unavailable'}"
        )
        return False

    async def _invalidate_cached_api(self, api: Any) -> None:
        """Close a stale client before rebuilding it with a live VPNTE proxy."""

        if self._api is api:
            self._api = None
        if self._token_api is api:
            self._token_api = None
        try:
            close = getattr(api, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.debug(f"KworkService: stale Kwork client close failed: {exc}")

    @staticmethod
    def _project_item_to_raw_dict(project: ProjectItem) -> dict[str, Any]:
        platform_data = project.platform_data if isinstance(project.platform_data, dict) else {}
        user = platform_data.get("user") if isinstance(platform_data.get("user"), dict) else {}
        return {
            "id": project.id,
            "title": project.title,
            "description": project.description,
            "price": project.budget,
            "possible_price_limit": platform_data.get("possible_price_limit"),
            "offers": project.offers_count,
            "date_create": project.created_at or platform_data.get("date_create"),
            "category_id": platform_data.get("category_id"),
            "classification_id": platform_data.get("classification_id"),
            "parent_category_id": platform_data.get("parent_category_id"),
            "views": platform_data.get("views_dirty"),
            "user_id": project.client_user_id,
            "username": user.get("username"),
            "user_hired_percent": project.client_hired_percent,
            "user_projects_count": platform_data.get("user_projects_count"),
            "user_active_projects_count": platform_data.get("user_active_projects_count"),
            "allow_higher_price": platform_data.get("allow_higher_price"),
            "is_higher_price": platform_data.get("is_higher_price"),
            "user_need_portfolio": platform_data.get("user_need_portfolio"),
            "url": project.url,
            "platform_data": platform_data,
            "source": "kwork_web_projects_state",
        }

    async def get_web_raw_projects(
        self,
        *,
        categories: str = "all",
        page: int = 1,
        query: str = "",
        price_from: int | None = None,
        price_to: int | None = None,
        hiring_from: int | None = None,
        kworks_filter_from: int | None = None,
        kworks_filter_to: int | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read buyer projects from Kwork web stateData using Session Hub cookies."""
        import httpx

        cookie_dict = await self._fetch_session_hub_cookies()
        if not cookie_dict:
            return [], {"source": "web_state", "auth_mode": "cookie-only", "error": "missing web cookies"}

        params: dict[str, Any] = {"page": max(1, int(page or 1))}
        category_text = str(categories or "all").strip()
        if category_text and category_text != "all":
            parts = [part.strip() for part in category_text.split(",") if part.strip()]
            if len(parts) == 1 and parts[0].isdigit():
                params["c"] = parts[0]
        if query:
            params["keyword"] = query
        if price_from is not None:
            params["price-from"] = int(price_from)
        if price_to is not None:
            params["price-to"] = int(price_to)
        if hiring_from is not None:
            params["hiring-from"] = int(hiring_from)
        web_kworks_filter = self._web_projects_filter_id(kworks_filter_from, kworks_filter_to)
        if web_kworks_filter is not None:
            params["kworks-filters"] = web_kworks_filter
        if filters.get("prices_filters") is not None:
            params["prices-filters"] = filters["prices_filters"]

        try:
            async with httpx.AsyncClient(
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "User-Agent": "Mozilla/5.0 PSR-KworkWebProjects/1.0",
                },
                cookies=cookie_dict,
                timeout=12.0,
                follow_redirects=True,
                proxy=kwork_http_proxy_url(rotate=False),
                trust_env=False,
            ) as client:
                response = await client.get(f"{KWORK_BASE_URL}/projects", params=params)
            state = KworkStateDataParser.extract(response.text) or {}
            projects = [self._project_item_to_raw_dict(item) for item in KworkStateDataParser.projects_from_state(state)]
            pagination = state.get("pagination") if isinstance(state.get("pagination"), dict) else {}
            return projects, {
                "source": "web_state",
                "auth_mode": "cookie-only",
                "url": str(response.url),
                "status_code": response.status_code,
                "filter": state.get("filter"),
                "paging": {
                    "page": pagination.get("current_page") or page,
                    "total": pagination.get("total") or len(projects),
                    "per_page": pagination.get("per_page") or len(projects),
                },
            }
        except Exception as e:
            logger.debug(f"KworkService: failed to read web projects state: {e}")
            return [], {"source": "web_state", "auth_mode": "cookie-only", "error": str(e)}

    async def get_raw_projects(
        self,
        *,
        categories: str = "all",
        page: int = 1,
        query: str = "",
        price_from: int | None = None,
        price_to: int | None = None,
        hiring_from: int | None = None,
        kworks_filter_from: int | None = None,
        kworks_filter_to: int | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Получить сырые проекты со всеми полями API (без WantWorker)."""
        api = await self.get_token_api()
        if not api:
            return [], {}
        if getattr(api, "_psr_auth_mode", "") == "cookie-only":
            return await self.get_web_raw_projects(
                categories=categories,
                page=page,
                query=query,
                price_from=price_from,
                price_to=price_to,
                hiring_from=hiring_from,
                kworks_filter_from=kworks_filter_from,
                kworks_filter_to=kworks_filter_to,
                **filters,
            )
        return await KworkExtensions.get_raw_projects(
            api,
            categories=categories,
            page=page,
            query=query,
            price_from=price_from,
            price_to=price_to,
            hiring_from=hiring_from,
            kworks_filter_from=kworks_filter_from,
            kworks_filter_to=kworks_filter_to,
            **filters,
        )

    async def upload_offer_file(self, project_id: int, file_path: str, **kwargs: Any) -> dict[str, Any] | None:
        """Загрузить файл к отклику (attachments)."""
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.upload_file_to_offer(api, project_id, file_path, **kwargs)

    async def approve_order(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.approve_order(api, order_id)

    async def cancel_order_by_worker(self, order_id: int, reason: str = "") -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.cancel_order_by_worker(api, order_id, reason)

    async def get_order_details(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_order_details(api, order_id)

    async def get_order_header(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_order_header(api, order_id)

    async def get_order_files(self, order_id: int) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.get_order_files(api, order_id)

    async def create_review(
        self, order_id: int, rating: int = 5, text: str = "", body: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.create_review(api, order_id, rating, text, body)

    async def get_kwork_reviews_api(self, kwork_id: int, page: int = 1) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_kwork_reviews(api, kwork_id, page)

    async def inbox_read(self, message_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.inbox_read(api, message_id)

    async def mark_inbox_read(self, dialog_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.mark_inbox_tracks_as_read(api, dialog_id)

    async def set_typing(self, recipient_id: int) -> None:
        api = await self.get_api()
        if not api:
            return
        await self._sync_session_hub_cookies(api)
        await KworkExtensions.set_typing(api, recipient_id)

    async def is_dialog_allowed(self, user_id: int) -> bool:
        api = await self.get_api()
        if not api:
            return True
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.is_dialog_allow(api, user_id)

    async def get_wants_count(self, categories: str = "all", **filters: Any) -> int:
        api = await self.get_token_api()
        if not api:
            return 0
        if getattr(api, "_psr_auth_mode", "") == "cookie-only":
            _projects, meta = await self.get_web_raw_projects(categories=categories, page=1, **filters)
            paging = meta.get("paging") if isinstance(meta.get("paging"), dict) else {}
            return int(paging.get("total") or 0)
        return await KworkExtensions.get_wants_count(api, categories, **filters)

    async def exchange_info(self) -> dict[str, Any]:
        api = await self.get_api()
        if not api:
            return {}
        return await KworkExtensions.exchange_info(api)

    async def get_offers(self) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_offers(api)

    async def get_offer(self, offer_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_offer(api, offer_id)

    async def delete_offer(self, offer_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.delete_offer(api, offer_id)

    async def upload_file(self, file_path: str) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.upload_file(api, file_path)

    async def get_user_info_api(self, user_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_user_info(api, user_id)

    async def get_kwork_details_api(self, kwork_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_kwork_details(api, kwork_id)

    async def get_kworks_list(self, category_id: int, page: int = 1) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_kworks_list(api, category_id, page)

    async def get_kworks_status(self) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_kworks_status_list(api)

    async def pause_kwork(self, kwork_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.pause_kwork(api, kwork_id)

    async def get_badges_info(self) -> dict[str, Any]:
        api = await self.get_api()
        if not api:
            return {}
        return await KworkExtensions.get_badges_info(api)

    async def get_captcha_status(self) -> bool:
        api = await self.get_api()
        if not api:
            return False
        return await KworkExtensions.get_captcha_status(api)

    async def get_current_versions(self) -> dict[str, Any]:
        api = await self.get_api()
        if not api:
            return {}
        return await KworkExtensions.get_current_versions(api)

    async def apply_filters(self, **filters: Any) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.apply_filters(api, **filters)

    async def orders_between(self, user_id: int) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.orders_between(api, user_id)

    async def check_is_template(self, project_id: int, description: str) -> bool:
        """Проверить, пометит ли Kwork текст отклика как шаблонный."""
        api = await self.get_api()
        if not api:
            return False
        return await KworkExtensions.is_text_template_flagged(api, project_id, description)

    async def check_web_session(self) -> bool:
        """Проверить валидность web-сессии."""
        api = await self.get_api()
        if not api:
            return False
        return await KworkExtensions.check_web_session(api)

    async def check_account_health(self) -> dict[str, Any]:
        """Полный health check аккаунта — connects, success rate, active orders, captcha, level."""
        api = await self.get_api()
        if not api:
            return {}
        from src.platforms.kwork_ext import get_account_health_monitor

        return await get_account_health_monitor().check(api)

    async def auto_review_completed(self, order_id: int, rating: int = 5, text: str = "") -> dict[str, Any] | None:
        """Автоматически оставить отзыв после завершения заказа.

        Отзывы = главный фактор ранкинга на Kwork.
        10-15 отзывов = поток клиентов начинается.
        """
        if not text:
            text = os.getenv("KWORK_AUTO_REVIEW_TEXT", "Спасибо за заказ! Буду рад сотрудничеству в будущем.")
        api = await self.get_api()
        if not api:
            return None
        await self._sync_session_hub_cookies(api)
        return await KworkExtensions.create_review(api, order_id, rating=rating, text=text)


_service: KworkService | None = None


def get_kwork_service() -> KworkService:
    global _service
    if _service is None:
        _service = KworkService()
    return _service
