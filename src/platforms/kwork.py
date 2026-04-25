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

from src.parsers.base_parser import ProjectItem


KWORK_BASE_URL = "https://kwork.ru"
STATE_MARKER = "window.stateData="


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
            "available_durations": want.get("availableDurations") or [],
            "max_days": want.get("max_days"),
            "date_active": want.get("date_active"),
            "date_expire": want.get("date_expire"),
            "views_dirty": want.get("views_dirty"),
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
    """Shared Kwork API/session service."""

    timeout: float = 30.0
    retry_max_attempts: int = 2

    def __post_init__(self) -> None:
        self._api: Any = None

    def get_api(self) -> Any | None:
        if self._api is not None:
            return self._api

        email = os.getenv("KWORK_EMAIL")
        password = os.getenv("KWORK_PASSWORD")
        if not email or not password:
            logger.warning("KworkService: нет KWORK_EMAIL/KWORK_PASSWORD")
            return None

        from kwork import Kwork

        self._api = Kwork(
            login=email,
            password=password,
            timeout=self.timeout,
            retry_max_attempts=max(1, self.retry_max_attempts),
            proxy=os.getenv("PROXY_URL") or None,
            relogin_on_auth_error=True,
        )
        logger.info("KworkService: общий API-клиент инициализирован")
        return self._api

    def reset_api(self) -> None:
        self._api = None

    async def close(self) -> None:
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
        api = self.get_api()
        if not api:
            return []
        return await api.get_projects(
            categories_ids=categories_ids,
            page=page,
            query=query,
            price_from=price_from,
            price_to=price_to,
            hiring_from=hiring_from,
            kworks_filter_from=kworks_filter_from,
            kworks_filter_to=kworks_filter_to,
        )

    async def fetch_client_data(self, project_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        api = self.get_api()
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


_service: KworkService | None = None


def get_kwork_service() -> KworkService:
    global _service
    if _service is None:
        _service = KworkService()
    return _service
