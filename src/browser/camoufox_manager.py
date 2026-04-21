"""
Альтернативный менеджер браузера на базе Camoufox.
Camoufox - это форк Firefox с улучшенной защитой от детекции.
"""

import os
import asyncio
from typing import Optional, Dict, Any
from loguru import logger
from src.paths import BROWSER_PROFILES_DIR, SCREENSHOTS_DIR


class CamoufoxManager:
    """
    Менеджер браузера Camoufox (альтернатива Nodriver).
    Использует playwright с camoufox для обхода анти-детекции.
    """

    def __init__(
        self,
        headless: bool = True,
        profile_dir: str = str(BROWSER_PROFILES_DIR / "camoufox"),
    ):
        self.headless = headless
        self.profile_dir = os.path.abspath(profile_dir)
        self.browser: Optional[Any] = None
        self.context: Optional[Any] = None
        self._initialized = False

    async def start(self):
        """Запуск Camoufox браузера."""
        try:
            from playwright.async_api import async_playwright

            logger.info(f"Camoufox: запуск браузера (headless={self.headless})...")
            os.makedirs(self.profile_dir, exist_ok=True)

            self._playwright = await async_playwright().start()

            # Запускаем camoufox через playwright
            self.browser = await self._playwright.firefox.launch_persistent_context(
                user_data_dir=self.profile_dir,
                headless=self.headless,
                args=[
                    "--width=1920",
                    "--height=1080",
                ],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
                bypass_csp=True,
                ignore_https_errors=True,
            )

            self._initialized = True
            logger.info("Camoufox: браузер запущен")

        except ImportError:
            logger.error("Camoufox: playwright не установлен. Установите: pip install playwright")
            raise
        except Exception as e:
            logger.error(f"Camoufox: ошибка запуска: {e}")
            raise

    async def get_page(self, url: str, reuse: bool = True):
        """Получить или создать страницу."""
        if not self._initialized:
            await self.start()

        # В playwright context управляет страницами
        pages = self.browser.pages

        if reuse and pages:
            for page in pages:
                if url in page.url:
                    logger.debug(f"Camoufox: переиспользуем страницу {url}")
                    return page

        page = await self.browser.new_page()
        await page.goto(url, wait_until="networkidle")
        return page

    async def set_cookies(self, domain: str, cookies: Dict[str, str]):
        """Установить cookies для домена."""
        if not self._initialized:
            await self.start()

        formatted_cookies = []
        for name, value in cookies.items():
            formatted_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                }
            )

        await self.browser.add_cookies(formatted_cookies)
        logger.info(f"Camoufox: установлено {len(cookies)} cookies для {domain}")

    async def take_screenshot(self, page, project_id: str, step_name: str) -> Optional[str]:
        """Сделать скриншот страницы."""
        try:
            import os

            folder = os.path.join(str(SCREENSHOTS_DIR), str(project_id))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{step_name}.png")
            await page.screenshot(path=path, full_page=True)
            logger.debug(f"Camoufox: скриншот сохранен: {path}")
            return path
        except Exception as e:
            logger.warning(f"Camoufox: ошибка скриншота: {e}")
            return None

    async def close(self):
        """Закрыть браузер."""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if hasattr(self, "_playwright"):
            await self._playwright.stop()
            self._playwright = None
        self._initialized = False
        logger.info("Camoufox: браузер остановлен")


# Фабрика для выбора менеджера браузера
def get_browser_manager(headless: bool = True, use_camoufox: bool = False):
    """Получить менеджер браузера (nodriver или camoufox)."""
    if use_camoufox:
        return CamoufoxManager(headless=headless)

    # Импортируем и возвращаем стандартный BrowserManager
    from src.browser.browser_manager import BrowserManager

    return BrowserManager(headless=headless)
