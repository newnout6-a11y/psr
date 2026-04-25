"""
Отправка откликов.
Поддерживаемые платформы: Kwork и Freelance.ru.
"""

import json
import os
import random
import shutil
from typing import Optional

from loguru import logger

from src.browser.browser_manager import BrowserManager
from src.paths import SCREENSHOTS_DIR
from src.platforms.kwork import get_kwork_service


class ProposalSender:
    def __init__(self, headless: bool = True, timeout: int = 30):
        self.timeout = timeout
        self.browser_mgr = BrowserManager(headless=headless)
        self.kwork_service = get_kwork_service()
        self._kwork_web_logged_in = False

    async def start(self):
        pass

    async def stop(self):
        await self.browser_mgr.stop()
        await self.kwork_service.close()

    def _cleanup_screenshots(self, project_id: str):
        """Удалить папку со скриншотами проекта после отправки."""
        screenshot_dir = os.path.join(str(SCREENSHOTS_DIR), project_id)
        if os.path.exists(screenshot_dir):
            try:
                shutil.rmtree(screenshot_dir)
                logger.debug(f"Скриншоты удалены: {screenshot_dir}")
            except Exception as e:
                logger.debug(f"Не удалось удалить скриншоты: {e}")

    async def scrape_competitor_prices(self, project_url: str) -> list:
        """Парсинг видимых цен конкурентов со страницы проекта (до отправки отклика)."""
        self.last_competitor_prices = []
        try:
            await self._ensure_auth("kwork.ru")
            mgr = self.browser_mgr
            if not mgr._auth_validated.get("kwork.ru", False):
                return []

            page = await mgr.get_page(project_url, reuse=True)
            await page.scroll_down(300)
            await page.sleep(2)

            competitor_prices = await page.evaluate(
                """
                () => {
                    const results = [];
                    const nodes = document.querySelectorAll(
                        '.offer-card, .response-item, .wants-offer, .offer-item, ' +
                        '[class*="offer"], [class*="response"], [class*="candidate"]'
                    );
                    nodes.forEach(node => {
                        const priceEl = node.querySelector(
                            '.price, .amount, .bid-value, [class*="price"], ' +
                            '[class*="cost"], [class*="budget"]'
                        );
                        const nameEl = node.querySelector(
                            '.username, .freelancer-name, [class*="username"], ' +
                            '[class*="name"]'
                        );
                        if (priceEl) {
                            const raw = priceEl.innerText.trim();
                            const clean = raw.replace(/[^0-9.]/g, '');
                            const val = parseFloat(clean);
                            if (val > 0) {
                                results.push({ price: val, name: nameEl ? nameEl.innerText.trim() : '', raw: raw });
                            }
                        }
                    });
                    if (results.length === 0) {
                        const allEls = document.querySelectorAll('span, div, p');
                        allEls.forEach(el => {
                            const text = el.innerText || '';
                            if (text.length < 30 && /\\d+\\s*(₽|руб)/.test(text)) {
                                const clean = text.replace(/[^0-9.]/g, '');
                                const val = parseFloat(clean);
                                if (val > 0 && val < 1000000) {
                                    results.push({ price: val, name: '', raw: text.trim() });
                                }
                            }
                        });
                    }
                    const unique = [];
                    const seen = new Set();
                    results.forEach(r => { if (!seen.has(r.price)) { seen.add(r.price); unique.push(r); } });
                    return unique;
                }
                """
            )
            if competitor_prices:
                prices_str = ", ".join(
                    f"{p['price']}₽" + (f" ({p['name']})" if p.get('name') else "")
                    for p in competitor_prices[:10]
                )
                logger.info(f"Kwork: конкурентные цены: [{prices_str}]")
                self.last_competitor_prices = competitor_prices
                return competitor_prices
        except Exception as e:
            logger.debug(f"Kwork: не удалось спарсить цены конкурентов: {e}")
        return []

    def _get_kwork_api(self):
        return self.kwork_service.get_api()

    async def _ensure_auth(self, domain: str):
        if not self.browser_mgr._auth_validated.get(domain, False):
            await self.browser_mgr.init_auth(domain)

    async def _page_contains(self, page, markers: list[str]) -> bool:
        try:
            html = (await page.get_content()).lower()
        except Exception:
            return False
        return any(marker.lower() in html for marker in markers)

    async def _confirm_kwork_submission(self, page) -> bool:
        try:
            return await page.evaluate(
                """
                (() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    const successMarkers = [
                        'предложение отправлено',
                        'отклик отправлен',
                        'ваше предложение отправлено',
                        'предложение успешно отправлено',
                        'предложение принято',
                        'ответ отправлен'
                    ];
                    if (successMarkers.some(marker => text.includes(marker))) {
                        return true;
                    }

                    const successSelectors = [
                        '.alert-success',
                        '.kw-alert--success',
                        '.wants-offer-success',
                        '[class*="success-message"]',
                        '[class*="offer-success"]'
                    ];
                    return successSelectors.some(selector => document.querySelector(selector));
                })()
                """
            )
        except Exception:
            return False

    async def send_kwork_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
        dry_run: bool = False,
    ) -> bool:
        logger.info(f"Kwork: отправка отклика на проект {project_id}")

        numeric_price = 500
        if price:
            try:
                numeric_price = int(float(price))
                if numeric_price < 500:
                    logger.warning(f"Kwork: цена {numeric_price} ниже минимума. Устанавливаю 500.")
                    numeric_price = 500
            except Exception:
                numeric_price = 500

        if dry_run:
            logger.info("Kwork: [DRY-RUN] браузерная демонстрация без отправки")
            return await self._send_kwork_browser(
                project_url,
                project_id,
                proposal_text,
                str(numeric_price),
                dry_run=True,
            )

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

                error_msg = json_resp.get("message", "неизвестная ошибка")
                logger.error(f"Kwork: отклик отклонён API: {error_msg}")
            except Exception as e:
                logger.error(f"Kwork: ошибка API-отправки: {e}")

        logger.warning("Kwork: API недоступен или упал, fallback на браузер")
        return await self._send_kwork_browser(
            project_url,
            project_id,
            proposal_text,
            str(numeric_price),
            dry_run=False,
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
        self.last_competitor_prices = []  # Сброс перед новым проектом

        await self._ensure_auth("kwork.ru")
        mgr = self.browser_mgr

        if not mgr._auth_validated.get("kwork.ru", False):
            logger.warning("Kwork: браузерная авторизация не подтверждена")
            return False

        page = await mgr.get_page(project_url, reuse=True)

        # Конкурентная разведка: парсим видимые цены конкурентов со страницы
        competitor_prices = []
        try:
            competitor_prices = await page.evaluate(
                """
                () => {
                    const results = [];
                    // Вариант 1: карточки откликов с классами offer-card / response-item
                    const nodes = document.querySelectorAll(
                        '.offer-card, .response-item, .wants-offer, .offer-item, ' +
                        '[class*="offer"], [class*="response"], [class*="candidate"]'
                    );
                    nodes.forEach(node => {
                        const priceEl = node.querySelector(
                            '.price, .amount, .bid-value, [class*="price"], ' +
                            '[class*="cost"], [class*="budget"]'
                        );
                        const nameEl = node.querySelector(
                            '.username, .freelancer-name, [class*="username"], ' +
                            '[class*="name"]'
                        );
                        if (priceEl) {
                            const raw = priceEl.innerText.trim();
                            const clean = raw.replace(/[^0-9.]/g, '');
                            const val = parseFloat(clean);
                            if (val > 0) {
                                results.push({
                                    price: val,
                                    name: nameEl ? nameEl.innerText.trim() : '',
                                    raw: raw
                                });
                            }
                        }
                    });

                    // Вариант 2: ищем все элементы с текстом "₽" или "руб" рядом с числом
                    if (results.length === 0) {
                        const allEls = document.querySelectorAll('span, div, p');
                        allEls.forEach(el => {
                            const text = el.innerText || '';
                            if (text.length < 30 && /\\d+\\s*(₽|руб)/.test(text)) {
                                const clean = text.replace(/[^0-9.]/g, '');
                                const val = parseFloat(clean);
                                if (val > 0 && val < 1000000) {
                                    results.push({ price: val, name: '', raw: text.trim() });
                                }
                            }
                        });
                    }

                    // Убираем дубликаты
                    const unique = [];
                    const seen = new Set();
                    results.forEach(r => {
                        if (!seen.has(r.price)) {
                            seen.add(r.price);
                            unique.push(r);
                        }
                    });
                    return unique;
                }
                """
            )
            if competitor_prices:
                prices_str = ", ".join(
                    f"{p['price']}₽" + (f" ({p['name']})" if p.get('name') else "")
                    for p in competitor_prices[:10]
                )
                logger.info(f"Kwork: конкурентные цены: [{prices_str}]")
                self.last_competitor_prices = competitor_prices
        except Exception as e:
            logger.debug(f"Kwork: не удалось спарсить цены конкурентов: {e}")

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
                await page.evaluate(
                    f"""
                    const editor = document.querySelector('.trumbowyg-editor');
                    if (editor) {{
                        editor.innerHTML = {safe_text};
                        editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        editor.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                    }}
                    """
                )
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
                        await page.evaluate(
                            """
                            () => {
                                const el = document.querySelector('#offer-custom-price') || document.querySelector('input[type="tel"]');
                                if (el) {
                                    el.value = '';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                }
                            }
                            """
                        )
                        await price_input.send_keys(str(int(float(price))))
                        await page.sleep(random.uniform(0.5, 1))
                except Exception as e:
                    logger.debug(f"Kwork: цена не введена: {e}")

            await mgr.take_screenshot(page, project_id, "03_form_filled")

            if dry_run:
                logger.info("Kwork: [DRY-RUN] форма заполнена, отправку пропускаем")
                await page.sleep(5)
                self._cleanup_screenshots(project_id)
                return True

            submit_btn = await page.find("Предложить", timeout=3)
            if not submit_btn:
                submit_btn = await page.find("button.kw-button--green", timeout=3)

            if submit_btn:
                await submit_btn.click()
                await page.sleep(4)
                if await self._confirm_kwork_submission(page):
                    self._cleanup_screenshots(project_id)
                    logger.info("Kwork: отправка подтверждена")
                    return True

                if await self._page_contains(
                    page,
                    [
                        "ошибка",
                        "не удалось",
                        "минимальная цена",
                        "заполните",
                        "слишком короткое",
                    ],
                ):
                    logger.warning("Kwork: после submit обнаружены признаки ошибки, отклик не подтверждён")
                else:
                    logger.warning("Kwork: submit выполнен, но подтверждение отправки не найдено")
                return False

            logger.warning("Kwork: не удалось найти кнопку отправки")
            return False
        except Exception as e:
            logger.error(f"Kwork: ошибка браузерной отправки: {e}")
            return False
        finally:
            await mgr.close_page(page)

    async def _open_freelanceru_answer_form(self, page) -> bool:
        for _ in range(2):
            opened = await page.evaluate(
                """
                (() => {
                    const button = document.querySelector('.answer-button');
                    if (button && getComputedStyle(button).display !== 'none') {
                        button.click();
                    }

                    const form = document.querySelector('.answer-form');
                    if (form) {
                        form.style.display = 'block';
                    }

                    return !!(
                        document.querySelector('.answer-form textarea') ||
                        document.querySelector('#discussion_div textarea') ||
                        document.querySelector('textarea')
                    );
                })()
                """
            )
            if opened:
                return True
            await page.sleep(1.5)

        try:
            return await page.find("textarea", timeout=3) is not None
        except Exception:
            return False

    async def _fill_freelanceru_form(self, page, proposal_text: str, price: Optional[str]) -> bool:
        safe_text = json.dumps(proposal_text)
        safe_price = json.dumps("")
        if price:
            try:
                safe_price = json.dumps(str(int(float(price))))
            except Exception:
                safe_price = json.dumps(str(price))

        return await page.evaluate(
            f"""
            (() => {{
                const form =
                    document.querySelector('.answer-form form') ||
                    document.querySelector('form[action*="discussion"]') ||
                    document.querySelector('form');
                const textarea =
                    document.querySelector('.answer-form textarea') ||
                    form?.querySelector('textarea') ||
                    document.querySelector('textarea');

                if (!textarea) {{
                    return false;
                }}

                textarea.focus();
                textarea.value = {safe_text};
                textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));

                const inlineMode = form?.querySelector('input[name="mode"]');
                if (inlineMode && !inlineMode.value) {{
                    inlineMode.value = 'inline';
                }}

                const priceValue = {safe_price};
                if (priceValue) {{
                    const priceInput =
                        form?.querySelector('input[name*="price"], input[name*="cost"], input[name*="budget"], input[name*="sum"]');
                    if (priceInput) {{
                        priceInput.value = priceValue;
                        priceInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        priceInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}

                return true;
            }})()
            """
        )

    async def send_freelanceru_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
        dry_run: bool = False,
    ) -> bool:
        logger.info(f"Freelance.ru: отправка отклика на {project_url}")

        await self._ensure_auth("freelance.ru")
        mgr = self.browser_mgr

        if not mgr._auth_validated.get("freelance.ru", False):
            logger.warning("Freelance.ru: авторизация не подтверждена")
            return False

        page = await mgr.get_page(project_url, reuse=True)

        try:
            await page.sleep(random.uniform(2, 4))
            await mgr.take_screenshot(page, project_id, "01_project_page")

            blocked = await self._page_contains(
                page,
                [
                    "доступ к этому заданию для базовых аккаунтов закрыт",
                    "/auth/login?return_url=",
                    "премиум-аккаунтом",
                ],
            )
            if blocked:
                logger.warning("Freelance.ru: проект закрыт для текущего аккаунта")
                return False

            form_opened = await self._open_freelanceru_answer_form(page)
            if not form_opened:
                logger.warning("Freelance.ru: форма ответа не найдена")
                return False

            filled = await self._fill_freelanceru_form(page, proposal_text, price)
            if not filled:
                logger.warning("Freelance.ru: не удалось заполнить форму")
                return False

            await mgr.take_screenshot(page, project_id, "02_form_filled")

            if dry_run:
                logger.info("Freelance.ru: [DRY-RUN] форма заполнена, отправку пропускаем")
                await page.sleep(5)
                self._cleanup_screenshots(project_id)
                return True

            submitted = await page.evaluate(
                """
                (() => {
                    const form =
                        document.querySelector('.answer-form form') ||
                        document.querySelector('form[action*="discussion"]') ||
                        document.querySelector('form');
                    if (!form) {
                        return false;
                    }

                    const submit =
                        form.querySelector('button[type="submit"], input[type="submit"], .btn-success, .btn-primary');
                    if (submit) {
                        submit.click();
                        return true;
                    }

                    if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                    }

                    form.submit();
                    return true;
                })()
                """
            )
            if not submitted:
                logger.warning("Freelance.ru: кнопка отправки не найдена")
                return False

            await page.sleep(5)
            await mgr.take_screenshot(page, project_id, "03_form_submitted")

            sent = await page.evaluate(
                """
                (() => !!(
                    document.querySelector('.new_message .th_message') ||
                    document.querySelector('.message.th_message') ||
                    document.querySelector('.message.have_answer')
                ))()
                """
            )
            if sent:
                logger.info("Freelance.ru: отклик отправлен")
                self._cleanup_screenshots(project_id)
                return True

            if await self._page_contains(page, ["сообщение отправлено", "ответ добавлен"]):
                logger.info("Freelance.ru: отправка подтверждена текстовым маркером")
                self._cleanup_screenshots(project_id)
                return True

            logger.warning("Freelance.ru: не удалось подтвердить отправку")
            return False
        except Exception as e:
            logger.error(f"Freelance.ru: ошибка отправки: {e}")
            return False
        finally:
            await mgr.close_page(page)

    async def get_project_preview(
        self,
        project_url: str,
        project_id: str,
        platform: str,
    ) -> Optional[str]:
        platform = platform.lower()
        selector_map = {
            "kwork": ".wants-card__header-title",
            "freelance_ru": ".proj-comm-card",
        }
        domain_map = {
            "kwork": "kwork.ru",
            "freelance_ru": "freelance.ru",
        }

        domain = domain_map.get(platform)
        selector = selector_map.get(platform, "body")
        if domain:
            try:
                await self._ensure_auth(domain)
            except Exception as e:
                logger.debug(f"Preview auth check failed for {domain}: {e}")

        mgr = self.browser_mgr
        page = await mgr.get_page(project_url, reuse=True)

        try:
            await mgr.wait_for_content(page, selector, timeout=10)
            screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page")
            if not screenshot_path:
                await page.scroll_down(400)
                await page.sleep(2)
                screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page_scrolled")
            return screenshot_path
        except Exception as e:
            logger.error(f"{platform} preview error: {e}")
            await mgr.close_page(page)
            return None

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
            return await self.send_kwork_proposal(
                project_url,
                project_id,
                proposal_text,
                price,
                dry_run=dry_run,
            )
        if platform == "freelance_ru":
            return await self.send_freelanceru_proposal(
                project_url,
                project_id,
                proposal_text,
                price,
                dry_run=dry_run,
            )

        logger.warning(f"Авто-отправка не поддерживается: {platform}")
        return False
