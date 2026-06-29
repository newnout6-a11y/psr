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


def _text_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts using 3-word shingles.

    Returns 0.0 (completely different) to 1.0 (identical).
    Used to detect duplicate/near-duplicate proposals before sending.
    """
    if not text_a or not text_b:
        return 0.0
    words_a = text_a.lower().split()
    words_b = text_b.lower().split()
    if len(words_a) < 3 or len(words_b) < 3:
        return 1.0 if text_a.strip() == text_b.strip() else 0.0
    shingles_a = set()
    shingles_b = set()
    for i in range(len(words_a) - 2):
        shingles_a.add(" ".join(words_a[i : i + 3]))
    for i in range(len(words_b) - 2):
        shingles_b.add(" ".join(words_b[i : i + 3]))
    if not shingles_a or not shingles_b:
        return 0.0
    intersection = shingles_a & shingles_b
    union = shingles_a | shingles_b
    return len(intersection) / len(union)


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

    async def _get_kwork_api(self):
        return await self.kwork_service.get_api()

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

    @staticmethod
    def _kwork_web_submit_succeeded(result: dict) -> bool:
        status = int(result.get("status") or 0)
        payload = result.get("json")
        if isinstance(payload, dict):
            if payload.get("success") is False:
                return False
            if payload.get("error"):
                return False
            if payload.get("success") is True:
                return True
            if str(payload.get("status", "")).lower() in {"ok", "success"}:
                return True
        return 200 <= status < 300

    @staticmethod
    def _kwork_web_error_message(result: dict) -> str:
        payload = result.get("json")
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or payload.get("response") or "неизвестная ошибка")
        text = str(result.get("text") or "")
        return text[:300] if text else "неизвестная ошибка"

    async def send_kwork_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
        dry_run: bool = False,
        attachments: Optional[list[str]] = None,
        platform_data: Optional[dict] = None,
    ) -> bool:
        logger.info(f"Kwork: отправка отклика на проект {project_id}")

        attachments = [item for item in (attachments or []) if item]
        if attachments and os.getenv("KWORK_IMAGE_ATTACH_CONFIRMED", "false").lower() not in {"1", "true", "yes", "on"}:
            logger.info("Kwork: attachments prepared but upload flow is not confirmed; sending text only")
            attachments = []
        elif attachments:
            logger.warning("Kwork: attachment upload is marked confirmed, but API helper has no file upload path yet; sending text only")
            attachments = []

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

        api = await self._get_kwork_api()
        if api:
            try:
                if not self._kwork_web_logged_in:
                    await api.web_login(url_to_redirect="/")
                    self._kwork_web_logged_in = True
                    logger.info("Kwork: web-сессия установлена через мобильный API")

                kwork_name = "Разработка"
                kwork_duration = 7
                if platform_data and isinstance(platform_data, dict):
                    cat_id = platform_data.get("category_id")
                    available_durations = platform_data.get("available_durations") or []
                    if available_durations and isinstance(available_durations, list):
                        try:
                            durations = [int(d) for d in available_durations if d]
                            if 7 in durations:
                                kwork_duration = 7
                            elif durations:
                                kwork_duration = min(durations, key=lambda d: abs(d - 7))
                        except (ValueError, TypeError):
                            pass
                    if cat_id:
                        try:
                            from src.platforms.kwork import get_kwork_service
                            svc = get_kwork_service()
                            cats = await svc.get_all_categories()
                            for cat in cats:
                                if isinstance(cat, dict):
                                    sub_cats = cat.get("categories", []) or cat.get("childs", []) or []
                                    for sub in sub_cats:
                                        if isinstance(sub, dict) and str(sub.get("id")) == str(cat_id):
                                            kwork_name = sub.get("name", kwork_name)
                                            break
                                    if str(cat.get("id")) == str(cat_id):
                                        kwork_name = cat.get("name", kwork_name)
                                        break
                        except Exception:
                            pass

                from src.platforms.kwork_ext import KworkExtensions
                is_flagged = await KworkExtensions.is_text_template_flagged(api, int(project_id), proposal_text)
                if is_flagged:
                    logger.warning(f"Kwork: proposal text flagged as TEMPLATE by check_is_template for project {project_id}")
                    if not dry_run:
                        logger.error("Kwork: отправка отклика отменена — текст помечен как шаблонный")
                        return False

                from src.action.proposal_db import ProposalDB
                db = ProposalDB()
                recent_texts = db.get_recent_proposal_texts(limit=20)
                for prev_text in recent_texts:
                    similarity = _text_similarity(proposal_text, prev_text)
                    if similarity > 0.8:
                        logger.warning(f"Kwork: proposal text {similarity:.0%} similar to a recent proposal — high duplicate risk")
                        if similarity > 0.9 and not dry_run:
                            logger.error("Kwork: отправка отменена — текст почти идентичен предыдущему отклику")
                            return False
                        break

                if not await KworkExtensions.check_web_session(api):
                    logger.warning("Kwork: web-сессия невалидна, реавторизация...")
                    self._kwork_web_logged_in = False
                    await api.web_login(url_to_redirect="/")
                    self._kwork_web_logged_in = True

                result = await api.web.submit_exchange_offer(
                    project_id=int(project_id),
                    offer_type="custom",
                    description=proposal_text,
                    kwork_duration=kwork_duration,
                    kwork_price=numeric_price,
                    kwork_name=kwork_name,
                )

                if self._kwork_web_submit_succeeded(result):
                    logger.info(f"Kwork: отклик отправлен через API на проект {project_id}")
                    return True

                error_msg = self._kwork_web_error_message(result)
                logger.error(f"Kwork: отклик отклонён API: {error_msg}")

                error_lower = error_msg.lower()
                if any(m in error_lower for m in ("уже отправл", "already responded", "уже откликнул")):
                    logger.info(f"Kwork: уже откликнулись на проект {project_id}, повторная отправка отменена")
                    return True

                if any(m in error_lower for m in ("csrf", "token", "auth", "login", "session", "unauthorized")):
                    logger.warning("Kwork: web-сессия истекла, сбрасываем _kwork_web_logged_in для реавторизации")
                    self._kwork_web_logged_in = False

            except Exception as e:
                error_str = str(e).lower()
                if any(m in error_str for m in ("csrf", "token", "auth", "login", "session")):
                    logger.warning(f"Kwork: auth/CSRF ошибка ({e}), сбрасываем web-сессию")
                    self._kwork_web_logged_in = False
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
                readback = await page.evaluate(
                    """
                    () => {
                        const editor = document.querySelector('.trumbowyg-editor');
                        const textarea = document.querySelector('textarea[name="description"], .trumbowyg-textarea');
                        return {
                            editorText: editor ? (editor.innerText || '').trim() : '',
                            textareaValue: textarea ? (textarea.value || '').trim() : ''
                        };
                    }
                    """
                )
                if readback and isinstance(readback, dict):
                    filled = readback.get("editorText", "") or readback.get("textareaValue", "")
                    if len(filled) < 40:
                        logger.warning(f"Kwork: Trumbowyg readback короткий ({len(filled)} chars), возможна пустая отправка")
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
                confirmed = False
                for _poll in range(20):
                    await page.sleep(0.75)
                    if await self._confirm_kwork_submission(page):
                        confirmed = True
                        break
                    if await self._page_contains(
                        page,
                        ["ошибка", "не удалось", "минимальная цена", "заполните", "слишком короткое"],
                    ):
                        break
                if confirmed:
                    self._cleanup_screenshots(project_id)
                    logger.info("Kwork: отправка подтверждена")
                    return True

                if await self._page_contains(
                    page,
                    ["ошибка", "не удалось", "минимальная цена", "заполните", "слишком короткое"],
                ):
                    logger.warning("Kwork: после submit обнаружены признаки ошибки, отклик не подтверждён")
                else:
                    logger.warning("Kwork: submit выполнен, но подтверждение отправки не найдено (15s polling)")
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
            await mgr.wait_for_content(page, selector, timeout=15)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.sleep(1)
            screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page", full_page=True)
            if not screenshot_path:
                await page.scroll_down(200)
                await page.sleep(2)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.sleep(1)
                screenshot_path = await mgr.take_screenshot(page, project_id, "01_project_page", full_page=True)
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
        attachments: Optional[list[str]] = None,
        platform_data: Optional[dict] = None,
    ) -> bool:
        from src.action.pii_filter import filter_proposal_text

        cleaned_text, violations = filter_proposal_text(proposal_text)
        if violations and not dry_run:
            logger.warning(f"ProposalSender: PII/stop-word фильтр сработал для {platform}/{project_id}: {'; '.join(violations)}")
        if len(cleaned_text) < 40 and not dry_run:
            logger.error(f"ProposalSender: после PII-фильтра текст слишком короткий ({len(cleaned_text)} chars), отправка отменена")
            return False

        if platform == "kwork":
            return await self.send_kwork_proposal(
                project_url,
                project_id,
                cleaned_text,
                price,
                dry_run=dry_run,
                attachments=attachments,
                platform_data=platform_data,
            )
        if platform == "freelance_ru":
            return await self.send_freelanceru_proposal(
                project_url,
                project_id,
                cleaned_text,
                price,
                dry_run=dry_run,
            )

        logger.warning(f"Авто-отправка не поддерживается: {platform}")
        return False
