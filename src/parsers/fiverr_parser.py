"""
Парсер Fiverr (проекты из раздела Buyer Requests).
Использует web-скрапинг через curl_cffi.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
import re
from .base_parser import BaseParser, ProjectItem
from loguru import logger


class FiverrParser(BaseParser):
    PLATFORM_NAME = "fiverr"
    BASE_URL = "https://www.fiverr.com"

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        logger.info(f"Поиск buyer requests на Fiverr...")
        
        search_query = filters.get("query", "") if filters else ""
        
        # Fiverr требует авторизацию для доступа к buyer requests
        # Используем поиск по категориям как альтернативу
        try:
            response = await self.throttled_get(
                f"{self.BASE_URL}/search/gigs",
                params={
                    "query": search_query or "python",
                    "page": page,
                },
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Ошибка парсинга Fiverr: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html: str) -> List[ProjectItem]:
        """Парсинг HTML страницы поиска Fiverr."""
        soup = BeautifulSoup(html, "lxml")
        projects = []
        
        # Fiverr использует разные классы, пробуем общие селекторы
        gig_cards = soup.find_all("div", class_=re.compile(r"gig-card|gig-wrapper"))
        
        if not gig_cards:
            # Альтернативный поиск
            gig_cards = soup.find_all("article") or soup.find_all("div", class_=re.compile(r"seller-gig"))
        
        for card in gig_cards[:20]:
            try:
                # Заголовок
                title_el = card.find("h3") or card.find("a", class_=re.compile(r"gig-link"))
                if not title_el:
                    continue
                
                title = title_el.get_text(strip=True)
                
                # Ссылка
                link = ""
                if title_el.name == "a":
                    link = title_el.get("href", "")
                else:
                    a_tag = card.find("a")
                    if a_tag:
                        link = a_tag.get("href", "")
                
                if link and not link.startswith("http"):
                    link = f"{self.BASE_URL}{link}"
                
                # ID из ссылки
                project_id = ""
                if link:
                    id_match = re.search(r'/([^/]+)$', link)
                    if id_match:
                        project_id = id_match.group(1)
                
                # Цена
                budget = None
                price_el = card.find("span", class_=re.compile(r"price|amount"))
                if price_el:
                    price_text = price_el.get_text(strip=True)
                    price_match = re.search(r'(\d+)', price_text.replace(",", ""))
                    if price_match:
                        budget = float(price_match.group(1))
                
                # Описание (обычно обрезанное в карточке)
                desc_el = card.find("p", class_=re.compile(r"description"))
                description = ""
                if desc_el:
                    description = desc_el.get_text(strip=True)
                
                if title and project_id:
                    projects.append(ProjectItem(
                        id=project_id,
                        title=title,
                        description=description or title,
                        budget=budget,
                        currency="USD",
                        skills=[],
                        url=link,
                        platform=self.PLATFORM_NAME,
                        created_at="",
                    ))
                    
            except Exception as e:
                logger.debug(f"Ошибка парсинга карточки Fiverr: {e}")
                continue
        
        logger.info(f"Fiverr: спарсено {len(projects)} проектов")
        return projects
