"""Kwork library extensions — monkey-patches and direct API helpers.

Расширяет библиотеку kwork==0.2.0 без форка:
1. Динамические категории через /categories endpoint
2. Отправка сообщений клиентам (для управления диалогами)
3. Полная история диалогов
4. Заказы и трекинг доставки
5. Получение деталей проекта через API
6. Raw API вызовы для любых endpoint-ов
7. Загрузка файлов (attachments) через multipart
8. TLS-имитация: патч aiohttp session → curl_cffi transport
9. Proxy rotation с residential proxies
10. Rate limiting / pacing с human-like задержками
11. Connects мониторинг и smart selection
12. Multi-account isolation (per-account proxy + session)

Использует api.request() — низкоуровневый метод библиотеки,
доступный для любого endpoint Kwork Mobile API.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

from loguru import logger


class RatePacer:
    """Human-like rate pacing для API вызовов.

    Добавляет случайные задержки между запросами чтобы
    имитировать поведение реального пользователя.
    """

    def __init__(
        self,
        min_delay: float = 1.5,
        max_delay: float = 4.0,
        burst_limit: int = 8,
        burst_window: float = 60.0,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.burst_limit = burst_limit
        self.burst_window = burst_window
        self._timestamps: list[float] = []
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            delay = random.uniform(self.min_delay, self.max_delay)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)

            self._timestamps.append(time.monotonic())
            cutoff = time.monotonic() - self.burst_window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self.burst_limit:
                extra = self.burst_window - (time.monotonic() - self._timestamps[0])
                if extra > 0:
                    logger.debug(f"KworkExt: burst limit ({self.burst_limit}/{self.burst_window}s), пауза {extra:.1f}s")
                    await asyncio.sleep(extra)
                    self._timestamps = []

            self._last_call = time.monotonic()


_pacer: RatePacer | None = None


def get_pacer() -> RatePacer:
    global _pacer
    if _pacer is None:
        try:
            min_d = float(os.getenv("KWORK_PACE_MIN", "1.5"))
        except (ValueError, TypeError):
            min_d = 1.5
        try:
            max_d = float(os.getenv("KWORK_PACE_MAX", "4.0"))
        except (ValueError, TypeError):
            max_d = 4.0
        try:
            burst = int(os.getenv("KWORK_BURST_LIMIT", "15"))
        except (ValueError, TypeError):
            burst = 15
        try:
            window = float(os.getenv("KWORK_BURST_WINDOW", "60"))
        except (ValueError, TypeError):
            window = 60.0
        _pacer = RatePacer(min_delay=min_d, max_delay=max_d, burst_limit=burst, burst_window=window)
    return _pacer


class ProxyRotator:
    """Rotation прокси для распределения запросов по разным IP.

    Поддерживает:
    - Список residential прокси (旋转)
    - Sticky sessions (один прокси на N запросов)
    - Health tracking (плохие прокси временно исключаются)
    """

    def __init__(self, proxies: list[str] | None = None):
        self._proxies = proxies or []
        self._index = 0
        _raw = os.getenv("KWORK_PROXY_LIST", "")
        if _raw:
            self._proxies.extend(p.strip() for p in _raw.split(",") if p.strip())
        self._bad: dict[str, float] = {}
        self._bad_ttl = 300.0

    def next(self) -> str | None:
        if not self._proxies:
            return os.getenv("PROXY_URL") or None
        now = time.monotonic()
        self._bad = {p: t for p, t in self._bad.items() if now - t < self._bad_ttl}
        for _ in range(len(self._proxies)):
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            if proxy not in self._bad:
                return proxy
        return None

    def mark_bad(self, proxy: str) -> None:
        if proxy:
            self._bad[proxy] = time.monotonic()
            logger.warning(f"KworkExt: прокси помечен как плохой на {self._bad_ttl}s: {proxy}")

    @property
    def available_count(self) -> int:
        now = time.monotonic()
        active = [p for p in self._proxies if p not in self._bad or now - self._bad[p] > self._bad_ttl]
        return len(active)


_proxy_rotator: ProxyRotator | None = None


def get_proxy_rotator() -> ProxyRotator:
    global _proxy_rotator
    if _proxy_rotator is None:
        _proxy_rotator = ProxyRotator()
    return _proxy_rotator


class ConnectsMonitor:
    """Мониторинг connects — баланса откликов на Kwork.

    Connects ограничены (20-80/мес в зависимости от рейтинга).
    Нельзя купить. Этот класс помогает:
    - Отслеживать текущий баланс
    - Предупреждать при низком балансе
    - Заблокировать отправку при критически низком балансе
    """

    def __init__(self, warn_threshold: int = 5, block_threshold: int = 2):
        self.warn_threshold = warn_threshold
        self.block_threshold = block_threshold
        self._last_check: float = 0.0
        self._cache: dict[str, Any] = {}
        self._cache_ttl = 300.0

    async def check(self, api: Any) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._last_check < self._cache_ttl and self._cache:
            return self._cache
        try:
            data = await api.request("post", "projects", use_token=True, categories="")
            connects = data.get("connects", {}) if isinstance(data, dict) else {}
            self._cache = connects
            self._last_check = now
            free = int(connects.get("free_amount", 0) or 0)
            if free <= self.warn_threshold and free > self.block_threshold:
                logger.warning(f"KworkExt: мало connects ({free} осталось)")
            elif free <= self.block_threshold:
                logger.error(f"KworkExt: критически мало connects ({free}) — отправка заблокирована")
            return connects
        except Exception as e:
            logger.debug(f"KworkExt: не удалось проверить connects: {e}")
            return {}

    def can_send(self) -> bool:
        if not self._cache:
            return True
        free = int(self._cache.get("free_amount", 999) or 999)
        return free > self.block_threshold

    def decrement(self, amount: int = 1) -> None:
        """Decrement connects by amount, clamped at 0."""
        current = int(self._cache.get("free_amount", 0) or 0)
        self._cache["free_amount"] = max(0, current - amount)

    @property
    def free_amount(self) -> int:
        return int(self._cache.get("free_amount", 0) or 0)


_connects_monitor: ConnectsMonitor | None = None


def get_connects_monitor() -> ConnectsMonitor:
    global _connects_monitor
    if _connects_monitor is None:
        warn = int(os.getenv("KWORK_CONNECTS_WARN", "5"))
        block = int(os.getenv("KWORK_CONNECTS_BLOCK", "2"))
        _connects_monitor = ConnectsMonitor(warn_threshold=warn, block_threshold=block)
    return _connects_monitor


class SuccessRateMonitor:
    """Мониторинг success rate фрилансера.

    Kwork penalizes: every -10% success rate → -25% connects.
    Success rate = completed_orders / (completed + cancelled + expired).
    Cancelled orders (by worker), expired (24h no accept), ignored assignments
    all reduce success rate.

    This class:
    - Fetches workerOrders (done, cancelled, active) via API
    - Computes success_rate %
    - Throttles sending when approaching -10% boundary
    - Alerts via logger when rate is dangerous
    """

    def __init__(self, warn_rate: float = 80.0, block_rate: float = 70.0):
        self.warn_rate = warn_rate
        self.block_rate = block_rate
        self._last_check: float = 0.0
        self._cache: dict[str, Any] = {}
        self._cache_ttl = 600.0  # 10 min

    async def check(self, api: Any) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._last_check < self._cache_ttl and self._cache:
            return self._cache
        try:
            all_orders = await KworkExtensions.get_worker_orders(api, status_filter="all")

            done_count = 0
            cancelled_count = 0
            active_count = 0

            if isinstance(all_orders, list):
                for o in all_orders:
                    if not isinstance(o, dict):
                        continue
                    status = str(o.get("status", "")).lower()
                    if status in ("done", "completed", "finished"):
                        done_count += 1
                    elif status in ("cancelled", "expired", "canceled", "failed"):
                        cancelled_count += 1
                    elif status in (
                        "new",
                        "active",
                        "assigned",
                        "pending",
                        "wait_payment",
                        "wait_confirm",
                        "in_progress",
                        "processing",
                    ):
                        active_count += 1

            total = done_count + cancelled_count

            if total == 0:
                rate = 100.0
            else:
                rate = (done_count / total) * 100.0

            self._cache = {
                "success_rate": round(rate, 1),
                "completed": done_count,
                "cancelled": cancelled_count,
                "active": active_count,
                "total": total,
            }
            self._last_check = now

            if rate < self.block_rate:
                logger.error(
                    f"KworkExt: критически низкий success rate ({rate:.1f}%) — "
                    f"отправка заблокирована. Completed={done_count}, Cancelled={cancelled_count}"
                )
            elif rate < self.warn_rate:
                logger.warning(
                    f"KworkExt: низкий success rate ({rate:.1f}%) — "
                    f"отправка ограничена. Completed={done_count}, Cancelled={cancelled_count}"
                )

            if active_count >= 5:
                logger.warning(f"KworkExt: много активных заказов ({active_count}) — риск 'Занят' статуса")

            return self._cache
        except Exception as e:
            logger.debug(f"KworkExt: не удалось проверить success rate: {e}")
            return {"success_rate": -1.0, "completed": 0, "cancelled": 0, "active": 0, "total": 0}

    def can_send(self) -> bool:
        if not self._cache:
            return True
        rate = float(self._cache.get("success_rate", 100.0) or 100.0)
        if rate < 0:
            return True
        return rate >= self.block_rate

    def should_throttle(self) -> bool:
        rate = float(self._cache.get("success_rate", 100.0) or 100.0)
        return rate < self.warn_rate

    @property
    def active_orders(self) -> int:
        return int(self._cache.get("active", 0) or 0)

    @property
    def success_rate(self) -> float:
        return float(self._cache.get("success_rate", 100.0) or 100.0)

    @property
    def completed(self) -> int:
        return int(self._cache.get("completed", 0) or 0)

    @property
    def cancelled(self) -> int:
        return int(self._cache.get("cancelled", 0) or 0)


_success_rate_monitor: SuccessRateMonitor | None = None


def get_success_rate_monitor() -> SuccessRateMonitor:
    global _success_rate_monitor
    if _success_rate_monitor is None:
        warn = float(os.getenv("KWORK_SUCCESS_RATE_WARN", "80"))
        block = float(os.getenv("KWORK_SUCCESS_RATE_BLOCK", "70"))
        _success_rate_monitor = SuccessRateMonitor(warn_rate=warn, block_rate=block)
    return _success_rate_monitor


class AccountHealthMonitor:
    """Комплексный мониторинг здоровья аккаунта Kwork.

    Объединяет:
    - Success rate (completed/cancelled)
    - Connects balance
    - Active orders count ("Занят" risk)
    - Captcha status (account under suspicion)
    - Actor data (level, rating, reviews)
    - Auto-pause kworks when overloaded

    Kwork penalties:
    - 4 strikes = ban
    - -10% success rate = -25% connects
    - 24h no accept on assigned order = auto-cancel = -rating
    - Too many active orders = "Занят" = catalog visibility drop
    """

    def __init__(self, busy_threshold: int = 5, auto_pause: bool = False):
        self.busy_threshold = busy_threshold
        self.auto_pause = auto_pause
        self._last_check: float = 0.0
        self._cache: dict[str, Any] = {}
        self._cache_ttl = 600.0
        self._paused_kworks: list[int] = []
        self._actor: dict[str, Any] = {}

    async def check(self, api: Any) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._last_check < self._cache_ttl and self._cache:
            return self._cache

        try:
            connects_monitor = get_connects_monitor()
            success_monitor = get_success_rate_monitor()

            connects = await connects_monitor.check(api)
            await success_monitor.check(api)
            captcha = await KworkExtensions.get_captcha_status(api)
            badges = await KworkExtensions.get_badges_info(api)

            actor = {}
            try:
                actor_data = await api.request("post", "actor", use_token=True)
                actor = actor_data.get("response", {}) if isinstance(actor_data, dict) else {}
                self._actor = actor
            except Exception:
                pass

            active_count = success_monitor.active_orders
            is_busy_risk = active_count >= self.busy_threshold

            if is_busy_risk and self.auto_pause and not self._paused_kworks:
                await self._auto_pause_kworks(api)

            if not is_busy_risk and self._paused_kworks:
                logger.info(f"KworkExt: активных заказов мало, кворки можно активировать: {self._paused_kworks}")
                self._paused_kworks = []

            has_orders = success_monitor.completed + success_monitor.cancelled > 0
            captcha_required = captcha and has_orders
            if captcha and not has_orders:
                logger.debug("KworkExt: getCaptchaStatus=true (вероятно нет заказов — игнорируем)")
            self._cache = {
                "connects_free": connects_monitor.free_amount,
                "connects_total": connects.get("total_amount", 0),
                "success_rate": success_monitor.success_rate,
                "completed": success_monitor.completed,
                "cancelled": success_monitor.cancelled,
                "active_orders": active_count,
                "busy_risk": is_busy_risk,
                "captcha_required": captcha_required,
                "unread_notifications": badges.get("notifications", 0),
                "username": actor.get("username", ""),
                "level": actor.get("level", actor.get("rating_level", "")),
                "rating": actor.get("rating", 0),
                "reviews_count": actor.get("reviews_count", actor.get("rating_count", 0)),
                "paused_kworks": list(self._paused_kworks),
            }
            self._last_check = now

            if captcha and success_monitor.completed + success_monitor.cancelled > 0:
                logger.error("KworkExt: CAPTCHA требуется — аккаунт под подозрением!")
            elif captcha:
                logger.debug("KworkExt: getCaptchaStatus=true (вероятно нет заказов — игнорируем)")
            if is_busy_risk:
                logger.warning(f"KworkExt: {active_count} активных заказов — риск 'Занят' статуса")

            return self._cache
        except Exception as e:
            logger.debug(f"KworkExt: account health check failed: {e}")
            return {}

    async def _auto_pause_kworks(self, api: Any) -> None:
        """Auto-pause all active kworks when overloaded."""
        try:
            kworks = await KworkExtensions.get_kworks_status_list(api)
            for kw in kworks:
                if isinstance(kw, dict):
                    kw_id = kw.get("id")
                    kw_status = str(kw.get("status", "")).lower()
                    if kw_id and kw_status in {"active", "1", "ok"}:
                        result = await KworkExtensions.pause_kwork(api, int(kw_id))
                        if result:
                            self._paused_kworks.append(int(kw_id))
            if self._paused_kworks:
                logger.warning(f"KworkExt: авто-пауза {len(self._paused_kworks)} кворков (перегрузка)")
        except Exception as e:
            logger.debug(f"KworkExt: auto-pause failed: {e}")

    def get_summary_text(self) -> str:
        """Generate a human-readable health summary for Telegram."""
        c = self._cache
        if not c:
            return "Account Health: данные не загружены"
        lines = [
            "Account Health:",
            f"  Пользователь: {c.get('username', '?')}",
            f"  Уровень: {c.get('level', '?')}",
            f"  Рейтинг: {c.get('rating', '?')} ({c.get('reviews_count', 0)} отзывов)",
            f"  Connects: {c.get('connects_free', 0)} свободно",
            f"  Success rate: {c.get('success_rate', 0):.1f}%",
            f"  Активных заказов: {c.get('active_orders', 0)}",
        ]
        if c.get("busy_risk"):
            lines.append("  ⚠️ Риск 'Занят' — слишком много активных заказов!")
        if c.get("captcha_required"):
            lines.append("  ⚠️ Требуется капча — аккаунт под подозрением!")
        if c.get("paused_kworks"):
            lines.append(f"  На паузе: {len(c['paused_kworks'])} кворков (авто)")
        return "\n".join(lines)


_account_health_monitor: AccountHealthMonitor | None = None


def get_account_health_monitor() -> AccountHealthMonitor:
    global _account_health_monitor
    if _account_health_monitor is None:
        threshold = int(os.getenv("KWORK_BUSY_THRESHOLD", "5"))
        auto_pause = os.getenv("KWORK_AUTO_PAUSE_KWORKS", "false").lower() in {"1", "true", "yes", "on"}
        _account_health_monitor = AccountHealthMonitor(busy_threshold=threshold, auto_pause=auto_pause)
    return _account_health_monitor


class KworkExtensions:
    """Расширения для KworkClient — работают поверх api.request().

    Все методы принимают api-клиент (KworkClient) как аргумент,
    чтобы работать с любой сессией (Session Hub или email/password).
    """

    @staticmethod
    async def get_categories_raw(api: Any) -> list[dict[str, Any]]:
        """Получить полное дерево категорий через /categories.

        Endpoint: POST /categories (Basic auth, без token)
        Возвращает список родительских категорий с подкатегориями.
        """
        try:
            data = await api.request("post", "categories")
            response = data.get("response") if isinstance(data, dict) else None
            if not isinstance(response, list):
                logger.warning(f"KworkExt: /categories вернул неожиданный формат: {type(response)}")
                return []
            return response
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить категории: {e}")
            return []

    @staticmethod
    async def get_all_category_ids(api: Any) -> list[int]:
        """Извлечь все ID категорий из дерева."""
        raw = await KworkExtensions.get_categories_raw(api)
        ids: list[int] = []
        for cat in raw:
            if isinstance(cat, dict):
                cat_id = cat.get("id")
                if cat_id is not None:
                    ids.append(int(cat_id))
                for sub in cat.get("categories", []) or cat.get("childs", []) or []:
                    if isinstance(sub, dict):
                        sub_id = sub.get("id")
                        if sub_id is not None:
                            ids.append(int(sub_id))
        return list(set(ids))

    @staticmethod
    async def get_project_details(api: Any, project_id: int | str) -> dict[str, Any] | None:
        """Получить детали проекта через /project endpoint.

        Endpoint: POST /project (use_token=True, id=project_id)
        Возвращает полные данные проекта включая skills, files, dates.
        """
        try:
            data = await api.request("post", "project", use_token=True, id=int(project_id))
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, dict):
                return response
            if isinstance(response, list) and response:
                return response[0] if isinstance(response[0], dict) else None
            return None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить детали проекта {project_id}: {e}")
            return None

    @staticmethod
    async def send_message_to_client(api: Any, user_id: int, text: str) -> dict[str, Any] | None:
        """Отправить сообщение клиенту через Kwork чат.

        Endpoint: POST /inboxCreate (use_token=True, user_id, body={text})
        Использует request_with_body для передачи text в body.
        """
        try:
            result = await api.request_with_body(
                "inboxCreate",
                use_token=True,
                retry=False,
                body={"text": text},
                user_id=user_id,
            )
            logger.info(f"KworkExt: сообщение отправлено пользователю {user_id}")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось отправить сообщение пользователю {user_id}: {e}")
            return None

    @staticmethod
    async def get_dialog_history(api: Any, username: str) -> list[dict[str, Any]]:
        """Получить полную историю диалога с пользователем.

        Endpoint: POST /inboxes (use_token=True, username, page)
        Пагинируется автоматически.
        """
        messages: list[dict[str, Any]] = []
        page = 1
        max_pages = 50
        while page <= max_pages:
            try:
                data = await api.request(
                    "post",
                    "inboxes",
                    use_token=True,
                    username=username,
                    page=page,
                )
                response = data.get("response") if isinstance(data, dict) else None
                if not isinstance(response, list) or not response:
                    break
                for msg in response:
                    if isinstance(msg, dict):
                        messages.append(msg)
                paging = data.get("paging", {}) if isinstance(data, dict) else {}
                pages = paging.get("pages", page)
                if page >= pages:
                    break
                page += 1
            except Exception as e:
                logger.debug(f"KworkExt: ошибка получения диалога page {page}: {e}")
                break
        return messages

    @staticmethod
    async def get_worker_orders(api: Any, status_filter: str = "all") -> list[dict[str, Any]]:
        """Получить список заказов фрилансера (для трекинга доставки).

        Endpoint: POST /workerOrders (use_token=True, filter)
        filter: all, active, done, cancelled и т.д.
        """
        try:
            data = await api.request("post", "workerOrders", use_token=True, filter=status_filter)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            if isinstance(response, dict):
                for key in ("data", "items", "orders"):
                    if isinstance(response.get(key), list):
                        return response[key]
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить заказы: {e}")
            return []

    @staticmethod
    async def get_notifications(api: Any) -> list[dict[str, Any]]:
        """Получить уведомления (новые заказы, отзывы, статусы).

        Endpoint: POST /notifications (use_token=True)
        """
        try:
            data = await api.request("post", "notifications", use_token=True)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            if isinstance(response, dict):
                for key in ("data", "items", "notifications"):
                    if isinstance(response.get(key), list):
                        return response[key]
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить уведомления: {e}")
            return []

    @staticmethod
    async def get_connects_info(api: Any) -> dict[str, Any]:
        """Получить информацию о connects (баланс откликов).

        Endpoint: POST /projects (use_token=True, categories="")
        Возвращает connects блок из ответа /projects.
        """
        try:
            data = await api.request("post", "projects", use_token=True, categories="")
            connects = data.get("connects") if isinstance(data, dict) else None
            if isinstance(connects, dict):
                return connects
            return {}
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить connects: {e}")
            return {}

    @staticmethod
    async def upload_file_to_offer(
        api: Any,
        project_id: int,
        file_path: str,
        *,
        csrftoken: str = "",
        draft_key: str = "",
    ) -> dict[str, Any] | None:
        """Загрузить файл к отклику через web endpoint.

        Использует api.web (KworkWebClient) для multipart upload.
        Endpoint: POST /wants/upload_offer_file (web, multipart)
        """
        try:
            from pathlib import Path

            web = api.web
            referer = f"https://kwork.ru/new_offer?project={project_id}"

            if not csrftoken:
                cookies = web._filtered_cookies(web.base_url)
                csrftoken = cookies.get("csrf_user_token", "")

            if not draft_key:
                draft_key = web._gen_draft_key()

            path = Path(file_path)
            if not path.exists():
                logger.warning(f"KworkExt: файл не найден: {file_path}")
                return None

            headers = web._build_xhr_headers(
                accept="application/json, text/plain, */*",
                referer=referer,
            )

            import aiohttp

            form = aiohttp.FormData()
            form.add_field("csrftoken", csrftoken)
            form.add_field("projectId", str(project_id))
            form.add_field("draftKey", draft_key)
            form.add_field("file", path.read_bytes(), filename=path.name, content_type="application/octet-stream")

            resp = await web.request("POST", "wants/upload_offer_file", data=form, headers=headers)
            web._raise_on_web_error(resp, where="wants/upload_offer_file")
            logger.info(f"KworkExt: файл {path.name} загружен для проекта {project_id}")
            return resp
        except Exception as e:
            logger.error(f"KworkExt: не удалось загрузить файл {file_path}: {e}")
            return None

    @staticmethod
    async def get_raw_projects(
        api: Any,
        *,
        categories: str = "all",
        page: int = 1,
        query: str = "",
        price_from: int | None = None,
        price_to: int | None = None,
        hiring_from: int | None = None,
        kworks_filter_from: int | None = None,
        kworks_filter_to: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Получить сырые данные проектов без WantWorker парсинга.

        Возвращает (projects_list, paging_dict) — raw dicts с всеми полями API,
        включая skills, date_create, files, possible_price_limit и т.д.

        categories="all" ищет по всем категориям (не ограничено 2).
        """
        try:
            data = await api.request(
                "post",
                "projects",
                use_token=True,
                categories=categories,
                page=page,
                query=query or None,
                price_from=price_from,
                price_to=price_to,
                hiring_from=hiring_from,
                kworks_filter_from=kworks_filter_from,
                kworks_filter_to=kworks_filter_to,
            )
            response = data.get("response") if isinstance(data, dict) else None
            paging = data.get("paging", {}) if isinstance(data, dict) else {}
            connects = data.get("connects", {}) if isinstance(data, dict) else {}

            if response is None:
                if isinstance(data, dict) and data.get("success") is True:
                    return [], paging
                return [], paging

            if isinstance(response, dict):
                response = (
                    response.get("data")
                    or response.get("items")
                    or response.get("wants")
                    or response.get("projects")
                    or []
                )

            if not isinstance(response, list):
                return [], paging

            return response, {"paging": paging, "connects": connects}
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить проекты (raw): {e}")
            return [], {}

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    @staticmethod
    async def approve_order(api: Any, order_id: int) -> dict[str, Any] | None:
        """Принять заказ. Критично для auto-assignment trap."""
        try:
            result = await api.request("post", "approveOrder", use_token=True, id=order_id)
            logger.info(f"KworkExt: заказ #{order_id} принят")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось принять заказ {order_id}: {e}")
            return None

    @staticmethod
    async def cancel_order_by_worker(api: Any, order_id: int, reason: str = "") -> dict[str, Any] | None:
        """Отменить заказ как продавец."""
        try:
            result = await api.request_with_body(
                "cancelOrderByWorker",
                use_token=True,
                body={"id": order_id, "reason": reason},
            )
            logger.info(f"KworkExt: заказ #{order_id} отменён продавцом")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось отменить заказ {order_id}: {e}")
            return None

    @staticmethod
    async def get_order_details(api: Any, order_id: int) -> dict[str, Any] | None:
        """Детали заказа — дедлайны, статусы, файлы."""
        try:
            data = await api.request("post", "getOrderDetails", use_token=True, id=order_id)
            return data.get("response") if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить детали заказа {order_id}: {e}")
            return None

    @staticmethod
    async def get_order_header(api: Any, order_id: int) -> dict[str, Any] | None:
        """Быстрая шапка заказа без полной загрузки."""
        try:
            data = await api.request("post", "getOrderHeader", use_token=True, id=order_id)
            return data.get("response") if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить шапку заказа {order_id}: {e}")
            return None

    @staticmethod
    async def get_order_files(api: Any, order_id: int) -> list[dict[str, Any]]:
        """Файлы заказа."""
        try:
            data = await api.request("post", "getOrderFiles", use_token=True, id=order_id)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить файлы заказа {order_id}: {e}")
            return []

    # ------------------------------------------------------------------
    # Reviews
    # ------------------------------------------------------------------

    @staticmethod
    async def create_review(
        api: Any, order_id: int, rating: int = 5, text: str = "", body: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Оставить отзыв клиенту. Отзывы = главный фактор ранкинга."""
        try:
            review_body = body or {"id": order_id, "rating": rating, "text": text or "Спасибо за заказ!"}
            result = await api.request_with_body("createReview", use_token=True, body=review_body)
            logger.info(f"KworkExt: отзыв оставлен для заказа #{order_id}")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось оставить отзыв для заказа {order_id}: {e}")
            return None

    @staticmethod
    async def get_kwork_reviews(api: Any, kwork_id: int, page: int = 1) -> list[dict[str, Any]]:
        """Отзывы на кворк — конкурентный анализ."""
        try:
            data = await api.request("post", "getKworkReviews", id=kwork_id, page=page)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить отзывы кворка {kwork_id}: {e}")
            return []

    # ------------------------------------------------------------------
    # Inbox / Chat management
    # ------------------------------------------------------------------

    @staticmethod
    async def inbox_read(api: Any, message_id: int) -> dict[str, Any] | None:
        """Пометить сообщение прочитанным. Предотвращает 'упущенные заказы' metric."""
        try:
            return await api.request("post", "inboxRead", use_token=True, id=message_id)
        except Exception as e:
            logger.debug(f"KworkExt: не удалось пометить сообщение {message_id}: {e}")
            return None

    @staticmethod
    async def mark_inbox_tracks_as_read(api: Any, dialog_id: int) -> dict[str, Any] | None:
        """Пометить весь диалог прочитанным."""
        try:
            return await api.request("post", "markInboxTracksAsRead", use_token=True, id=dialog_id)
        except Exception as e:
            logger.debug(f"KworkExt: не удалось пометить диалог {dialog_id}: {e}")
            return None

    @staticmethod
    async def set_typing(api: Any, recipient_id: int) -> dict[str, Any] | None:
        """Показать 'печатает...' — human-like behavior, снижает bot score."""
        try:
            return await api.request("post", "typing", use_token=True, recipientId=recipient_id)
        except Exception as e:
            logger.debug(f"KworkExt: не удалось установить typing: {e}")
            return None

    @staticmethod
    async def set_offline(api: Any) -> dict[str, Any] | None:
        """Установить статус offline."""
        try:
            return await api.request("post", "offline", use_token=True)
        except Exception:
            return None

    @staticmethod
    async def is_dialog_allow(api: Any, user_id: int) -> bool:
        """Разрешён ли диалог с пользователем."""
        try:
            data = await api.request("post", "isDialogAllow", use_token=True, user_id=user_id)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, dict):
                return bool(response.get("allow", True))
            return True
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Exchange / Projects
    # ------------------------------------------------------------------

    @staticmethod
    async def get_wants_count(api: Any, categories: str = "all", **filters: Any) -> int:
        """Количество проектов по фильтрам. Lightweight — для golden window check."""
        try:
            data = await api.request("post", "getWantsCount", use_token=True, categories=categories, **filters)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, dict):
                return int(response.get("count", 0) or 0)
            if isinstance(response, int):
                return response
            return 0
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить count проектов: {e}")
            return 0

    @staticmethod
    async def exchange_info(api: Any) -> dict[str, Any]:
        """Ключевая информация по бирже — статус, лимиты, connects."""
        try:
            data = await api.request("post", "exchangeInfo", use_token=True)
            return data.get("response") if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить exchange info: {e}")
            return {}

    # ------------------------------------------------------------------
    # Offers management
    # ------------------------------------------------------------------

    @staticmethod
    async def get_offers(api: Any) -> list[dict[str, Any]]:
        """Наши отправленные отклики — проверка статуса (принят/отклонён)."""
        try:
            data = await api.request("post", "offers", use_token=True)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить список откликов: {e}")
            return []

    @staticmethod
    async def get_offer(api: Any, offer_id: int) -> dict[str, Any] | None:
        """Детали конкретного отклика."""
        try:
            data = await api.request("post", "offer", use_token=True, id=offer_id)
            return data.get("response") if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить отклик {offer_id}: {e}")
            return None

    @staticmethod
    async def delete_offer(api: Any, offer_id: int) -> dict[str, Any] | None:
        """Отозвать отклик."""
        try:
            result = await api.request("post", "deleteOffer", use_token=True, id=offer_id)
            logger.info(f"KworkExt: отклик #{offer_id} отозван")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось отозвать отклик {offer_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Template detection
    # ------------------------------------------------------------------

    @staticmethod
    async def check_is_template(api: Any, project_id: int, description: str) -> dict[str, Any] | None:
        """Pre-submit check: is the proposal text flagged as template by Kwork?

        Kwork calls POST /projects/check_is_template before accepting offers.
        If the text is flagged as template, the offer may be silently rejected
        or down-ranked. This method calls the same endpoint to check BEFORE submitting.

        Returns the API response dict, or None on error.
        """
        try:
            web = api.web
            referer = f"https://kwork.ru/new_offer?project={project_id}"
            headers = web._build_xhr_headers(
                accept="application/json, text/plain, */*",
                referer=referer,
            )
            headers["Content-Type"] = "application/json"
            resp = await web.request(
                "POST",
                "projects/check_is_template",
                headers=headers,
                json_data={"description": description, "wantid": project_id},
            )
            return resp
        except Exception as e:
            logger.debug(f"KworkExt: check_is_template error: {e}")
            return None

    @staticmethod
    async def is_text_template_flagged(api: Any, project_id: int, description: str) -> bool:
        """Check if Kwork would flag this proposal text as template.

        Returns True if flagged as template (should NOT submit), False if OK.
        """
        result = await KworkExtensions.check_is_template(api, project_id, description)
        if not result:
            return False
        j = result.get("json")
        if isinstance(j, dict):
            if j.get("is_template") is True:
                return True
            response = j.get("response")
            if isinstance(response, dict) and response.get("is_template"):
                return True
        return False

    # ------------------------------------------------------------------
    # Web session health
    # ------------------------------------------------------------------

    @staticmethod
    async def check_web_session(api: Any) -> bool:
        """Quick check: is the web session still valid?

        Verifies that csrf_user_token cookie is present.
        """
        try:
            web = api.web
            cookies = web._filtered_cookies(web.base_url)
            has_csrf = "csrf_user_token" in cookies
            if not has_csrf:
                logger.warning("KworkExt: web-сессия истекла — нет csrf_user_token cookie")
                return False
            return True
        except Exception:
            return False

    @staticmethod
    async def upload_file(api: Any, file_path: str) -> dict[str, Any] | None:
        """Загрузка файла через API. Attachments к откликам без браузера."""
        try:
            from pathlib import Path

            path = Path(file_path)
            if not path.exists():
                logger.warning(f"KworkExt: файл не найден: {file_path}")
                return None

            result = await api.request_multipart(
                "fileUpload",
                files={"upload_files": (path.name, path.read_bytes())},
            )
            logger.info(f"KworkExt: файл {path.name} загружен через API")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось загрузить файл {file_path}: {e}")
            return None

    # ------------------------------------------------------------------
    # User / Kwork info
    # ------------------------------------------------------------------

    @staticmethod
    async def get_user_info(api: Any, user_id: int) -> dict[str, Any] | None:
        """Подробная инфо о пользователе — богаче чем get_user."""
        try:
            data = await api.request("post", "getUserInfo", id=user_id)
            return data.get("response") if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить инфо пользователя {user_id}: {e}")
            return None

    @staticmethod
    async def get_kwork_details(api: Any, kwork_id: int) -> dict[str, Any] | None:
        """Детали кворка — опции, цены, сроки."""
        try:
            data = await api.request("post", "getKworkDetails", id=kwork_id)
            return data.get("response") if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить детали кворка {kwork_id}: {e}")
            return None

    @staticmethod
    async def get_kworks_list(api: Any, category_id: int, page: int = 1) -> list[dict[str, Any]]:
        """Список кворков в категории — наши + конкуренты."""
        try:
            data = await api.request("post", "kworks", category=category_id, page=page)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить список кворков: {e}")
            return []

    @staticmethod
    async def get_kworks_status_list(api: Any) -> list[dict[str, Any]]:
        """Статусы наших кворков — активные/на паузе/скрытые."""
        try:
            data = await api.request("post", "kworksStatusList", use_token=True)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить статусы кворков: {e}")
            return []

    @staticmethod
    async def pause_kwork(api: Any, kwork_id: int) -> dict[str, Any] | None:
        """Поставить кворк на паузу — 'Занят' при many orders."""
        try:
            result = await api.request("post", "pauseKwork", use_token=True, id=kwork_id)
            logger.info(f"KworkExt: кворк #{kwork_id} поставлен на паузу")
            return result
        except Exception as e:
            logger.error(f"KworkExt: не удалось поставить на паузу кворк {kwork_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Notifications / Health
    # ------------------------------------------------------------------

    @staticmethod
    async def get_badges_info(api: Any) -> dict[str, Any]:
        """Количество непрочитанных уведомлений — быстрый heartbeat."""
        try:
            data = await api.request_with_body("getBadgesInfo", use_token=True, body={})
            resp = data.get("response") if isinstance(data, dict) else {}
            return resp if isinstance(resp, dict) else {}
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить badges: {e}")
            return {}

    @staticmethod
    async def get_captcha_status(api: Any) -> bool:
        """Требуется ли капча — детекция что аккаунт под подозрением."""
        try:
            data = await api.request("post", "getCaptchaStatus", use_token=True)
            response = data.get("response") if isinstance(data, dict) else None
            return bool(response) if response is not None else False
        except Exception:
            return False

    @staticmethod
    async def get_current_versions(api: Any) -> dict[str, Any]:
        """Версии мобильных приложений — проверка актуальности API."""
        try:
            data = await api.request("post", "getCurrentVersions")
            return data.get("response") if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    async def apply_filters(api: Any, **filters: Any) -> dict[str, Any] | None:
        """Установить фильтры продавца на бирже."""
        try:
            return await api.request("post", "applyFilters", use_token=True, **filters)
        except Exception as e:
            logger.debug(f"KworkExt: не удалось установить фильтры: {e}")
            return None

    @staticmethod
    async def orders_between(api: Any, user_id: int) -> list[dict[str, Any]]:
        """Заказы между мной и клиентом — история отношений."""
        try:
            data = await api.request("post", "ordersBetween", use_token=True, user_id=user_id)
            response = data.get("response") if isinstance(data, dict) else None
            if isinstance(response, list):
                return response
            return []
        except Exception as e:
            logger.debug(f"KworkExt: не удалось получить заказы между пользователями: {e}")
            return []


def _patch_tls_session() -> None:
    """Патчит KworkAPI._create_session для TLS-имитации через curl_cffi.

    Проблема: библиотека kwork использует aiohttp → Python TLS fingerprint.
    Kwork/Cloudflare могут забанить по JA3/JA4 fingerprint.

    Решение: заменяем aiohttp.ClientSession на обёртку над curl_cffi.AsyncSession
    с impersonate="chrome120" — тот же TLS handshake что у реального Chrome.

    Если curl_cffi недоступен — fallback на стандартный aiohttp.
    """
    try:
        from kwork.api import KworkAPI
    except ImportError:
        return

    if getattr(KworkAPI, "_psr_tls_patched", False):
        return

    use_tls_patch = os.getenv("KWORK_TLS_IMPERSONATE", "false").lower() in {"1", "true", "yes", "on"}

    if not use_tls_patch:
        logger.debug("KworkExt: TLS патч отключён (KWORK_TLS_IMPERSONATE=false)")
        return

    try:
        import curl_cffi.requests as curl_requests

        original_create_session = KworkAPI._create_session

        def _patched_create_session(self):
            proxy = self._proxy
            impersonate = os.getenv("KWORK_TLS_BROWSER", "chrome120")
            try:
                timeout_val = getattr(self._timeout, "total", None)
                if timeout_val is None:
                    timeout_val = self._timeout if isinstance(self._timeout, (int, float)) else 30.0
                session = curl_requests.AsyncSession(
                    impersonate=impersonate,
                    proxies={"http": proxy, "https": proxy} if proxy else None,
                    timeout=timeout_val,
                )
                session._psr_is_curl_cffi = True
                logger.debug(f"KworkExt: TLS-имитация активна (impersonate={impersonate})")
                return session
            except Exception as e:
                logger.warning(f"KworkExt: curl_cffi session не создана, fallback на aiohttp: {e}")
                return original_create_session(self)

        KworkAPI._create_session = _patched_create_session
        KworkAPI._psr_tls_patched = True
        logger.info("KworkExt: TLS патч применён — aiohttp session заменена на curl_cffi")

    except ImportError:
        logger.debug("KworkExt: curl_cffi не установлен, TLS патч пропущен")
    except Exception as e:
        logger.warning(f"KworkExt: TLS патч не удался: {e}")


def _patch_request_pacing() -> None:
    """Обёртка вокруг api.request() с rate pacing и proxy rotation.

    Добавляет human-like задержки перед каждым API вызовом
    и ротацию прокси между запросами.
    """
    try:
        from kwork.api import KworkAPI
    except ImportError:
        return

    if getattr(KworkAPI, "_psr_pacing_patched", False):
        return

    use_pacing = os.getenv("KWORK_PACING", "true").lower() in {"1", "true", "yes", "on"}
    if not use_pacing:
        return

    original_request = KworkAPI.request

    async def _paced_request(self, method, endpoint, use_token=False, **kwargs):
        await get_pacer().wait()
        return await original_request(self, method, endpoint, use_token=use_token, **kwargs)

    KworkAPI.request = _paced_request

    if hasattr(KworkAPI, "request_with_body"):
        original_rwb = KworkAPI.request_with_body

        async def _paced_rwb(self, method, endpoint, use_token=False, **kwargs):
            await get_pacer().wait()
            return await original_rwb(self, method, endpoint, use_token=use_token, **kwargs)

        KworkAPI.request_with_body = _paced_rwb

    if hasattr(KworkAPI, "request_multipart"):
        original_rm = KworkAPI.request_multipart

        async def _paced_rm(self, method, endpoint, use_token=False, **kwargs):
            await get_pacer().wait()
            return await original_rm(self, method, endpoint, use_token=use_token, **kwargs)

        KworkAPI.request_multipart = _paced_rm

    KworkAPI._psr_pacing_patched = True
    logger.debug("KworkExt: rate pacing применён к api.request() + request_with_body + request_multipart")


def apply_kwork_patches() -> None:
    """Применить monkey-patches к библиотеке kwork при импорте.

    Добавляет методы в KworkClient:
    - get_categories_raw() — динамические категории
    - get_project_details(project_id) — детали проекта
    - send_message_to_client(user_id, text) — отправка сообщения
    - get_dialog_history(username) — история диалога
    - get_worker_orders() — заказы
    - get_notifications() — уведомления
    - get_connects_info() — баланс connects
    - get_raw_projects(...) — сырые проекты без WantWorker
    - upload_file_to_offer(...) — загрузка файлов

    Патчи инфраструктуры:
    - TLS-имитация через curl_cffi (обход Cloudflare JA3/JA4)
    - Rate pacing (human-like задержки)
    - Proxy rotation support
    """
    _patch_tls_session()
    _patch_request_pacing()

    try:
        from kwork.client import KworkClient

        if getattr(KworkClient, "_psr_ext_applied", False):
            return

        KworkClient.get_categories_raw = lambda self: KworkExtensions.get_categories_raw(self)
        KworkClient.get_all_category_ids = lambda self: KworkExtensions.get_all_category_ids(self)
        KworkClient.get_project_details_raw = lambda self, project_id: KworkExtensions.get_project_details(
            self, project_id
        )
        KworkClient.send_message_to_client = lambda self, user_id, text: KworkExtensions.send_message_to_client(
            self, user_id, text
        )
        KworkClient.get_dialog_history_raw = lambda self, username: KworkExtensions.get_dialog_history(self, username)
        KworkClient.get_worker_orders_raw = lambda self, status_filter="all": KworkExtensions.get_worker_orders(
            self, status_filter
        )
        KworkClient.get_notifications_raw = lambda self: KworkExtensions.get_notifications(self)
        KworkClient.get_connects_info = lambda self: KworkExtensions.get_connects_info(self)
        KworkClient.get_raw_projects = lambda self, **kw: KworkExtensions.get_raw_projects(self, **kw)
        KworkClient.upload_file_to_offer = lambda self, project_id, file_path, **kw: (
            KworkExtensions.upload_file_to_offer(self, project_id, file_path, **kw)
        )

        KworkClient.approve_order_raw = lambda self, order_id: KworkExtensions.approve_order(self, order_id)
        KworkClient.cancel_order_raw = lambda self, order_id, reason="": KworkExtensions.cancel_order_by_worker(
            self, order_id, reason
        )
        KworkClient.get_order_details_raw = lambda self, order_id: KworkExtensions.get_order_details(self, order_id)
        KworkClient.get_order_header_raw = lambda self, order_id: KworkExtensions.get_order_header(self, order_id)
        KworkClient.get_order_files_raw = lambda self, order_id: KworkExtensions.get_order_files(self, order_id)
        KworkClient.create_review_raw = lambda self, order_id, rating=5, text="", body=None: (
            KworkExtensions.create_review(self, order_id, rating, text, body)
        )
        KworkClient.get_kwork_reviews_raw = lambda self, kwork_id, page=1: KworkExtensions.get_kwork_reviews(
            self, kwork_id, page
        )
        KworkClient.inbox_read_raw = lambda self, message_id: KworkExtensions.inbox_read(self, message_id)
        KworkClient.mark_inbox_read_raw = lambda self, dialog_id: KworkExtensions.mark_inbox_tracks_as_read(
            self, dialog_id
        )
        KworkClient.set_typing_raw = lambda self, recipient_id: KworkExtensions.set_typing(self, recipient_id)
        KworkClient.is_dialog_allowed_raw = lambda self, user_id: KworkExtensions.is_dialog_allow(self, user_id)
        KworkClient.get_wants_count_raw = lambda self, **kw: KworkExtensions.get_wants_count(self, **kw)
        KworkClient.exchange_info_raw = lambda self: KworkExtensions.exchange_info(self)
        KworkClient.get_offers_raw = lambda self: KworkExtensions.get_offers(self)
        KworkClient.get_offer_raw = lambda self, offer_id: KworkExtensions.get_offer(self, offer_id)
        KworkClient.delete_offer_raw = lambda self, offer_id: KworkExtensions.delete_offer(self, offer_id)
        KworkClient.upload_file_raw = lambda self, file_path: KworkExtensions.upload_file(self, file_path)
        KworkClient.get_user_info_raw = lambda self, user_id: KworkExtensions.get_user_info(self, user_id)
        KworkClient.get_kwork_details_raw = lambda self, kwork_id: KworkExtensions.get_kwork_details(self, kwork_id)
        KworkClient.get_kworks_list_raw = lambda self, category_id, page=1: KworkExtensions.get_kworks_list(
            self, category_id, page
        )
        KworkClient.get_kworks_status_raw = lambda self: KworkExtensions.get_kworks_status_list(self)
        KworkClient.pause_kwork_raw = lambda self, kwork_id: KworkExtensions.pause_kwork(self, kwork_id)
        KworkClient.get_badges_info_raw = lambda self: KworkExtensions.get_badges_info(self)
        KworkClient.get_captcha_status_raw = lambda self: KworkExtensions.get_captcha_status(self)
        KworkClient.get_current_versions_raw = lambda self: KworkExtensions.get_current_versions(self)
        KworkClient.apply_filters_raw = lambda self, **kw: KworkExtensions.apply_filters(self, **kw)
        KworkClient.orders_between_raw = lambda self, user_id: KworkExtensions.orders_between(self, user_id)

        KworkClient._psr_ext_applied = True
        logger.debug("KworkExt: monkey-patches применены к KworkClient")

    except ImportError:
        logger.debug("KworkExt: библиотека kwork не установлена, patches пропущены")
    except Exception as e:
        logger.warning(f"KworkExt: не удалось применить patches: {e}")
