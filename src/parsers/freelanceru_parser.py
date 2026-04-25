"""
Парсер Freelance.ru.
Использует HTML парсинг списка проектов.
"""

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from loguru import logger

from .base_parser import BaseParser, ProjectItem


class FreelanceRuParser(BaseParser):
    """Парсер для Freelance.ru."""

    PLATFORM_NAME = "freelance_ru"
    BASE_URL = "https://freelance.ru"
    API_URL = "https://freelance.ru/projects/"

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на Freelance.ru (страница {page})...")

        filters = filters or {}
        search_query = (filters.get("query") or "").strip()

        params = {
            "spec": 4,
            "page": page,
        }
        if search_query:
            params["keyword"] = search_query

        try:
            response = await self.throttled_get(self.API_URL, params=params)
            return self._parse_html(response.text)[:per_page]
        except Exception as e:
            logger.error(f"Исключение при парсинге Freelance.ru: {e}")
            return []

    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None

    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        """Парсинг карточек проектов."""
        soup = BeautifulSoup(html_text, "lxml")
        items = soup.find_all("div", class_="project-item-default-card")
        if not items:
            return []

        normalized: List[ProjectItem] = []
        for item in items:
            title_el = item.find("h2", class_="title")
            if not title_el:
                continue

            a_tag = title_el.find("a")
            if not a_tag:
                continue

            title = a_tag.text.strip()
            href = a_tag.get("href", "")
            link = f"{self.BASE_URL}{href}" if href.startswith("/") else href

            project_id = (item.get("data-project-id") or "").strip()
            if not project_id:
                id_match = re.search(r"-(\d+)\.html(?:$|[?#])", link)
                if id_match:
                    project_id = id_match.group(1)

            budget = None
            price_el = item.find("div", class_="cost") or item.find("span", class_="cost")
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                num_match = re.search(r"(\d+)", price_text)
                if num_match:
                    budget = float(num_match.group(1))

            desc_el = item.find("a", class_="description") or item.find("div", class_="description")
            desc = desc_el.text.strip() if desc_el else ""

            skills = []
            spec_el = item.find("div", class_="specs-list")
            if spec_el:
                skills = [s.strip() for s in spec_el.stripped_strings if s.strip()]

            created_at = ""
            publish_el = item.find("div", class_="publish-time")
            if publish_el:
                created_at = " ".join(publish_el.stripped_strings)

            normalized.append(
                ProjectItem(
                    id=project_id or link.rsplit("/", 1)[-1],
                    title=title,
                    description=desc,
                    budget=budget,
                    currency="RUB",
                    skills=skills,
                    created_at=created_at,
                    url=link,
                    platform=self.PLATFORM_NAME,
                )
            )

        return normalized
