"""
Парсер Kwork.ru.
Использует Nodriver (невидимый браузер) для обхода JS-рендеринга.
Fallback на curl_cffi если Nodriver недоступен.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re
import asyncio
import os


class KworkParser(BaseParser):
    """Парсер для Kwork через Nodriver."""
    
    PLATFORM_NAME = "kwork"
    BASE_URL = "https://kwork.ru"
    PROJECTS_URL = "https://kwork.ru/projects?c=all&attr=211"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг заказов Kwork через Nodriver (браузер)."""
        logger.info(f"Поиск заказов на Kwork (страница {page})...")
        
        url = f"{self.PROJECTS_URL}&page={page}"
        
        try:
            html = await self._fetch_with_nodriver(url)
            if html:
                projects = self._parse_html(html)[:per_page]
                logger.info(f"Kwork Nodriver: получено {len(projects)} заказов")
                return projects
            else:
                logger.warning("Kwork Nodriver: пустой HTML, пробуем curl_cffi...")
                return await self._fallback_curl(page, per_page)
                
        except Exception as e:
            logger.error(f"Kwork Nodriver ошибка: {e}, пробуем curl_cffi...")
            return await self._fallback_curl(page, per_page)
    
    async def _fetch_with_nodriver(self, url: str) -> Optional[str]:
        """Загрузка страницы через невидимый браузер."""
        import nodriver as uc
        
        browser = None
        try:
            browser = await uc.start(headless=True)
            page = await browser.get("about:blank")
            
            # Подкладываем куки авторизации (если есть)
            remember = os.getenv("KWORK_COOKIE_REMEMBERME", "")
            user_id = os.getenv("KWORK_COOKIE_USERID", "")
            
            if remember and user_id:
                await page.send(uc.cdp.network.set_cookie(
                    name="slrememberme", value=remember,
                    domain="kwork.ru", path="/"
                ))
                await page.send(uc.cdp.network.set_cookie(
                    name="userId", value=user_id,
                    domain="kwork.ru", path="/"
                ))
            
            # Загружаем страницу и ждём рендеринг JS
            await page.get(url)
            await asyncio.sleep(4)  # Ждём пока JS подгрузит карточки
            
            # Скроллим вниз чтобы подгрузить lazy-load карточки
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await asyncio.sleep(2)
            
            html = await page.get_content()
            await page.close()
            return html
            
        except Exception as e:
            logger.error(f"Nodriver fetch error: {e}")
            return None
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass
    
    async def _fallback_curl(self, page: int, per_page: int) -> List[ProjectItem]:
        """Запасной вариант через curl_cffi (может не работать)."""
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
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        """Парсинг div.want-card (актуальная вёрстка Kwork 2026)."""
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("div.want-card")
        
        normalized = []
        for card in cards:
            # Заголовок: ссылка на /projects/ID
            a_tag = card.find("a", href=lambda h: h and "/projects/" in h)
            if not a_tag:
                continue
                
            title = a_tag.text.strip()
            link = a_tag.get("href", "")
            
            # Полный URL
            if link and not link.startswith("http"):
                link = f"{self.BASE_URL}{link}"
            
            # ID
            project_id = ""
            id_match = re.search(r'/projects/(\d+)', link)
            if id_match:
                project_id = id_match.group(1)
                
            # Бюджет: ищем текст с ₽ или числа в price-блоках
            budget = None
            price_el = card.find("div", class_="wants-card__header-price")
            if not price_el:
                # Фоллбэк: ищем любой элемент с ₽
                price_el = card.find(string=re.compile(r'\d.*[₽руб]'))
                if price_el:
                    price_el = price_el.parent
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                num_match = re.search(r'(\d+)', price_text)
                if num_match:
                    budget = float(num_match.group(1))
            
            # Описание
            desc_el = card.find("div", class_="wants-card__description-text")
            if not desc_el:
                desc_el = card.find("div", class_=lambda c: c and "description" in " ".join(c) if isinstance(c, list) else c and "description" in c)
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
