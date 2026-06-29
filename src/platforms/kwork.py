"""Kwork integration primitives.

Kwork pages expose a large `window.stateData` payload. Parsing that payload is
more stable than relying only on CSS classes, and it gives us fields that are
not always present in the rendered card text: buyer hiring percent, files,
dates, category ids, price limits and raw user metadata.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.platforms.kwork_ext import KworkExtensions, apply_kwork_patches

apply_kwork_patches()

from src.parsers.base_parser import ProjectItem


KWORK_BASE_URL = "https://kwork.ru"
STATE_MARKER = "window.stateData="


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


def _strip_html(value: Any) -> str:
    text = str(value or "")
    if "<" not in text and "&" not in text:
        return text.strip()
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    except Exception:
        return text.strip()


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
        title = str(want.get("name") or want.get("title") or "").strip()
        description = _strip_html(want.get("description") or "")
        budget = _as_float(want.get("priceLimit")) or _as_float(want.get("possiblePriceLimit"))
        offers_count = _as_int(want.get("kwork_count") or want.get("offers"))
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
        self._reset_count: int = 0

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
            return self._api

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
        if not email or not password:
            logger.debug("KworkService: Session Hub cookies found path skipped without KWORK_EMAIL/KWORK_PASSWORD")
            return None

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{hub_url}?domain=kwork.ru",
                    timeout=10.0,
                )
            if resp.status_code != 200:
                logger.debug(f"KworkService: Session Hub вернул HTTP {resp.status_code}")
                return None

            data = resp.json()
            if data.get("status") != "ok":
                logger.debug(f"KworkService: Session Hub статус: {data.get('status')}")
                return None

            cookies = data.get("cookies", [])
            if not cookies:
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

            # Create Kwork client with cookies from Session Hub
            from kwork import Kwork
            from src.platforms.kwork_ext import get_proxy_rotator

            phone = os.getenv("KWORK_PHONE", "")
            api = Kwork(
                login=email,
                password=password,
                phone_last=phone if phone else None,
                timeout=self.timeout,
                retry_max_attempts=max(1, self.retry_max_attempts),
                proxy=get_proxy_rotator().next() if os.getenv("KWORK_PROXY_LIST") else (os.getenv("PROXY_URL") or None),
            )
            # Inject Session Hub cookies into the client session
            if hasattr(api, "_session") and api._session is not None:
                api._session.cookie_jar.update_cookies(cookie_dict)
            elif hasattr(api, "session") and api.session is not None:
                api.session.cookie_jar.update_cookies(cookie_dict)

            logger.info(
                f"KworkService: API-клиент инициализирован через Session Hub "
                f"({len(cookie_dict)} кук)"
            )
            return api

        except Exception as e:
            logger.debug(f"KworkService: Session Hub недоступен ({e})")
            return None

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

        # Retry once with 5s delay on auth failure (Requirement 7.5)
        logger.info("KworkService: повторная попытка авторизации через 5с...")
        await asyncio.sleep(5)

        api = await self._create_api_client(email, password)
        if api is not None:
            logger.info("KworkService: повторная авторизация успешна")
            return api

        # Both attempts failed (Requirement 7.6)
        logger.error("KworkService: авторизация не удалась после повторной попытки")
        return None

    async def _create_api_client(self, email: str, password: str) -> Any | None:
        """Create and authenticate a Kwork API client. Returns None on failure."""
        try:
            from kwork import Kwork

            from src.platforms.kwork_ext import get_proxy_rotator

            proxy = get_proxy_rotator().next()

            api = Kwork(
                login=email,
                password=password,
                timeout=self.timeout,
                retry_max_attempts=max(1, self.retry_max_attempts),
                proxy=proxy,
                relogin_on_auth_error=True,
            )
            logger.info(f"KworkService: API-клиент инициализирован через email/password (proxy={'yes' if proxy else 'no'})")
            return api
        except Exception as e:
            logger.warning(f"KworkService: ошибка авторизации email/password: {e}")
            return None

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
            "auth",
            "authorization",
            "token",
            "login",
            "session",
            "signin",
        )
        return any(marker in details for marker in markers)

    @staticmethod
    def _payload_summary(data: dict[str, Any]) -> str:
        keys = sorted(str(key) for key in data.keys())
        error = data.get("error") or data.get("message") or data.get("errors")
        return f"keys={keys}, success={data.get('success')!r}, error={error!r}"

    async def close(self) -> None:
        """Close resources and release the API client."""
        if self._api is not None:
            try:
                await self._api.close()
            except Exception:
                pass
            self._api = None

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
                logger.debug(f"Kwork /projects returned no response list; treating as empty ({self._payload_summary(data)})")
                return []
            summary = self._payload_summary(data) if isinstance(data, dict) else f"type={type(data).__name__}"
            payload = data if isinstance(data, dict) else None
            raise KworkAPIResponseError(f"Kwork /projects response missing project list ({summary})", payload)

        if isinstance(response, dict):
            response = (
                response.get("data")
                or response.get("items")
                or response.get("wants")
                or response.get("projects")
            )
        if not isinstance(response, list):
            summary = self._payload_summary(data) if isinstance(data, dict) else f"type={type(data).__name__}"
            payload = data if isinstance(data, dict) else None
            raise KworkAPIResponseError(f"Kwork /projects response has unexpected shape ({summary})", payload)

        return [WantWorker(**item) for item in response if isinstance(item, dict)]

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
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_project_details(api, project_id)

    async def send_message(self, user_id: int, text: str) -> dict[str, Any] | None:
        """Отправить сообщение клиенту через Kwork чат."""
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.send_message_to_client(api, user_id, text)

    async def get_dialog_history(self, username: str) -> list[dict[str, Any]]:
        """Получить полную историю диалога с клиентом."""
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_dialog_history(api, username)

    async def get_worker_orders(self, status_filter: str = "all") -> list[dict[str, Any]]:
        """Получить заказы фрилансера для трекинга доставки."""
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_worker_orders(api, status_filter)

    async def get_notifications(self) -> list[dict[str, Any]]:
        """Получить уведомления (новые заказы, отзывы, статусы)."""
        api = await self.get_api()
        if not api:
            return []
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
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Получить сырые проекты со всеми полями API (без WantWorker)."""
        api = await self.get_api()
        if not api:
            return [], {}
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
        )

    async def upload_offer_file(self, project_id: int, file_path: str, **kwargs: Any) -> dict[str, Any] | None:
        """Загрузить файл к отклику (attachments)."""
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.upload_file_to_offer(api, project_id, file_path, **kwargs)

    async def approve_order(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.approve_order(api, order_id)

    async def cancel_order_by_worker(self, order_id: int, reason: str = "") -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.cancel_order_by_worker(api, order_id, reason)

    async def get_order_details(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_order_details(api, order_id)

    async def get_order_header(self, order_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.get_order_header(api, order_id)

    async def get_order_files(self, order_id: int) -> list[dict[str, Any]]:
        api = await self.get_api()
        if not api:
            return []
        return await KworkExtensions.get_order_files(api, order_id)

    async def create_review(self, order_id: int, rating: int = 5, text: str = "", body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
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
        return await KworkExtensions.inbox_read(api, message_id)

    async def mark_inbox_read(self, dialog_id: int) -> dict[str, Any] | None:
        api = await self.get_api()
        if not api:
            return None
        return await KworkExtensions.mark_inbox_tracks_as_read(api, dialog_id)

    async def set_typing(self, recipient_id: int) -> None:
        api = await self.get_api()
        if not api:
            return
        await KworkExtensions.set_typing(api, recipient_id)

    async def is_dialog_allowed(self, user_id: int) -> bool:
        api = await self.get_api()
        if not api:
            return True
        return await KworkExtensions.is_dialog_allow(api, user_id)

    async def get_wants_count(self, categories: str = "all", **filters: Any) -> int:
        api = await self.get_api()
        if not api:
            return 0
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
        return await KworkExtensions.create_review(api, order_id, rating=rating, text=text)


_service: KworkService | None = None


def get_kwork_service() -> KworkService:
    global _service
    if _service is None:
        _service = KworkService()
    return _service
