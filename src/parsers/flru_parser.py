"""
Парсер FL.ru.
Использует парсинг HTML страницы поиска проектов через curl_cffi.
"""

from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from .base_parser import BaseParser, ProjectItem
from loguru import logger
import re


class FLRuParser(BaseParser):
    """Парсер для FL.ru."""
    
    PLATFORM_NAME = "fl_ru"
    BASE_URL = "https://www.fl.ru"
    API_URL = "https://www.fl.ru/projects/"
    
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Парсинг списка заказов через HTML."""
        logger.info(f"Поиск вакансий на FL.ru (страница {page})...")
        
        # 5 = Разработка сайтов, 33 = Программирование
        params = {
            "kind": 5, 
            "category": 33,
            "page": page,
        }
        
        try:
            response = self._safe_get(
                self.API_URL,
                params=params,
            )
            
            return self._parse_html(response.text)[:per_page]
            
        except Exception as e:
            logger.error(f"Исключение при парсинге FL.ru: {e}")
            return []
    
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        return None
    
    def _parse_html(self, html_text: str) -> List[ProjectItem]:
        """Парсинг HTML списка проектов. На FL.ru это class 'b-post'."""
        soup = BeautifulSoup(html_text, "lxml")
        posts = soup.find_all("div", class_=re.compile(r"b-post\b"))
        
        normalized = []
        for post in posts:
            title_el = post.find("h2", class_="b-post__title")
            if not title_el:
                continue
                
            a_tag = title_el.find("a")
            if not a_tag:
                continue
                
            title = a_tag.text.strip()
            link = f"{self.BASE_URL}{a_tag['href']}" if not a_tag['href'].startswith('http') else a_tag['href']
            
            # ID
            project_id = ""
            id_match = re.search(r'/projects/(\d+)/', link)
            if id_match:
                project_id = id_match.group(1)
            
            # Budget
            budget = None
            currency = "RUB"
            price_el = post.find("div", class_="b-post__price")
            if price_el:
                price_text = price_el.text.strip().replace(" ", "").replace("\xa0", "")
                if price_text and price_text.lower() != "подоговоренности":
                    num_match = re.search(r'(\d+)', price_text)
                    if num_match:
                        budget = float(num_match.group(1))
                    if "₽" in price_text or "руб" in price_text.lower():
                        currency = "RUB"
                    elif "$" in price_text or "usd" in price_text.lower():
                        currency = "USD"
            
            # Description
            desc_el = post.find("div", class_="b-post__txt")
            desc = desc_el.text.strip() if desc_el else ""
            
            normalized.append(ProjectItem(
                id=project_id,
                title=title,
                description=desc,
                budget=budget,
                currency=currency,
                skills=[], # HTML list view doesn't explicitly provide tags reliably
                created_at="", 
                url=link,
                platform=self.PLATFORM_NAME,
            ))
            
        return normalized
