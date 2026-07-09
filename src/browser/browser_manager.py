"""
Единый менеджер браузера Nodriver.
Singleton — один инстанс на всё приложение, постоянный профиль,
валидация авторизации, экспорт кук для curl_cffi.
Cloudflare Turnstile detection + cf_verify + cf_clearance persistence.
Browser proxy support for IP reputation management.
"""

import os
import json
import asyncio
from typing import Optional, Dict

import httpx
import nodriver as uc
from loguru import logger

from .fingerprint import Fingerprint, pick as pick_fingerprint
from src.paths import BROWSER_PROFILES_DIR, SCREENSHOTS_DIR
from src.utils.vpnte_proxy import effective_proxy_url, vpnte_proxy_enabled


class BrowserManager:
    _instance = None
    _lock = asyncio.Lock()
    _browser_lock = asyncio.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        headless: bool = True,
        profile_dir: str = str(BROWSER_PROFILES_DIR),
    ):
        # Используем классовый флаг для thread-safety
        if getattr(BrowserManager, "_globally_initialized", False):
            return
        BrowserManager._globally_initialized = True

        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.headless = headless
        self.profile_dir = os.path.abspath(profile_dir)
        self.browser: Optional[uc.Browser] = None
        self._pages: Dict[str, uc.Tab] = {}
        self._auth_validated: Dict[str, bool] = {}
        self._starting = False  # Флаг чтобы не запускать параллельно

        logger.debug(f"BrowserManager: создан инстанс id={id(self)} (Singleton)")

    async def get_browser(self) -> uc.Browser:
        # Быстрая проверка без lock
        if self.browser and not self.browser.stopped:
            return self.browser

        # Используем lock чтобы только один поток запускал браузер
        async with self._browser_lock:
            # Двойная проверка после получения lock
            if self.browser and not self.browser.stopped:
                return self.browser

            if self._starting:
                logger.debug("BrowserManager: ожидание запуска браузера другим потоком...")
                while self._starting:
                    await asyncio.sleep(0.1)
                if self.browser and not self.browser.stopped:
                    return self.browser

            self._starting = True
            logger.info(f"BrowserManager: запуск браузера (headless={self.headless})...")
            os.makedirs(self.profile_dir, exist_ok=True)

            # Детерминированный отпечаток по profile_dir → сессия стабильна,
            # но между разными профилями различается.
            fp: Fingerprint = pick_fingerprint(seed=self.profile_dir)
            self._fingerprint = fp
            logger.debug(
                f"BrowserManager: fingerprint UA={fp.user_agent[:50]}... "
                f"viewport={fp.viewport} lang={fp.accept_language.split(',')[0]} tz={fp.timezone}"
            )

            try:
                configured_proxy = os.getenv("PROXY_URL") or os.getenv("KWORK_PROXY_LIST", "").split(",")[0].strip() or None
                proxy_url = (
                    effective_proxy_url(rotate=False, fallback=configured_proxy)
                    if vpnte_proxy_enabled()
                    else configured_proxy
                )
                browser_args = fp.browser_args()
                if proxy_url:
                    browser_args.append(f"--proxy-server={proxy_url}")
                    logger.info("BrowserManager: прокси активен для браузера")

                self.browser = await uc.start(
                    headless=self.headless,
                    browser_args=browser_args,
                    user_data_dir=self.profile_dir,
                )
                logger.info("BrowserManager: браузер запущен (единый инстанс)")
                return self.browser
            except Exception as e:
                logger.error(f"BrowserManager: ошибка запуска браузера: {e}")
                self.browser = None
                raise
            finally:
                self._starting = False

    async def ensure_browser(self) -> uc.Browser:
        try:
            return await self.get_browser()
        except Exception:
            logger.warning("BrowserManager: повторная попытка запуска браузера...")
            self.browser = None
            self._auth_validated.clear()
            await asyncio.sleep(2)
            return await self.get_browser()

    async def get_page(self, url: str, reuse: bool = True) -> uc.Tab:
        browser = await self.ensure_browser()

        if reuse:
            for target in browser.targets:
                if target.type_ == "page" and url in target.url:
                    logger.debug(f"BrowserManager: переиспользуем вкладку {url}")
                    return target

        try:
            page = await browser.get(url)
            await self._check_cloudflare(page, url)
            return page
        except Exception as e:
            logger.warning(f"BrowserManager: ошибка загрузки страницы, реконнект: {e}")
            browser = await self.ensure_browser()
            page = await browser.get(url)
            await self._check_cloudflare(page, url)
            return page

    async def _check_cloudflare(self, page: uc.Tab, url: str) -> None:
        """Detect Cloudflare challenge and attempt to solve it.

        Cloudflare Turnstile / Managed Challenge detection:
        - Look for cf-challenge, turnstile iframe, "Verify you are human"
        - If detected: try tab.cf_verify() (nodriver built-in)
        - Wait for cf_clearance cookie to appear
        - Alert if challenge cannot be solved
        """
        try:
            await page.sleep(2)
            html = await page.get_content()
            html_lower = html.lower() if html else ""

            cf_markers = [
                "cf-challenge",
                "cf-turnstile",
                "cf_chl_opt",
                "just a moment",
                "checking your browser",
                "verify you are human",
                "challenge-platform",
                "cf-mitigated",
            ]

            is_cf_challenge = any(marker in html_lower for marker in cf_markers)

            if not is_cf_challenge:
                return

            logger.warning(f"BrowserManager: Cloudflare challenge detected for {url}")

            if hasattr(page, "cf_verify"):
                try:
                    logger.info("BrowserManager: attempting cf_verify()...")
                    await page.cf_verify()
                    await page.sleep(5)
                except Exception as cf_err:
                    logger.warning(f"BrowserManager: cf_verify() failed: {cf_err}")

            html_after = await page.get_content()
            if html_after and "cf-challenge" in html_after.lower():
                logger.error(f"BrowserManager: Cloudflare challenge NOT solved for {url}")
            else:
                logger.info("BrowserManager: Cloudflare challenge passed")

        except Exception as e:
            logger.debug(f"BrowserManager: Cloudflare check error: {e}")

    async def set_cookies_from_env(self, domain: str):
        """
        Fallback метод - загружает куки из переменных окружения.
        Используется только если Session Hub недоступен.
        """
        cookies_map = self._load_env_cookies(domain)
        if not cookies_map:
            logger.warning(f"BrowserManager: нет кук для {domain} в .env")
            return

        browser = await self.get_browser()
        page = await browser.get(f"https://{domain}/")
        await page.sleep(1)

        success_count = 0
        for name, value in cookies_map.items():
            if not value:
                continue
            try:
                await page.send(
                    uc.cdp.network.set_cookie(
                        name=name,
                        value=value,
                        domain=domain,
                        path="/",
                    )
                )
                success_count += 1
            except Exception as e:
                logger.debug(f"BrowserManager: ошибка установки куки {name}: {e}")

        logger.info(f"BrowserManager: ⚠️ fallback на .env: {success_count} кук для {domain}")

    async def set_cookies_from_hub(self, domain: str) -> bool:
        """
        Основной источник кук - Session Hub (127.0.0.1:8669).
        Формат ответа: {"status":"ok","count":N,"cookies":[...]}
        """
        try:
            hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(
                    f"{hub_url}?domain={domain}",
                    timeout=10,
                )
            if resp.status_code != 200:
                logger.warning(f"BrowserManager: Session Hub вернул HTTP {resp.status_code}")
                return False

            data = resp.json()

            # Проверяем статус ответа
            if data.get("status") != "ok":
                logger.warning(f"BrowserManager: Session Hub статус: {data.get('status')}")
                return False

            cookies = data.get("cookies", [])
            if not cookies:
                logger.warning("BrowserManager: Session Hub вернул пустой список кук")
                return False

            browser = await self.get_browser()
            page = await browser.get(f"https://{domain}/")
            await page.sleep(1)

            # Получаем метаданные для логирования
            first_cookie = cookies[0]
            profile_info = f"{first_cookie.get('browser', 'unknown')}/{first_cookie.get('profile_name', first_cookie.get('profile', 'unknown'))}"

            success_count = 0
            for c in cookies:
                if not c.get("name") or not c.get("value"):
                    continue
                clean_domain = c["domain"].lstrip(".")
                try:
                    await page.send(
                        uc.cdp.network.set_cookie(
                            name=c["name"],
                            value=c["value"],
                            domain=clean_domain,
                            path=c.get("path", "/"),
                            secure=c.get("secure", False),
                        )
                    )
                    success_count += 1
                except Exception as e:
                    logger.debug(f"BrowserManager: ошибка установки куки {c['name']}: {e}")

            logger.info(
                f"BrowserManager: ✅ {success_count}/{len(cookies)} кук из Session Hub ({profile_info}) для {domain}"
            )
            return True
        except Exception as e:
            logger.warning(f"BrowserManager: Session Hub недоступен ({e})")
            return False

    async def validate_auth(self, domain: str) -> bool:
        if self._auth_validated.get(domain, False):
            return True

        browser = await self.get_browser()
        page = await browser.get(f"https://{domain}/")
        await page.sleep(3)

        try:
            html = await page.get_content()
        except Exception:
            return False

        await self._check_cloudflare(page, f"https://{domain}/")
        html = await page.get_content()
        if html and "cf-challenge" in html.lower():
            logger.error(f"BrowserManager: Cloudflare challenge not solved, auth impossible for {domain}")
            self._auth_validated[domain] = False
            return False

        is_auth = self._check_auth_markers(domain, html)

        if is_auth:
            self._auth_validated[domain] = True
            logger.info(f"BrowserManager: авторизация на {domain} подтверждена")
        else:
            logger.warning(f"BrowserManager: авторизация на {domain} НЕ подтверждена!")
            self._auth_validated[domain] = False

        return is_auth

    async def init_auth(self, domain: str):
        """
        Инициализация авторизации.
        Приоритет: 1) Session Hub (основной), 2) .env (fallback)
        Thread-safe: параллельные вызовы для одного домена блокируются.
        """
        # Быстрая проверка без lock
        if self._auth_validated.get(domain, False):
            logger.debug(f"BrowserManager: auth for {domain} already validated, skipping")
            return True

        # Lock для этого домена (создаем если нет)
        lock_key = f"_auth_lock_{domain}"
        if not hasattr(self, lock_key):
            setattr(self, lock_key, asyncio.Lock())
        domain_lock = getattr(self, lock_key)

        async with domain_lock:
            # Двойная проверка после получения lock
            if self._auth_validated.get(domain, False):
                logger.debug(f"BrowserManager: auth for {domain} validated by another coroutine")
                return True

            logger.info(f"BrowserManager: начинаем init_auth для {domain}...")

            # Пробуем Session Hub (основной источник свежих кук)
            hub_ok = await self.set_cookies_from_hub(domain)

            if not hub_ok:
                # Строгий режим: отказываемся работать без Hub (куки в .env быстро протухают)
                strict = os.getenv("SESSION_HUB_REQUIRED", "false").lower() == "true"
                if strict:
                    logger.error(
                        "BrowserManager: Session Hub недоступен, а SESSION_HUB_REQUIRED=true → отказ. "
                        "Запусти Session Hub: python scripts/session_hub/session_hub_manual.py"
                    )
                    self._auth_validated[domain] = False
                    return False
                # Иначе fallback на .env
                logger.warning(f"BrowserManager: Session Hub недоступен, используем .env как fallback для {domain}")
                await self.set_cookies_from_env(domain)

            is_auth = await self.validate_auth(domain)
            if not is_auth:
                logger.error(
                    f"BrowserManager: авторизация на {domain} провалена! "
                    "Проверь что Session Hub запущен (127.0.0.1:8669) или куки в .env актуальны."
                )
            return is_auth

    async def export_cookies(self, domain: str) -> Dict[str, str]:
        browser = await self.get_browser()
        try:
            result = {}
            cookies = await browser.send(uc.cdp.network.get_cookies())
            for cookie in cookies:
                if domain in (cookie.domain or ""):
                    result[cookie.name] = cookie.value
            if "cf_clearance" in result:
                logger.info(f"BrowserManager: cf_clearance cookie present for {domain}")
            return result
            for page in browser.targets:
                if page.type_ == "page" and domain in page.url:
                    cookies_data = await page.send(uc.cdp.network.get_cookies())
                    for c in cookies_data.cookies:
                        if domain in (c.domain or ""):
                            result[c.name] = c.value
                    break
            return result
        except Exception as e:
            logger.debug(f"BrowserManager: не удалось экспортировать куки: {e}")
            return {}

    async def take_screenshot(
        self, page: uc.Tab, project_id: str, step_name: str, full_page: bool = False
    ) -> Optional[str]:
        try:
            folder = os.path.join(str(SCREENSHOTS_DIR), str(project_id))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{step_name}.png")
            if full_page:
                await page.evaluate("window.scrollTo(0, 0)")
                await page.sleep(0.5)
                await page.save_screenshot(path, full_page=True)
            else:
                await page.save_screenshot(path)
            logger.debug(f"Скриншот сохранён: {path}")
            return path
        except Exception as e:
            logger.warning(f"Не удалось сделать скриншот {step_name}: {e}")
            return None

    async def wait_for_content(
        self,
        page: uc.Tab,
        selector: str,
        timeout: int = 10,
        poll_interval: float = 0.5,
    ) -> bool:
        elapsed = 0.0
        while elapsed < timeout:
            try:
                element = await page.find(selector, timeout=1)
                if element:
                    return True
            except Exception:
                pass
            await page.sleep(poll_interval)
            elapsed += poll_interval
        return False

    async def close_page(self, page: uc.Tab):
        try:
            if self.browser and len(self.browser.targets) > 2:
                await page.close()
        except Exception:
            pass

    async def stop(self):
        if self.browser:
            try:
                self.browser.stop()
            except Exception:
                pass
            self.browser = None
            self._pages.clear()
            self._auth_validated.clear()
            logger.info("BrowserManager: браузер остановлен")

    def _check_auth_markers(self, domain: str, html: str) -> bool:
        html_lower = html.lower()
        markers = {
            "kwork.ru": [
                "мои кворки",
                "мой баланс",
                "kw-username",
                "sidebar-user-info",
                "data-userid",
            ],
            "freelance.ru": [
                "/auth/logout",
                "мой профиль",
                "мои проекты",
                "сообщения",
                "userpanel",
                "top-menu-user",
            ],
        }
        domain_markers = markers.get(domain, markers.get(domain.replace("www.", ""), []))
        return any(m in html_lower for m in domain_markers)

    def _load_env_cookies(self, domain: str) -> Dict[str, str]:
        if domain == "kwork.ru":
            return {
                "slrememberme": os.getenv("KWORK_COOKIE_REMEMBERME", ""),
                "userId": os.getenv("KWORK_COOKIE_USERID", ""),
                "PHPSESSID": os.getenv("KWORK_COOKIE_PHPSESSID", ""),
                "csrf_token": os.getenv("KWORK_CSRF_TOKEN", "") or os.getenv("KWORK_COOKIE_CSRF", ""),
            }
        elif domain in ("freelance.ru", "www.freelance.ru"):
            cookies_json = os.getenv("FREELANCE_RU_COOKIES_JSON", "").strip()
            if cookies_json:
                try:
                    parsed = json.loads(cookies_json)
                    if isinstance(parsed, dict):
                        return {str(k): str(v) for k, v in parsed.items() if v}
                except Exception as e:
                    logger.warning(f"BrowserManager: FREELANCE_RU_COOKIES_JSON не распарсен: {e}")
            return {
                "PHPSESSID": os.getenv("FREELANCE_RU_COOKIE_SESSION", ""),
                "DUID": os.getenv("FREELANCE_RU_COOKIE_DUID", ""),
                "remember": os.getenv("FREELANCE_RU_COOKIE_REMEMBER", ""),
            }
        return {}

    @classmethod
    def reset(cls):
        cls._instance = None
        cls._globally_initialized = False
