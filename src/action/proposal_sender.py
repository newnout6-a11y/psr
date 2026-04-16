"""
Отправка откликов.
Kwork — через библиотеку kwork (мобильный API + web-сессия).
FL.ru — через BrowserManager (Nodriver).
Freelancer.com — через REST API.
"""

import os
import random
import json
import shutil
from typing import Dict, Any, Optional
import httpx
from loguru import logger
from src.browser.browser_manager import BrowserManager


class ProposalSender:
    def __init__(self, headless: bool = True, timeout: int = 30):
        self.timeout = timeout
        self.browser_mgr = BrowserManager(headless=headless)
        self._kwork_api = None
        self._kwork_web_logged_in = False

    async def start(self):
        pass

    async def stop(self):
        await self.browser_mgr.stop()
        if self._kwork_api:
            try:
                await self._kwork_api.close()
            except Exception:
                pass

    def _cleanup_screenshots(self, project_id: str):
        """Удалить папку со скриншотами проекта после отправки."""
        screenshot_dir = os.path.join("data", "screenshots", project_id)
        if os.path.exists(screenshot_dir):
            try:
                shutil.rmtree(screenshot_dir)
                logger.debug(f"Скриншоты удалены: {screenshot_dir}")
            except Exception as e:
                logger.debug(f"Не удалось удалить скриншоты: {e}")

    def _get_kwork_api(self):
        if self._kwork_api is not None:
            return self._kwork_api

        from kwork import Kwork

        email = os.getenv("KWORK_EMAIL")
        password = os.getenv("KWORK_PASSWORD")
        proxy_url = os.getenv("PROXY_URL") or None

        if not email or not password:
            logger.warning("ProposalSender: нет KWORK_EMAIL/KWORK_PASSWORD — отправка через API недоступна")
            return None

        self._kwork_api = Kwork(
            login=email,
            password=password,
            timeout=30.0,
            retry_max_attempts=2,
            proxy=proxy_url,
        )
        return self._kwork_api

    async def _ensure_auth(self, domain: str):
        if not self.browser_mgr._auth_validated.get(domain, False):
            await self.browser_mgr.init_auth(domain)

    async def send_kwork_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
        dry_run: bool = False,
    ) -> bool:
        logger.info(f"Kwork: отправка отклика на проект {project_id}")

        # Минимальная цена на Kwork — 500 руб
        numeric_price = 500
        if price:
            try:
                numeric_price = int(float(price))
                if numeric_price < 500:
                    logger.warning(f"Kwork: цена {numeric_price} ниже минимума. Устанавливаю 500.")
                    numeric_price = 500
            except Exception:
                numeric_price = 500

        # Dry-run: только браузерная демонстрация без реальной отправки
        if dry_run:
            logger.info("Kwork: [DRY-RUN] браузерная демонстрация без отправки")
            return await self._send_kwork_browser(
                project_url, project_id, proposal_text, str(numeric_price), dry_run=True
            )

        # Шаг 1: Попытка через API
        api = self._get_kwork_api()
        if api:
            try:
                if not self._kwork_web_logged_in:
                    await api.web_login(url_to_redirect="/")
                    self._kwork_web_logged_in = True
                    logger.info("Kwork: web-сессия установлена через мобильный API")

                result = await api.web.submit_exchange_offer(
                    project_id=int(project_id),
                    offer_type="custom",
                    description=proposal_text,
                    kwork_duration=7,
                    kwork_price=numeric_price,
                    kwork_name="Разработка",
                )

                json_resp = result.get("json", {})
                if json_resp.get("success"):
                    logger.info(f"Kwork: отклик отправлен через API на проект {project_id}")
                    return True
                else:
                    error_msg = json_resp.get("message", "неизвестная ошибка")
                    logger.error(f"Kwork: отклик отклонён API: {error_msg}")

            except Exception as e:
                logger.error(f"Kwork: ошибка API-отправки: {e}")

        # Шаг 2: Fallback на браузер
        logger.warning("Kwork: API недоступен или упал, fallback на браузер")
        return await self._send_kwork_browser(
            project_url, project_id, proposal_text, str(numeric_price), dry_run=False
        )

    async def _send_kwork_browser(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: str,
        dry_run: bool = False,
    ) -> bool:
        logger.info(f"Kwork: отправка через браузер на {project_url}")

        await self._ensure_auth("kwork.ru")
        mgr = self.browser_mgr

        if not mgr._auth_validated.get("kwork.ru", False):
            logger.warning("Kwork: браузерная авторизация не подтверждена!")
            return False

        page = await mgr.get_page(project_url, reuse=True)

        try:
            await page.scroll_down(300)
            await page.sleep(2)
            await mgr.take_screenshot(page, project_id, "01_project_page")

            respond_btn = await page.find("Предложить услугу", timeout=5)
            if not respond_btn:
                respond_btn = await page.find("Откликнуться", timeout=5)

            if not respond_btn:
                logger.warning("Kwork: кнопка отклика не найдена")
                return False

            await respond_btn.click()
            await page.sleep(random.uniform(3, 5))
            await mgr.take_screenshot(page, project_id, "02_form_opened")

            editor = await page.find(".trumbowyg-editor", timeout=4)
            if editor:
                safe_text = json.dumps(proposal_text)
                await page.evaluate(f"""
                    const editor = document.querySelector('.trumbowyg-editor');
                    if (editor) {{
                        editor.innerHTML = {safe_text};
                        editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        editor.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                    }}
                """)
                await page.sleep(random.uniform(1, 2))
            else:
                textarea = await page.find("textarea[name='description']", timeout=2)
                if textarea:
                    await textarea.send_keys(proposal_text)

            if price:
                try:
                    price_input = await page.find("#offer-custom-price", timeout=2)
                    if not price_input:
                        price_input = await page.find("input[type='tel']", timeout=2)
                    if price_input:
                        await page.evaluate("""
                            () => {
                                const el = document.querySelector('#offer-custom-price') || document.querySelector('input[type="tel"]');
                                if (el) {
                                    el.value = '';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                }
                            }
                        """)
                        await price_input.send_keys(str(int(float(price))))
                        await page.sleep(random.uniform(0.5, 1))
                except Exception as e:
                    logger.debug(f"Kwork: цена не введена: {e}")

            await mgr.take_screenshot(page, project_id, "03_form_filled")

            if dry_run:
                logger.info(f"Kwork: [DRY-RUN] Форма заполнена, ПРОПУСКАЕМ КЛИК Отправить. Пауза 5 сек...")
                await page.sleep(5)
                self._cleanup_screenshots(project_id)
                return True

            submit_btn = await page.find("Предложить", timeout=3)
            if not submit_btn:
                submit_btn = await page.find("button.kw-button--green", timeout=3)

            if submit_btn:
                await submit_btn.click()
                logger.info(f"Kwork: Форма отправлена!")
                await page.sleep(4)
                self._cleanup_screenshots(project_id)
                return True

            logger.warning("Kwork: не удалось найти кнопку отправки")
            return False

        except Exception as e:
            logger.error(f"Kwork: ошибка браузерной отправки: {e}")
            return False
        finally:
            await mgr.close_page(page)

    async def send_flru_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
    ) -> bool:
        logger.info(f"FL.ru: отправка отклика на {project_url}")

        await self._ensure_auth("www.fl.ru")
        mgr = self.browser_mgr

        if not mgr._auth_validated.get("www.fl.ru", False):
            logger.warning("FL.ru: авторизация не подтверждена")
            return False

        page = await mgr.get_page(project_url, reuse=True)

        try:
            await page.sleep(random.uniform(2, 4))

            respond_btn = await page.find("Откликнуться", timeout=5)
            if not respond_btn:
                logger.warning("FL.ru: кнопка 'Откликнуться' не найдена")
                return False

            await respond_btn.click()
            await page.sleep(random.uniform(1.5, 3))

            textarea = await page.find('textarea[name*="offer"]', timeout=3)
            if not textarea:
                textarea = await page.find("textarea", timeout=3)

            if textarea and proposal_text:
                safe_text = json.dumps(proposal_text)
                await page.evaluate(f"""
                    const ta = document.querySelector('textarea[name*="offer"]') || document.querySelector('textarea');
                    if (ta) {{
                        ta.value = {safe_text};
                        ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                """)
                await page.sleep(0.5)

            if price:
                price_input = await page.find('input[name="price"]', timeout=3)
                if not price_input:
                    price_input = await page.find("input.js-money", timeout=3)
                if price_input:
                    await price_input.send_keys(str(price))
                    await page.sleep(0.5)

            await mgr.take_screenshot(page, project_id, "03_form_filled")

            submit_btn = await page.find('button[type="submit"]', timeout=3)
            if submit_btn:
                await submit_btn.click()
                await page.sleep(random.uniform(3, 5))

                html = await page.get_content()
                if any(m in html.lower() for m in ["отклик отправлен", "предложение отправлено"]):
                    logger.info("FL.ru: отклик отправлен")
                    return True

            logger.warning("FL.ru: не удалось подтвердить отправку")
            return False

        except Exception as e:
            logger.error(f"FL.ru: ошибка отправки: {e}")
            return False
        finally:
            await mgr.close_page(page)

    async def get_project_preview(
        self,
        project_url: str,
        project_id: str,
        project_title: Optional[str] = None,
    ) -> Optional[str]:
        await self._ensure_auth("kwork.ru")
        mgr = self.browser_mgr

        page = await mgr.get_page(project_url, reuse=True)

        try:
            logger.info(f"Kwork: загрузка превью для {project_id}...")
            loaded = await mgr.wait_for_content(page, "div.project-description", timeout=10)

            logger.info(f"Kwork: делаем скриншот страницы проекта...")
            screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page")

            if not loaded:
                await page.scroll_down(400)
                await page.sleep(2)
                screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page_scrolled")

            return screenshot_path
        except Exception as e:
            logger.error(f"Kwork preview error: {e}")
            await mgr.close_page(page)
            return None

    async def send_freelancercom_proposal(
        self,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
    ) -> bool:
        token = os.getenv("FREELANCER_OAUTH_TOKEN")
        if not token:
            logger.warning("Freelancer.com: нет OAUTH токена")
            return False

        url = "https://www.freelancer.com/api/projects/0.1/bids/"
        headers = {"freelancer-oauth-v1": token, "Content-Type": "application/json"}
        payload = {
            "project_id": int(project_id),
            "amount": float(price) if price else 50.0,
            "period": 7,
            "milestone_percentage": 100,
            "description": proposal_text,
        }

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, headers=headers, json=payload, timeout=10.0)
                if res.status_code == 200:
                    logger.info("Freelancer.com: бид отправлен")
                    return True
                logger.error(f"Freelancer.com API Error {res.status_code}: {res.text}")
                return False
        except Exception as e:
            logger.error(f"Freelancer.com Request Error: {e}")
            return False

    async def send_proposal(
        self,
        platform: str,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
        dry_run: bool = False,
    ) -> bool:
        if platform == "kwork":
            return await self.send_kwork_proposal(project_url, project_id, proposal_text, price, dry_run=dry_run)
        elif platform == "fl_ru":
            return await self.send_flru_proposal(project_url, project_id, proposal_text, price)
        elif platform == "freelancer_com":
            return await self.send_freelancercom_proposal(project_id, proposal_text, price)
        else:
            logger.warning(f"Авто-отправка не поддерживается: {platform}")
            return False
