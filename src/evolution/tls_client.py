"""
Модуль для обхода WAF через подмену TLS/HTTP/2 фингерпринтов.
Использует curl_cffi для эмуляции реальных браузеров.
"""

from curl_cffi import requests as curl_requests
from typing import Optional, Dict, Any
from loguru import logger


class TLSClient:
    """
    HTTP-клиент с подменой TLS-отпечатков (JA3/JA4) и HTTP/2 фингерпринтов.
    Эмулирует Chrome 120 для обхода Cloudflare, DataDome, PerimeterX.
    """

    # Пресеты браузеров для curl_cffi (динамически определяем доступные)
    BROWSER_PRESETS = {}
    _available_browsers = [
        "chrome142",
        "chrome136",
        "chrome133a",
        "chrome131",
        "chrome124",
        "chrome123",
        "chrome120",
        "chrome119",
        "chrome116",
        "chrome110",
        "chrome107",
        "chrome104",
        "chrome101",
        "chrome100",
        "chrome99",
        "firefox144",
        "firefox135",
        "firefox133",
        "firefox124",
        "firefox120",
        "safari18_0",
        "safari17_0",
        "safari15_5",
        "safari153",
        "edge101",
        "edge99",
    ]

    for _name in _available_browsers:
        BROWSER_PRESETS[_name] = _name

    # Fallback если ни один браузер не найден
    if not BROWSER_PRESETS:
        BROWSER_PRESETS = {"default": "chrome120"}

    def __init__(
        self,
        browser: str = "chrome120",
        proxy: Optional[str] = None,
        timeout: int = 30,
        impersonate: bool = True,
    ):
        # Нормализуем имя (chrome_120 → chrome120)
        browser_normalized = browser.replace("_", "")
        if browser_normalized not in self.BROWSER_PRESETS:
            logger.warning(f"Неизвестный браузер: {browser}, используем chrome120")
            browser_normalized = "chrome120"

        self.browser_name = browser_normalized
        self.proxy = proxy
        self.timeout = timeout
        self.impersonate = impersonate

        # Создаём сессию с подменой отпечатков
        self.session = curl_requests.Session(
            impersonate=browser_normalized,
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=timeout,
            verify=True,
        )

        logger.info(f"TLSClient инициализирован: {browser_normalized}, прокси: {proxy or 'нет'}")

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        allow_redirects: bool = True,
        **kwargs,
    ) -> curl_requests.Response:
        """GET-запрос с подменой TLS-отпечатка."""
        try:
            response = self.session.get(
                url,
                headers=headers or self._default_headers(),
                params=params,
                allow_redirects=allow_redirects,
                **kwargs,
            )
            logger.debug(f"GET {url} → {response.status_code}")
            return response
        except Exception as e:
            logger.error(f"Ошибка GET {url}: {e}")
            raise

    def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        **kwargs,
    ) -> curl_requests.Response:
        """POST-запрос с подменой TLS-отпечатка."""
        try:
            response = self.session.post(
                url, headers=headers or self._default_headers(), json=json, data=data, **kwargs
            )
            logger.debug(f"POST {url} → {response.status_code}")
            return response
        except Exception as e:
            logger.error(f"Ошибка POST {url}: {e}")
            raise

    def graphql_query(
        self,
        url: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> curl_requests.Response:
        """Отправка GraphQL-запроса (для Upwork и др.)."""
        payload = {
            "query": query,
            "variables": variables or {},
        }

        graphql_headers = self._default_headers()
        graphql_headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        if headers:
            graphql_headers.update(headers)

        return self.post(url, json=payload, headers=graphql_headers)

    def update_proxy(self, proxy: str):
        """Обновить прокси (для IPv6 ротации)."""
        self.proxy = proxy
        self.session.proxies = {"http": proxy, "https": proxy}
        logger.debug(f"Прокси обновлён: {proxy}")

    def get_cookies(self) -> Dict[str, str]:
        """Получить cookies сессии."""
        return dict(self.session.cookies)

    def set_cookies(self, cookies: Dict[str, str]):
        """Установить cookies."""
        for name, value in cookies.items():
            self.session.cookies.set(name, value)

    def _default_headers(self) -> Dict[str, str]:
        """Заголовки по умолчанию (Chrome 120)."""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def close(self):
        """Закрыть сессию."""
        self.session.close()
        logger.info("Сессия TLSClient закрыта")
