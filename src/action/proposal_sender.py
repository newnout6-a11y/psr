"""
Браузерная отправка откликов через Nodriver.
Обходит SPA-интерфейсы и анти-боты Kwork и FL.ru.
"""

import os
import random
import asyncio
import json
from typing import Dict, Any, Optional
import nodriver as uc
import httpx
from loguru import logger


class ProposalSender:
    """Отправка откликов через Nodriver (undetected)."""

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 30,
    ):
        self.headless = headless
        self.timeout = timeout
        self.browser = None
        
    async def start(self):
        """Запуск браузера Nodriver."""
        if not self.browser:
            logger.info("Запуск браузера Nodriver...")
            self.browser = await uc.start(
                headless=self.headless,
                browser_args=['--window-size=1920,1080', '--disable-blink-features=AutomationControlled']
            )

    async def stop(self):
        """Закрытие браузера."""
        if self.browser:
            self.browser.stop()
            self.browser = None
            logger.info("Браузер закрыт")

    async def _set_cookies(self, page: uc.Tab, cookies: Dict[str, str], domain: str):
        """Установка кук в страницу."""
        # Для Nodriver нужно сначала перейти на домен перед установкой кук,
        # либо ставить их глобально. Перейдем на главную.
        await page.get(f"https://{domain.lstrip('.')}/")
        
        for k, v in cookies.items():
            if not v:
                continue
            await page.send(uc.cdp.network.set_cookie(
                name=k,
                value=str(v),
                domain=domain,
                path="/"
            ))

    def _get_kwork_cookies(self) -> Dict[str, str]:
        return {
            "slrememberme": os.getenv("KWORK_COOKIE_REMEMBERME", ""),
            "userId": os.getenv("KWORK_COOKIE_USERID", ""),
        }

    def _looks_like_loaded_kwork_page(self, html: str, expected_text: Optional[str] = None) -> bool:
        if not html:
            return False

        html_lower = html.lower()
        if expected_text and expected_text.lower() in html_lower:
            return True

        markers = (
            "предложить услугу",
            "откликнуться",
            "want-page",
            "project-description",
            "wants-card__description-text",
            "sidebar-user-info"
        )
        return any(marker in html_lower for marker in markers)

    async def _open_kwork_page(self, page: uc.Tab, project_url: str, expected_text: Optional[str] = None) -> bool:
        """Открывает страницу проекта Kwork с короткими ретраями, пока контент не дорендерится."""
        for attempt in range(1, 4):
            await page.get(project_url)
            await page.sleep(2 + attempt)

            try:
                await page.evaluate("window.scrollTo(0, Math.max(250, document.body.scrollHeight * 0.15))")
                await page.sleep(1)
            except Exception as e:
                logger.debug(f"Kwork preview: не удалось проскроллить страницу: {e}")

            try:
                html = await page.get_content()
            except Exception as e:
                logger.warning(f"Kwork preview: не удалось получить HTML (попытка {attempt}/3): {e}")
                html = ""

            if self._looks_like_loaded_kwork_page(html, expected_text):
                return True

            logger.warning(f"Kwork preview: страница выглядит недогруженной (попытка {attempt}/3)")

        return False

    async def send_flru_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
    ) -> bool:
        """Отправить отклик на FL.ru через Nodriver."""
        logger.info(f"FL.ru: отправка отклика на {project_url}")

        cookies = {
            "PHPSESSID": os.getenv("FL_RU_COOKIE_SESSION", ""),
            "id": os.getenv("FL_RU_COOKIE_ID", ""),
            "pwd": os.getenv("FL_RU_COOKIE_PWD", ""),
            "XSRF-TOKEN": os.getenv("FL_RU_XSRF_TOKEN", ""),
        }
        if not cookies["PHPSESSID"]:
            logger.warning("FL.ru: нет кук авторизации")
            return False

        await self.start()
        page = await self.browser.get("about:blank")
        await self._set_cookies(page, cookies, domain="www.fl.ru")

        try:
            await page.get(project_url)
            await page.sleep(random.uniform(2, 4))

            # Ищем кнопку "Откликнуться" - Nodriver find
            respond_btn = await page.find('a[href*="/projects/"]', text='Откликнуться', timeout=5)
            if not respond_btn:
                logger.warning("FL.ru: кнопка 'Откликнуться' не найдена")
                return False

            await respond_btn.click()
            await page.sleep(random.uniform(1.5, 3))

            # Ищем textarea 
            textarea = await page.find('textarea[name*="offer"]', timeout=3)
            if not textarea:
                textarea = await page.find('textarea', timeout=3)
                
            if price:
                price_input = await page.find('input[name="price"]', timeout=3)
                if not price_input:
                    # Попробуем найти по частичному совпадению или другому ID
                    price_input = await page.find('input.js-money', timeout=3)
                
                if price_input:
                    await price_input.send_keys(str(price))
                    await page.sleep(0.5)
            
            # ДЕЛАЕМ СКРИНШОТ ЗАПОЛНЕННОЙ ФОРМЫ (то, что ты просил!)
            await self._take_action_screenshot(page, project_id, "03_form_filled")
            logger.info(f"Kwork: форма заполнена для {project_id}, цена {price}")

            # Кнопка отправки
            submit_btn = await page.find('button[type="submit"]', text='Откликнуться', timeout=3)
            if submit_btn:
                await submit_btn.click()
                await page.sleep(random.uniform(3, 5))
                # Проверки успеха
                success = await page.find('text="Отклик отправлен"', timeout=3)
                if success:
                    logger.info("FL.ru: отклик отправлен ✓")
                    return True
            
            logger.warning("FL.ru: не удалось подтвердить отправку")
            return False

        except Exception as e:
            logger.error(f"FL.ru: ошибка отправки Nodriver: {e}")
            return False
        finally:
            await page.close()

    async def get_project_preview(
        self,
        project_url: str,
        project_id: str,
        project_title: Optional[str] = None,
    ) -> Optional[str]:
        """Зайти на страницу и сделать превью-скриншот для уведомления."""
        await self.start()
        # Заходим сразу на проект, без промежуточных страниц
        page = await self.browser.get(project_url)

        try:
            # Умное ожидание контента
            logger.info(f"Kwork: ожидание загрузки контента для {project_id}...")
            await page.scroll_down(400) # Прокрутка для активации рендеринга
            await page.sleep(2)
            
            # Ждем появления описания проекта
            try:
                await page.wait_for("div.project-description", timeout=10)
                logger.info("Kwork: описание найдено, делаем скриншот")
            except:
                pass
            return await self._take_action_screenshot(page, project_id, "01_project_page")
        except Exception as e:
            logger.error(f"Kwork preview error: {e}")
            await page.close()
            return None
        # ВАЖНО: Мы НЕ закрываем страницу в finally, 
        # чтобы использовать её для заполнения формы после ответа юзера

    async def _take_action_screenshot(self, page: Any, project_id: str, step_name: str) -> Optional[str]:
        """Вспомогательный метод для сохранения скриншота шага."""
        try:
            folder = os.path.join("data", "screenshots", str(project_id))
            if not os.path.exists(folder):
                os.makedirs(folder)
            
            # Читабельное имя файла
            filename = f"{step_name}.png"
            path = os.path.join(folder, filename)
            await page.save_screenshot(path)
            logger.debug(f"Скриншот сохранен: {path}")
            return path
        except Exception as e:
            logger.warning(f"Не удалось сделать скриншот {step_name}: {e}")
            return None

    async def send_kwork_proposal(
        self,
        project_url: str,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
    ) -> bool:
        """Отправить отклик на Kwork через Nodriver."""
        logger.info(f"Kwork: подготовка отклика на {project_url}")
        
        # Валидация цены для Kwork (минимум 500 руб)
        if price:
            try:
                numeric_price = int(float(price))
                if numeric_price < 500:
                    logger.warning(f"Kwork: цена {numeric_price} ниже минимума. Устанавливаю 500.")
                    price = "500"
                else:
                    price = str(numeric_price)
            except:
                price = "500" # дефолт если ошибка парсинга

        cookies = self._get_kwork_cookies()
        if not cookies["slrememberme"]:
            logger.warning("Kwork: нет кук авторизации")
            return False

        await self.start()
        # Ищем открытую вкладку с этим проектом, чтобы не загружать заново
        page = None
        for target in self.browser.targets:
            if target.type_ == "page" and project_url in target.url:
                page = target
                break
        
        if not page:
            page = await self.browser.get(project_url)
        
        try:
            # Ждем загрузку формы
            await page.scroll_down(300)
            await page.sleep(2)
            await self._take_action_screenshot(page, project_id, "01_project_page")

            # Ищем кнопку "Откликнуться" или "Предложить услугу"
            respond_btn = await page.find("Предложить услугу", timeout=5)
            if not respond_btn:
                respond_btn = await page.find("Откликнуться", timeout=5)
                
            if not respond_btn:
                logger.warning("Kwork: кнопка отклика ('Предложить услугу' / 'Откликнуться') не найдена")
                return False

            await respond_btn.click()
            await page.sleep(random.uniform(3, 5))
            await self._take_action_screenshot(page, project_id, "02_form_opened")

            # Ищем редактор Trumbowyg
            editor = await page.find(".trumbowyg-editor", timeout=4)
            if editor:
                # Экранируем текст через JSON для безопасной вставки в JS
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
                # Фоллбэк на обычную textarea
                textarea = await page.find("textarea[name='description']", timeout=2)
                if textarea:
                    await textarea.send_keys(proposal_text)

            # Бюджет (ID #offer-custom-price)
            if price:
                try:
                    price_input = await page.find("#offer-custom-price", timeout=2)
                    if not price_input:
                        price_input = await page.find("input[type='tel']", timeout=2)
                        
                    if price_input:
                        # Чистим поле и вводим число
                        # Очищаем поле через JS (ищем сначала по ID, потом по типу)
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

            await self._take_action_screenshot(page, project_id, "03_form_filled")

            # Финальная кнопка "Предложить"
            submit_btn = await page.find("Предложить", timeout=3)
            if not submit_btn:
                submit_btn = await page.find("button.kw-button--green", timeout=3)
                  
            if submit_btn:
                # await submit_btn.click() # ПОКА ЗАКОММЕНТИРОВАНО ДЛЯ БЕЗОПАСНОСТИ
                logger.info(f"Kwork: отклик готов. Скриншоты в data/screenshots/{project_id}/")
                return True

            logger.warning("Kwork: не удалось найти кнопку отправки")
            return False

        except Exception as e:
            logger.error(f"Kwork: ошибка отправки Nodriver: {e}")
            return False
        finally:
            await page.close()

    async def send_freelancercom_proposal(
        self,
        project_id: str,
        proposal_text: str,
        price: Optional[str] = None,
    ) -> bool:
        """Отправка бида через официальный REST API Freelancer.com."""
        logger.info(f"Freelancer.com: отправка бида на проект ID {project_id}")
        token = os.getenv("FREELANCER_OAUTH_TOKEN")
        
        if not token:
            logger.warning("Freelancer.com: нет OAUTH токена в .env (нужен FREELANCER_OAUTH_TOKEN)")
            return False
            
        url = "https://www.freelancer.com/api/projects/0.1/bids/"
        headers = {
            "freelancer-oauth-v1": token,
            "Content-Type": "application/json"
        }
        
        # Минимальный payload по документации Freelancer API
        payload = {
            "project_id": int(project_id),
            "amount": float(price) if price else 50.0,
            "period": 7, # Время выполнения в днях
            "milestone_percentage": 100,
            "description": proposal_text
        }
        
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(url, headers=headers, json=payload, timeout=10.0)
                if res.status_code == 200:
                    logger.info("Freelancer.com: бид успешно отправлен ✓")
                    return True
                else:
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
    ) -> bool:
        """Универсальная отправка отклика."""
        if platform == "fl_ru":
            return await self.send_flru_proposal(project_url, project_id, proposal_text, price)
        elif platform == "kwork":
            return await self.send_kwork_proposal(project_url, project_id, proposal_text, price)
        elif platform == "freelancer_com":
            return await self.send_freelancercom_proposal(project_id, proposal_text, price)
        else:
            logger.warning(f"Авто-отправка пока не поддерживается для платформы: {platform}")
            return False
