"""
Парсер Kwork.ru через библиотеку kwork (kesha1225/kwork).
Использует мобильный API (api.kwork.ru) — авторизация через email/password.
Быстрый, надёжный, без браузера.
"""

from typing import Optional, Dict, Any, List
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import os


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
        self._api = None

    def _get_api(self):
        if self._api is not None:
            return self._api

        from kwork import Kwork

        email = os.getenv("KWORK_EMAIL")
        password = os.getenv("KWORK_PASSWORD")
        proxy_url = os.getenv("PROXY_URL") or None

        if not email or not password:
            logger.error("KworkAPI: нет KWORK_EMAIL/KWORK_PASSWORD в .env! Добавь и перезапусти.")
            return None

        self._api = Kwork(
            login=email,
            password=password,
            timeout=30.0,
            retry_max_attempts=2,
            proxy=proxy_url,
        )
        logger.info("KworkAPI: клиент kwork инициализирован")
        return self._api

    def _resolve_categories(self, query: str) -> list:
        """Разрешает категории из поискового запроса.
        Поддерживает составные запросы: 'python бот' → [11] + [11] = [11]
        'сайт api' → [79] + [11] = [11, 79]
        """
        query_lower = query.lower().strip()
        words = query_lower.replace(",", " ").split()

        all_cats = set()
        for word in words:
            for key, cats in KWORK_CATEGORIES_MAP.items():
                if key == "all":
                    continue
                if key in word or word in key:
                    all_cats.update(cats)

        # Если ничего не нашли — дефолт на скрипты (11)
        return sorted(all_cats) if all_cats else [11]

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        api = self._get_api()
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

        for query in queries:
            categories_ids = self._resolve_categories(query)

            try:
                raw_projects = await api.get_projects(
                    categories_ids=categories_ids,
                    page=page,
                    query=query,
                    price_from=500,
                )

                for p in self._normalize(raw_projects):
                    if p.id not in seen_ids:
                        seen_ids.add(p.id)
                        all_projects.append(p)

                logger.info(f"KworkAPI: запрос='{query}', кат.={categories_ids}, получено {len(raw_projects)} проектов (стр.{page})")
            except Exception as e:
                logger.error(f"KworkAPI: ошибка для запроса '{query}': {e}")

        logger.info(f"KworkAPI: итого {len(all_projects)} уникальных проектов по {len(queries)} запросам")
        return all_projects[:per_page]

    def _normalize(self, raw_projects) -> List[ProjectItem]:
        from bs4 import BeautifulSoup

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

            # Извлекаем навыки
            skills = []
            if hasattr(p, "skills") and p.skills:
                for s in p.skills:
                    skill_name = s.name if hasattr(s, "name") else str(s)
                    skills.append(skill_name)
            elif hasattr(p, "category_name") and p.category_name:
                skills.append(p.category_name)

            # Извлекаем дату
            created_at = ""
            if hasattr(p, "date_limit") and p.date_limit:
                created_at = str(p.date_limit)
            elif hasattr(p, "created_at") and p.created_at:
                created_at = str(p.created_at)

            projects.append(ProjectItem(
                id=project_id,
                title=title,
                description=description,
                budget=budget,
                currency="RUB",
                skills=skills,
                created_at=created_at,
                url=url,
                platform=self.PLATFORM_NAME,
            ))

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
        if self._api:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._api.close())
                else:
                    loop.run_until_complete(self._api.close())
            except Exception:
                pass
            self._api = None
        super().close()
