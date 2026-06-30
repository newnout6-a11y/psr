"""
Парсер Kwork.ru через библиотеку kwork (kesha1225/kwork).
Использует мобильный API (api.kwork.ru) — авторизация через email/password.
Быстрый, надёжный, без браузера.
"""

import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import yaml
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .base_parser import BaseParser, ProjectItem
from src.platforms.kwork import KworkAPIResponseError, KworkService, get_kwork_service


KWORK_CATEGORIES_MAP = {
    # Скрипты и автоматизация (cat 11)
    "python": [11],
    "парсер": [11],
    "скрейпинг": [11],
    "скрапинг": [11],
    "телеграм": [11],
    "бот": [11],
    "автоматизация": [11],
    "api": [11],
    "скрипт": [11],
    "fastapi": [11],
    "flask": [11],
    "selenium": [11],
    "библиотека": [11],
    "расширение": [11],
    "плагин": [11],
    "интеграция": [11],
    # Веб-разработка (cat 79)
    "сайт": [79],
    "веб": [79],
    "django": [79],
    "react": [79],
    "javascript": [79],
    "typescript": [79],
    "node": [79],
    "frontend": [79],
    "backend": [79],
    "верстка": [79],
    "лендинг": [79],
    # Мульти-категории
    "разработка": [11, 79],
    "приложение": [11, 79],
    "программирование": [11, 79],
    "все": [],
    "all": [],
}


class KworkAPIParser(BaseParser):
    PLATFORM_NAME = "kwork"
    BASE_URL = "https://kwork.ru"

    def __init__(self, proxy=None, browser_preset="chrome120"):
        super().__init__(proxy=proxy, browser_preset=browser_preset)
        self.service: KworkService = get_kwork_service()
        self.min_budget, self.max_budget, self.min_hiring, self.max_proposals = self._load_filters()

    def _load_filters(self) -> tuple[int, int, int, int]:
        try:
            config = yaml.safe_load(Path("config/filters.yaml").read_text(encoding="utf-8")) or {}
            min_budget = max(int(config.get("min_budget", 0) or 0), 0)
            max_budget = max(int(config.get("max_budget", 0) or 0), 0)
            min_hiring = max(int(config.get("min_client_hiring_percent", 0) or 0), 0)
            max_proposals = max(int(config.get("max_proposals", 0) or 0), 0)
            return min_budget, max_budget, min_hiring, max_proposals
        except Exception as e:
            logger.warning(f"KworkAPI: не удалось прочитать filters.yaml: {e}")
            return 0, 0, 0, 0

    async def _get_api(self):
        return await self.service.get_api()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    async def _get_projects_with_retry(
        self,
        api,
        *,
        categories_ids: list[int],
        page: int,
        query: str,
        price_from: int,
        price_to: int | None,
        hiring_from: int | None,
        kworks_filter_to: int | None,
    ):
        return await self.service.get_projects(
            categories_ids=categories_ids,
            page=page,
            query=query,
            price_from=price_from,
            price_to=price_to,
            hiring_from=hiring_from,
            kworks_filter_to=kworks_filter_to,
        )

    def _resolve_categories(self, query: str) -> list:
        """Разрешает категории из поискового запроса.

        Если в запросе есть "all"/"все" — возвращает ["all"] для поиска по всем рубрикам.
        Иначе использует маппинг ключевых слов на ID категорий.
        Динамические категории загружаются при первом вызове через KworkExtensions.
        """
        query_lower = query.lower().strip()

        # Явный "all" / "все" → поиск по всем категориям
        if query_lower in {"all", "все", "любые"}:
            return []

        words = query_lower.replace(",", " ").split()

        all_cats = set()
        for word in words:
            for key, cats in KWORK_CATEGORIES_MAP.items():
                if key in {"all", "все"}:
                    continue
                if key in word or word in key:
                    all_cats.update(cats)

        # Если ничего не нашли — ищем по всем категориям вместо дефолта на cat 11
        return sorted(all_cats) if all_cats else []

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        api = await self._get_api()
        if not api:
            logger.warning("KworkAPI: API недоступен, fallback на браузер")
            return await self._fallback_browser(page, per_page, filters)

        search_query = filters.get("query", "") if filters else ""

        # Поддержка нескольких запросов через | : "python|telegram бот|парсер"
        queries = [q.strip() for q in search_query.split("|") if q.strip()]
        if not queries:
            queries = [search_query or "python"]

        all_projects = []
        seen_ids = set()
        price_from = self.min_budget
        price_to = self.max_budget or None
        hiring_from = self.min_hiring or None
        kworks_filter_to = self.max_proposals or None
        api_failures = 0  # Счётчик подряд идущих ошибок API

        for query in queries:
            categories_ids = self._resolve_categories(query)
            started_at = time.perf_counter()

            # Если API уже падало 3+ раза подряд — сразу fallback на браузер
            if api_failures >= 3:
                logger.warning(f"KworkAPI: {api_failures} ошибок подряд, fallback на браузер для '{query}'")
                try:
                    browser_projects = await self._fallback_browser(page, per_page, filters)
                    for p in browser_projects:
                        if p.id not in seen_ids:
                            seen_ids.add(p.id)
                            all_projects.append(p)
                except Exception as e2:
                    logger.error(f"KworkAPI: браузерный fallback тоже упал: {e2}")
                continue

            try:
                raw_projects = await self._get_projects_with_retry(
                    api,
                    categories_ids=categories_ids,
                    page=page,
                    query=query,
                    price_from=price_from,
                    price_to=price_to,
                    hiring_from=hiring_from,
                    kworks_filter_to=kworks_filter_to,
                )

                for p in self._normalize(raw_projects):
                    if p.id not in seen_ids:
                        seen_ids.add(p.id)
                        all_projects.append(p)

                api_failures = 0  # Сброс счётчика при успехе

                duration_ms = int((time.perf_counter() - started_at) * 1000)
                logger.info(
                    f"KworkAPI: запрос='{query}', кат.={categories_ids}, "
                    f"price_from={price_from}, price_to={price_to}, hiring_from={hiring_from}, "
                    f"kworks_to={kworks_filter_to}, получено {len(raw_projects)} проектов "
                    f"(стр.{page}, {duration_ms}мс)"
                )
            except Exception as e:
                api_failures += 1
                logger.error(f"KworkAPI: ошибка для запроса '{query}': {e}")
                # Сессия протухла — сбросить клиент, чтобы следующий запрос переавторизовался
                if self.service.is_auth_error(e):
                    logger.warning("KworkAPI: сессия протухла, сбрасываем клиент для реавторизации")
                    self.service.reset_api()
                    api = await self._get_api()
                elif isinstance(e, KworkAPIResponseError):
                    logger.warning("KworkAPI: unexpected /projects response shape; continuing without session reset")

        logger.info(f"KworkAPI: итого {len(all_projects)} уникальных проектов по {len(queries)} запросам")
        return all_projects

    def _normalize(self, raw_projects) -> List[ProjectItem]:
        from bs4 import BeautifulSoup

        if not raw_projects:
            return []
        projects = []
        for p in raw_projects:
            project_id = str(p.id) if p.id else ""
            title = p.title or ""

            raw_desc = p.description or ""
            if raw_desc and ("<" in raw_desc or "&" in raw_desc):
                description = BeautifulSoup(raw_desc, "lxml").text.strip()
            else:
                description = raw_desc

            budget = float(p.price) if p.price else None
            url = f"{self.BASE_URL}/projects/{project_id}" if project_id else ""

            # Extract raw data for fields not exposed by WantWorker
            raw_data = {}
            if hasattr(p, "model_dump"):
                try:
                    raw_data = p.model_dump()
                except Exception:
                    raw_data = {}

            # Извлекаем навыки — WantWorker не имеет skills, пробуем raw_data
            skills = []
            if hasattr(p, "skills") and p.skills:
                for s in p.skills:
                    skill_name = s.name if hasattr(s, "name") else str(s)
                    skills.append(skill_name)
            elif raw_data.get("skills_possible") or raw_data.get("skills"):
                raw_skills = raw_data.get("skills_possible") or raw_data.get("skills") or []
                if isinstance(raw_skills, list):
                    for s in raw_skills:
                        if isinstance(s, dict):
                            skills.append(s.get("name", str(s)))
                        elif isinstance(s, str):
                            skills.append(s)

            # Извлекаем дату — WantWorker не имеет date_create, берём из raw_data
            created_at = ""
            if raw_data.get("date_create"):
                created_at = str(raw_data["date_create"])
            elif raw_data.get("date_active"):
                created_at = str(raw_data["date_active"])
            elif hasattr(p, "date_limit") and p.date_limit:
                created_at = str(p.date_limit)
            elif hasattr(p, "created_at") and p.created_at:
                created_at = str(p.created_at)

            raw_data = {}
            if hasattr(p, "model_dump"):
                raw_data = p.model_dump()
            elif hasattr(p, "__dict__"):
                raw_data = dict(p.__dict__)

            projects.append(
                ProjectItem(
                    id=project_id,
                    title=title,
                    description=description,
                    budget=budget,
                    currency="RUB",
                    skills=skills,
                    created_at=created_at,
                    url=url,
                    platform=self.PLATFORM_NAME,
                    offers_count=int(getattr(p, "offers", 0) or 0),
                    client_hired_percent=int(getattr(p, "user_hired_percent", 0) or 0),
                    client_user_id=str(getattr(p, "user_id", "") or ""),
                    platform_data={
                        "source": "kwork_mobile_api",
                        "category_id": raw_data.get("category_id") or getattr(p, "category_id", None),
                        "parent_category_id": raw_data.get("parent_category_id")
                        or getattr(p, "parent_category_id", None),
                        "category_name": getattr(p, "category_name", None) or raw_data.get("category_name"),
                        "available_durations": raw_data.get("availableDurations")
                        or raw_data.get("available_durations")
                        or [],
                        "possible_price_limit": raw_data.get("possiblePriceLimit")
                        or raw_data.get("possible_price_limit"),
                        "allow_higher_price": raw_data.get("allowHigherPrice") or raw_data.get("allow_higher_price"),
                        "already_work": raw_data.get("alreadyWork") or raw_data.get("already_work"),
                        "files": raw_data.get("files") or [],
                        "date_create": raw_data.get("date_create") or raw_data.get("dateCreate"),
                        "raw": raw_data,
                    },
                )
            )

        return projects

    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return await self._fallback_browser_details(project_id)

    async def _fallback_browser_details(self, project_id: str) -> Optional[ProjectItem]:
        from .kwork_parser import KworkParser

        logger.info(f"KworkAPI: fallback на браузерный парсер для проекта {project_id}...")
        browser_parser = KworkParser()
        try:
            return await browser_parser.get_project_details(project_id)
        except Exception as e:
            logger.error(f"KworkAPI браузерный fallback (details): {e}")
            return None

    async def _fallback_browser(self, page, per_page, filters):
        from .kwork_parser import KworkParser

        logger.info("KworkAPI: fallback на браузерный KworkParser...")
        browser_parser = KworkParser()
        try:
            return await browser_parser.get_projects(page=page, per_page=per_page, filters=filters)
        except Exception as e:
            logger.error(f"KworkAPI браузерный fallback: {e}")
            return []

    def close(self):
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.service.close())
            else:
                loop.run_until_complete(self.service.close())
        except Exception:
            pass
        super().close()
