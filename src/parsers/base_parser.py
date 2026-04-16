"""
Базовый класс для всех парсеров фриланс-платформ.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from src.evolution import TLSClient
from loguru import logger
from pydantic import BaseModel
from tenacity import retry, wait_exponential, stop_after_attempt


class ProjectItem(BaseModel):
    id: str
    title: str
    description: str
    budget: Optional[float] = None
    currency: str = "RUB"
    skills: List[str] = []
    url: str
    platform: str
    created_at: str
    client_id: Optional[str] = None


class RateLimiter:
    def __init__(self, min_interval: float = 2.0):
        self.min_interval = min_interval
        self._last_request: float = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self.min_interval:
                delay = self.min_interval - elapsed
                logger.debug(f"RateLimiter: ждём {delay:.1f}с")
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()


class BaseParser(ABC):
    PLATFORM_NAME = "base"
    BASE_URL = ""
    API_URL = ""
    RATE_LIMIT_SECONDS: float = 2.0

    def __init__(
        self,
        proxy: Optional[str] = None,
        browser_preset: str = "chrome120",
    ):
        self.proxy = proxy
        self.client = TLSClient(
            browser=browser_preset,
            proxy=proxy,
        )
        self.headers = self._default_headers()
        self.auth_token = None
        self.csrf_token = None
        self.rate_limiter = RateLimiter(min_interval=self.RATE_LIMIT_SECONDS)

        logger.info(f"{self.PLATFORM_NAME} парсер инициализирован (rate_limit={self.RATE_LIMIT_SECONDS}с)")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _safe_get(self, url: str, **kwargs):
        if 'headers' not in kwargs:
            kwargs['headers'] = self.headers

        try:
            response = self.client.get(url, **kwargs)
        except Exception as e:
            logger.warning(f"{self.PLATFORM_NAME} ошибка сети к {url}: {e}")
            raise

        if response.status_code in [403, 429, 503]:
            logger.warning(f"{self.PLATFORM_NAME} получил HTTP {response.status_code}, ретрай...")
            raise Exception(f"HTTP {response.status_code}")

        return response

    async def throttled_get(self, url: str, **kwargs):
        await self.rate_limiter.wait()
        return self._safe_get(url, **kwargs)

    @abstractmethod
    async def get_projects(
        self,
        page: int = 1,
        per_page: int = 20,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ProjectItem]:
        """Получить список проектов/заказов."""
        pass
    
    @abstractmethod
    async def get_project_details(self, project_id: str) -> Optional[ProjectItem]:
        """Получить детали проекта."""
        pass
    
    def authenticate(self, token: str, token_type: str = "bearer"):
        """Установить токен авторизации."""
        if token_type == "bearer":
            self.headers["Authorization"] = f"Bearer {token}"
        elif token_type == "jwt":
            self.headers["Authorization"] = f"Bearer {token}"
        elif token_type == "cookie":
            self.client.set_cookies({"session": token})
        
        self.auth_token = token
        logger.info(f"Авторизация установлена для {self.PLATFORM_NAME}")
    
    def set_csrf_token(self, token: str):
        """Установить CSRF-токен."""
        self.csrf_token = token
        self.headers["X-CSRF-Token"] = token
        self.headers["X-Requested-With"] = "XMLHttpRequest"
    
    def update_proxy(self, proxy: str):
        """Обновить прокси (для IPv6 ротации)."""
        self.client.update_proxy(proxy)
        logger.debug(f"Прокси обновлён для {self.PLATFORM_NAME}: {proxy}")
    
    def _default_headers(self) -> Dict[str, str]:
        """Заголовки по умолчанию."""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    
    def close(self):
        """Закрыть сессию."""
        self.client.close()
        logger.info(f"{self.PLATFORM_NAME} парсер закрыт")
