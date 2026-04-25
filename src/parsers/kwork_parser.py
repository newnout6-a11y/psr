"""
Парсер Kwork.ru.
Использует BrowserManager (единый Nodriver инстанс) с постоянным профилем.
Fallback на curl_cffi если браузер недоступен.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from src.browser.browser_manager import BrowserManager
from src.platforms.kwork import KworkStateDataParser
from loguru import logger
import re
import os


class KworkParser(BaseParser):
    PLATFORM_NAME = "kwork"
    BASE_URL = "https://kwork.ru"
    PROJECTS_URL = "https://kwork.ru/projects?c=all&attr=211"

    def __init__(self, proxy=None, browser_preset="chrome120"):
        super().__init__(proxy=proxy, browser_preset=browser_preset)
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
        self.browser_mgr = BrowserManager(headless=headless)
        self._auth_initialized = False

    async def _ensure_auth(self):
        if self._auth_initialized:
            return
        await self.browser_mgr.init_auth("kwork.ru")
        self._auth_initialized = True

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        logger.info(f"Поиск заказов на Kwork (страница {page})...")

        try:
            await self._ensure_auth()
            html = await self._fetch_with_browser(page)
            if html:
                projects = self._parse_html(html)[:per_page]
                logger.info(f"Kwork: получено {len(projects)} заказов через браузер")
                return projects
        except Exception as e:
            logger.error(f"Kwork браузер ошибка: {e}")

        logger.warning("Kwork: fallback на curl_cffi...")
        return await self._fallback_curl(page, per_page)

    async def _fetch_with_browser(self, page: int) -> Optional[str]:
        url = f"{self.PROJECTS_URL}&page={page}"
        mgr = self.browser_mgr

        browser = await mgr.get_browser()
        page_tab = await browser.get(url)

        try:
            loaded = await mgr.wait_for_content(
                page_tab, "div.want-card", timeout=10
            )
            if not loaded:
                await page_tab.sleep(4)

            try:
                await page_tab.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight / 2)"
                )
            except Exception:
                pass
            await page_tab.sleep(2)

            html = await page_tab.get_content()
            return html
        except Exception as e:
            logger.error(f"Kwork: ошибка загрузки страницы: {e}")
            return None
        finally:
            await mgr.close_page(page_tab)

    async def _fallback_curl(self, page: int, per_page: int) -> List[ProjectItem]:
        params = {"c": "all", "attr": 211, "page": page}
        try:
            response = self._safe_get(
                f"{self.BASE_URL}/projects",
                params=params,
            )
            projects = self._parse_html(response.text)[:per_page]
            logger.info(f"Kwork curl_cffi fallback: получено {len(projects)} заказов")
            return projects
        except Exception as e:
            logger.error(f"Kwork curl_cffi fallback тоже упал: {e}")
            return []

    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        url = f"{self.BASE_URL}/projects/{project_id}"
        try:
            await self._ensure_auth()
            mgr = self.browser_mgr
            page_tab = await mgr.get_page(url, reuse=True)

            await mgr.wait_for_content(page_tab, "body", timeout=10)
            await page_tab.sleep(2)
            html = await page_tab.get_content()

            state_project = KworkStateDataParser.project_from_html(html, project_id=project_id)
            if state_project:
                await mgr.close_page(page_tab)
                return state_project

            soup = BeautifulSoup(html, "lxml")

            desc_el = soup.find("div", class_="project-description")
            description = desc_el.text.strip() if desc_el else ""

            title_el = soup.find("h1") or soup.find("div", class_="wants-page__title")
            title = title_el.text.strip() if title_el else ""

            skills = []
            skill_els = soup.find_all("a", class_="kw-tag--skills")
            for s in skill_els:
                skills.append(s.text.strip())

            budget = None
            price_el = soup.find("div", class_="wants-card__header-price") or soup.find("div", class_="wants-card__price")
            if not price_el:
                price_el = soup.find(string=re.compile(r'\d.*[₽руб]'))
                if price_el:
                    price_el = price_el.parent
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                num_match = re.search(r'(\d+)', price_text)
                if num_match:
                    budget = float(num_match.group(1))

            await mgr.close_page(page_tab)

            return ProjectItem(
                id=project_id,
                title=title,
                description=description,
                budget=budget,
                currency="RUB",
                skills=skills,
                created_at="",
                url=url,
                platform=self.PLATFORM_NAME,
            )
        except Exception as e:
            logger.error(f"Kwork get_project_details ошибка: {e}")
            return None

    def close(self):
        """Закрыть сессию и остановить браузер."""
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.browser_mgr.stop())
            else:
                loop.run_until_complete(self.browser_mgr.stop())
        except Exception:
            pass
        super().close()

    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        state_projects = KworkStateDataParser.projects_from_html(html_text)
        if state_projects:
            return state_projects

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("div.want-card")

        normalized = []
        for card in cards:
            a_tag = card.find("a", href=lambda h: h and "/projects/" in h)
            if not a_tag:
                continue

            title = a_tag.text.strip()
            link = a_tag.get("href", "")

            if link and not link.startswith("http"):
                link = f"{self.BASE_URL}{link}"

            project_id = ""
            id_match = re.search(r'/projects/(\d+)', link)
            if id_match:
                project_id = id_match.group(1)

            budget = None
            price_el = card.find("div", class_="wants-card__header-price") or card.find("div", class_="wants-card__price")
            if not price_el:
                price_el = card.find(string=re.compile(r'\d.*[₽руб]'))
                if price_el:
                    price_el = price_el.parent
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                num_match = re.search(r'(\d+)', price_text)
                if num_match:
                    budget = float(num_match.group(1))

            desc_el = card.find("div", class_="wants-card__description-text")
            if not desc_el:
                desc_el = card.find("div", class_=lambda c: c and "description" in c if isinstance(c, str) else False)
            desc = desc_el.text.strip() if desc_el else ""

            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description=desc,
                budget=budget,
                currency="RUB",
                skills=[],
                created_at="",
                url=link,
                platform=self.PLATFORM_NAME,
            ))

        return normalized
