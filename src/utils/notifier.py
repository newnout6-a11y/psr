from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from src.action.decision_policy import DecisionPolicy, ExecutionMode
from src.action.proposal_db import ProposalDB
from src.paths import SCREENSHOTS_DIR
from src.utils.telegram_transport import send_telegram, send_telegram_photo


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TelegramNotifier:
    """Telegram-first операторский интерфейс."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db: ProposalDB | None = None):
        if getattr(self, "_initialized", False):
            if db is not None:
                self.db = db
            return

        self._initialized = True
        self.db = db or ProposalDB()
        self.token = os.getenv("TELEGRAM_TOKEN")
        self.admin_id = os.getenv("ADMIN_CHAT_ID")
        self.timeout = int(os.getenv("APPROVAL_TIMEOUT", "900"))
        self.running_task = None
        self._candidate_executor: Optional[Callable[[int, str, Optional[dict[str, Any]]], Awaitable[str]]] = None
        self._awaiting_text_edit: dict[str, int] = {}
        self._awaiting_price_edit: dict[str, int] = {}
        self._awaiting_price_apply: dict[str, bool] = {}

        if not self.token:
            logger.error("TelegramNotifier: TELEGRAM_TOKEN не найден в .env")
            self.bot = None
            self.dp = None
            return

        proxy_url = os.getenv("TELEGRAM_PROXY_URL")
        if proxy_url:
            from aiogram.client.session.aiohttp import AiohttpSession
            self.bot = Bot(token=self.token, session=AiohttpSession(proxy=proxy_url))
        else:
            self.bot = Bot(token=self.token)
        self.dp = Dispatcher()
        self._setup_handlers()

    def bind_runtime(
        self,
        *,
        db: ProposalDB | None = None,
        candidate_executor: Optional[Callable[[int, str, Optional[dict[str, Any]]], Awaitable[str]]] = None,
    ) -> None:
        if db is not None:
            self.db = db
        if candidate_executor is not None:
            self._candidate_executor = candidate_executor

    def _setup_handlers(self) -> None:
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            self.admin_id = str(message.chat.id)
            self._save_id_to_env(self.admin_id)
            await message.answer(
                "Операторский режим PSR подключен.\n"
                "Доступны команды: /queue /digest /stats /mode /pause /resume /rules"
            )

        @self.dp.message(Command("queue"))
        async def cmd_queue(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            page = max(int(arg) - 1, 0) if arg and arg.isdigit() else 0
            await self.send_queue_page(page=page, chat_id=str(message.chat.id))

        @self.dp.message(Command("digest"))
        async def cmd_digest(message: types.Message):
            if not self._is_admin_message(message):
                return
            await self.send_digest(chat_id=str(message.chat.id))

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: types.Message):
            if not self._is_admin_message(message):
                return
            await self.send_stats(chat_id=str(message.chat.id))

        @self.dp.message(Command("pause"))
        async def cmd_pause(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message).lower()
            if arg:
                self.db.set_platform_paused(arg, True)
                await message.answer(f"Платформа {arg} поставлена на паузу.")
            else:
                self.db.set_runtime_mode(ExecutionMode.PAUSED.value)
                await message.answer("Система переведена в режим paused.")

        @self.dp.message(Command("resume"))
        async def cmd_resume(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message).lower()
            if arg:
                self.db.set_platform_paused(arg, False)
                await message.answer(f"Пауза с платформы {arg} снята.")
            else:
                self.db.set_runtime_mode(ExecutionMode.SEMI_AUTO.value)
                await message.answer("Система переведена в режим semi_auto.")

        @self.dp.message(Command("mode"))
        async def cmd_mode(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message).lower()
            if arg in {mode.value for mode in ExecutionMode}:
                self.db.set_runtime_mode(arg)
                await message.answer(f"Режим обновлён: {arg}")
                return

            mode = self.db.get_runtime_mode(os.getenv("EXECUTION_MODE", "semi_auto"))
            paused = self.db.get_paused_platforms()
            tail = f"\nПлатформы на паузе: {', '.join(paused)}" if paused else ""
            await message.answer(f"Текущий режим: {mode}{tail}")

        @self.dp.message(Command("rules"))
        async def cmd_rules(message: types.Message):
            if not self._is_admin_message(message):
                return
            policy = DecisionPolicy()
            await message.answer(
                "Правила semi_auto:\n"
                f"AI-оценка >= {policy.auto_send_score_min}\n"
                f"Оценка заказчика >= {policy.auto_send_vet_min}\n"
                f"Откликов <= {policy.auto_send_max_offers}\n"
                f"Бюджет <= {int(policy.auto_send_max_budget)}\n"
                "Без красных флагов, валидный текст отклика, отправка не на паузе."
            )

        @self.dp.callback_query(F.data.startswith("candidate:"))
        async def handle_candidate_action(callback: CallbackQuery):
            if self.admin_id and str(callback.from_user.id) != str(self.admin_id):
                await callback.answer("Не авторизован", show_alert=True)
                return

            parts = callback.data.split(":")
            if len(parts) < 3:
                await callback.answer("Некорректное действие", show_alert=True)
                return

            candidate_id = int(parts[1])
            action = parts[2]
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await callback.answer("Кандидат не найден", show_alert=True)
                return

            if action == "approve":
                await callback.answer("Отправляю...")
                message = await self._run_candidate_executor(candidate_id, "approve")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer(message)
                return

            if action == "price":
                if len(parts) < 4:
                    await callback.answer("Цена не передана", show_alert=True)
                    return
                price = self._extract_price(parts[3])
                if not price:
                    await callback.answer("Некорректная цена", show_alert=True)
                    return
                self.db.update_candidate(
                    candidate_id,
                    chosen_price=price,
                    manual_override=True,
                    last_actor="telegram",
                )
                self.db.record_candidate_action(
                    candidate_id,
                    "choose_price",
                    actor="telegram",
                    payload={"chosen_price": price},
                )
                await callback.answer(f"Принято: {price} руб.")
                message = await self._run_candidate_executor(candidate_id, "approve")
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer(message)
                return

            if action == "skip":
                self.db.update_candidate_status(
                    candidate_id,
                    "skipped",
                    actor="telegram",
                    reason="skipped by operator",
                    manual_override=True,
                )
                if candidate.get("search_query"):
                    self.db.record_query_signal(candidate["platform"], candidate["search_query"], "skipped")
                await callback.answer("Пропущено")
                await callback.message.edit_reply_markup(reply_markup=None)
                return

            if action == "prefer":
                if candidate.get("search_query"):
                    self.db.mark_query_preferred(candidate["platform"], candidate["search_query"])
                self.db.record_candidate_action(candidate_id, "prefer_query", actor="telegram")
                await callback.answer("Похожий запрос отмечен как полезный")
                return

            if action == "edit_text":
                self._awaiting_text_edit[str(callback.from_user.id)] = candidate_id
                await callback.answer("Пришли новый текст")
                await callback.message.answer(
                    f"Пришли новый текст отклика для #{candidate_id}. После этого выбери цену кнопками выше."
                )
                return

            if action in {"edit_price", "custom_price"}:
                chat_key = str(callback.from_user.id)
                self._awaiting_price_edit[chat_key] = candidate_id
                self._awaiting_price_apply[chat_key] = action == "custom_price"
                await callback.answer("Пришли цену числом")
                await callback.message.answer(
                    f"Пришли цену для #{candidate_id} числом, например 3000."
                )
                return

            if action == "snooze":
                minutes = int(parts[3]) if len(parts) > 3 else 60
                self.db.snooze_candidate(candidate_id, minutes, actor="telegram")
                await callback.answer(f"Отложено на {minutes} мин.")
                await callback.message.edit_reply_markup(reply_markup=None)
                return

            await callback.answer("Неизвестное действие", show_alert=True)

        @self.dp.message(F.text)
        async def handle_text_input(message: types.Message):
            if not self._is_admin_message(message):
                return

            chat_id = str(message.chat.id)
            text = (message.text or "").strip()

            candidate_id = self._awaiting_text_edit.pop(chat_id, None)
            if candidate_id:
                if len(text) < 40:
                    await message.answer("Текст слишком короткий. Нужен внятный отклик.")
                    self._awaiting_text_edit[chat_id] = candidate_id
                    return
                self.db.update_candidate(
                    candidate_id,
                    proposal_text=text,
                    manual_override=True,
                    last_actor="telegram",
                )
                self.db.record_candidate_action(candidate_id, "edit_text", actor="telegram")
                await message.answer(f"Текст отклика для #{candidate_id} обновлён.")
                return

            candidate_id = self._awaiting_price_edit.pop(chat_id, None)
            if candidate_id:
                should_apply = self._awaiting_price_apply.pop(chat_id, False)
                normalized_price = self._extract_price(text)
                if not normalized_price:
                    await message.answer("Цена должна содержать число, например 2500.")
                    self._awaiting_price_edit[chat_id] = candidate_id
                    self._awaiting_price_apply[chat_id] = should_apply
                    return
                self.db.update_candidate(
                    candidate_id,
                    chosen_price=normalized_price,
                    manual_override=True,
                    last_actor="telegram",
                )
                self.db.record_candidate_action(
                    candidate_id,
                    "edit_price",
                    actor="telegram",
                    payload={"chosen_price": normalized_price},
                )
                await message.answer(f"Цена для #{candidate_id} обновлена: {normalized_price} руб.")
                if should_apply:
                    result = await self._run_candidate_executor(candidate_id, "approve")
                    await message.answer(result)

    def _is_admin_message(self, message: types.Message) -> bool:
        if not self.admin_id:
            return True
        return str(message.chat.id) == str(self.admin_id)

    def _command_arg(self, message: types.Message) -> str:
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _save_id_to_env(self, chat_id: str) -> None:
        env_path = Path(".env")
        if not env_path.exists():
            return
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
            updated = []
            found = False
            for line in lines:
                if line.startswith("ADMIN_CHAT_ID="):
                    updated.append(f"ADMIN_CHAT_ID={chat_id}")
                    found = True
                else:
                    updated.append(line)
            if not found:
                updated.append(f"ADMIN_CHAT_ID={chat_id}")
            env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning(f"TelegramNotifier: не удалось сохранить ADMIN_CHAT_ID: {e}")

    def _extract_price(self, text: str) -> Optional[str]:
        digits = re.sub(r"[^\d]", "", text or "")
        if not digits:
            return None
        return str(int(digits))

    def _truncate(self, text: str, limit: int = 3800) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _truncate_caption(self, text: str, limit: int = 1024) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _budget_int(self, candidate: dict[str, Any]) -> int:
        raw = candidate.get("chosen_price") or candidate.get("budget") or 0
        try:
            return int(float(raw))
        except Exception:
            return 0

    def _candidate_keyboard(self, candidate: dict[str, Any]) -> InlineKeyboardMarkup:
        candidate_id = int(candidate["candidate_id"])
        base = self._budget_int(candidate)
        buttons: list[list[InlineKeyboardButton]] = []
        if base > 0:
            buttons.append(
                [
                    InlineKeyboardButton(text=f"{base} руб.", callback_data=f"candidate:{candidate_id}:price:{base}"),
                    InlineKeyboardButton(
                        text=f"{int(base * 1.2)} руб. (+20%)",
                        callback_data=f"candidate:{candidate_id}:price:{int(base * 1.2)}",
                    ),
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{int(base * 1.5)} руб. (+50%)",
                        callback_data=f"candidate:{candidate_id}:price:{int(base * 1.5)}",
                    ),
                ]
            )
        else:
            buttons.append([InlineKeyboardButton(text="Отправить", callback_data=f"candidate:{candidate_id}:approve")])

        buttons.append(
            [
                InlineKeyboardButton(text="Своя цена", callback_data=f"candidate:{candidate_id}:custom_price"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"candidate:{candidate_id}:skip"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(text="✏️ Изменить отклик", callback_data=f"candidate:{candidate_id}:edit_text"),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def _candidate_message(self, candidate: dict[str, Any]) -> str:
        budget = self._budget_int(candidate)
        budget_str = f"{budget} руб." if budget else "не указан"
        offers = candidate.get("offers_count") or 0
        project_id = candidate.get("project_id") or candidate.get("candidate_id")
        platform = candidate.get("platform") or "unknown"
        proposal = candidate.get("proposal_text") or "Отклик ещё не сгенерирован."
        hired_percent = candidate.get("client_hired_percent") or 0
        client_context = candidate.get("client_context") or {}
        client_data = client_context.get("client") if isinstance(client_context, dict) else {}
        if not hired_percent and isinstance(client_data, dict):
            hired_percent = client_data.get("order_done_repeat_persent") or 0

        competition = ""
        if offers:
            competition += f"\n👥 Конкуренция: {offers} откликов"
            if offers > 50:
                competition += " высокая"
            elif offers > 20:
                competition += " средняя"
            else:
                competition += " низкая"
        if hired_percent:
            competition += f"\n📈 Нанимает: {hired_percent}%"

        competitor_prices = candidate.get("competitor_prices") or []
        if competitor_prices:
            prices = [int(float(item["price"])) for item in competitor_prices[:5] if item.get("price")]
            if prices:
                competition += "\n💰 Цены конкурентов: " + ", ".join(f"{price}₽" for price in prices)

        text = (
            f"🔔 Новый проект ({platform})\n\n"
            f"📌 {candidate.get('title', 'Без названия')}\n"
            f"🆔 ID: {project_id}\n"
            f"💰 Бюджет: {budget_str}"
            f"{competition}\n\n"
            f"📝 Текст отклика:\n{proposal}\n\n"
            f"Выбери цену:"
        )
        return self._truncate_caption(text)

    async def start(self) -> None:
        if self.running_task or not self.bot or not self.dp:
            return
        logger.info("TelegramNotifier: запуск polling")
        self.running_task = asyncio.create_task(self._polling_loop())

    async def _polling_loop(self) -> None:
        if not self.bot or not self.dp:
            return
        retry_delay = 5
        while True:
            try:
                await self.dp.start_polling(self.bot)
                retry_delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"TelegramNotifier: polling недоступен ({e}), повтор через {retry_delay} сек."
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)

    async def stop(self) -> None:
        if self.running_task:
            self.running_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.running_task
            self.running_task = None
        if self.bot:
            await self.bot.session.close()

    async def send_text(self, text: str, chat_id: Optional[str] = None) -> None:
        if not self.bot:
            return
        target = chat_id or self.admin_id
        if not target:
            return
        await self._safe_send_message(target, text)

    async def notify_candidate(self, candidate: dict[str, Any], chat_id: Optional[str] = None) -> None:
        if not self.bot:
            return
        target = chat_id or self.admin_id
        if not target:
            return
        text = self._candidate_message(candidate)
        keyboard = self._candidate_keyboard(candidate)
        screenshot_path = candidate.get("screenshot_path") or self._find_project_screenshot(candidate)
        if screenshot_path and Path(str(screenshot_path)).exists():
            if await self._safe_send_photo(target, str(screenshot_path), text, reply_markup=keyboard):
                return
        await self._safe_send_message(
            target,
            text,
            reply_markup=keyboard,
        )

    async def _safe_send_message(self, target: str, text: str, **kwargs) -> bool:
        if not target:
            return False
        result = await send_telegram(
            self._truncate(text),
            chat_id=target,
            reply_markup=kwargs.get("reply_markup"),
        )
        if result.ok:
            return True
        logger.warning(f"TelegramNotifier: не удалось отправить сообщение: {result.to_dict()}")
        return False

    async def _safe_send_photo(self, target: str, photo_path: str, caption: str, **kwargs) -> bool:
        result = await send_telegram_photo(
            photo_path,
            self._truncate_caption(caption),
            chat_id=target,
            reply_markup=kwargs.get("reply_markup"),
        )
        if result.ok:
            return True
        logger.warning(f"TelegramNotifier: не удалось отправить фото: {result.to_dict()}")
        return False

    def _find_project_screenshot(self, candidate: dict[str, Any]) -> Optional[str]:
        project_id = str(candidate.get("project_id") or "").strip()
        if not project_id:
            return None
        folder = SCREENSHOTS_DIR / project_id
        if not folder.exists():
            return None
        preferred = [
            folder / "01_project_page.png",
            folder / "01_project_page_scrolled.png",
        ]
        for path in preferred:
            if path.exists():
                return str(path)
        first = next(folder.glob("*.png"), None)
        return str(first) if first else None

    async def send_queue_page(self, page: int = 0, chat_id: Optional[str] = None) -> None:
        target = chat_id or self.admin_id
        if not target or not self.bot:
            return
        page_size = int(os.getenv("TELEGRAM_QUEUE_PAGE_SIZE", "5"))
        items = self.db.get_queue(page=page, page_size=page_size)
        if not items:
            await self._safe_send_message(target, "Очередь пуста.")
            return
        await self._safe_send_message(target, f"Очередь кандидатов, страница {page + 1}:")
        for item in items:
            await self.notify_candidate(item, chat_id=target)

    async def send_digest(self, chat_id: Optional[str] = None) -> None:
        target = chat_id or self.admin_id
        if not target or not self.bot:
            return

        cursor = self.db.get_runtime_state("telegram.digest.cursor", "1970-01-01 00:00:00")
        digest = self.db.build_digest(cursor, limit=100)
        now = _now()
        counts = digest.get("counts", {})
        lines = digest.get("lines", [])

        if not lines:
            await self._safe_send_message(target, "Дайджест: новых событий нет.")
            self.db.set_runtime_state("telegram.digest.last_sent_at", now)
            return

        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "нет агрегатов"
        body = "\n".join(lines[:20])
        await self._safe_send_message(
            target,
            f"Дайджест с {cursor}\nИтог: {summary}\n\n{body}",
        )
        self.db.set_runtime_state("telegram.digest.cursor", now)
        self.db.set_runtime_state("telegram.digest.last_sent_at", now)

    async def maybe_send_digest(self) -> None:
        if not self.admin_id or not self.bot:
            return
        interval = int(os.getenv("DIGEST_INTERVAL_MIN", "60"))
        last_sent = self.db.get_runtime_state("telegram.digest.last_sent_at")
        if last_sent:
            try:
                last_dt = datetime.strptime(last_sent, "%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.now() - last_dt).total_seconds() / 60
                if elapsed < interval:
                    return
            except Exception:
                pass
        await self.send_digest()

    async def send_stats(self, chat_id: Optional[str] = None) -> None:
        target = chat_id or self.admin_id
        if not target or not self.bot:
            return
        summary = self.db.get_summary()
        status_counts = self.db.get_candidate_status_counts()
        paused = self.db.get_paused_platforms()
        mode = self.db.get_runtime_mode(os.getenv("EXECUTION_MODE", "semi_auto"))
        platform_stats = self.db.get_platform_stats()
        platform_lines = [
            f"{item['platform']}: sent={item['total']}, replied={item['responded']}"
            for item in platform_stats
        ] or ["нет данных по платформам"]

        text = (
            f"Режим: {mode}\n"
            f"Платформы на паузе: {', '.join(paused) if paused else 'нет'}\n"
            f"Всего отправлено: {summary['total_sent']}\n"
            f"Отправлено сегодня: {summary['today_sent']}\n"
            f"В очереди: {summary['queue_count']}\n"
            f"Статусы кандидатов: {status_counts}\n\n"
            f"Платформы:\n" + "\n".join(platform_lines)
        )
        await self._safe_send_message(target, text)

    async def notify_response(self, project_title: str, platform: str, response_text: str):
        if not self.bot or not self.admin_id:
            return
        msg = (
            f"Ответ от заказчика\n\n"
            f"Проект: {project_title}\n"
            f"Платформа: {platform}\n\n"
            f"{response_text[:1200]}"
        )
        await self._safe_send_message(self.admin_id, msg)

    async def _run_candidate_executor(self, candidate_id: int, action: str) -> str:
        if self._candidate_executor is None:
            return f"Исполнитель не привязан. Действие {action} не выполнено."
        try:
            return await self._candidate_executor(candidate_id, action, None)
        except Exception as e:
            logger.error(f"TelegramNotifier: executor error for {candidate_id}/{action}: {e}")
            return f"Ошибка выполнения для #{candidate_id}: {e}"

    # Совместимость со старым интерфейсом
    def get_edited_proposal(self, project_id: str) -> Optional[str]:
        return None

    async def request_approval(self, *args, **kwargs) -> Optional[str]:
        logger.warning("TelegramNotifier.request_approval устарел: используется queue-driven flow")
        return None

    async def notify_new_project_with_prices(self, *args, **kwargs) -> None:
        logger.warning("TelegramNotifier.notify_new_project_with_prices устарел: используется queue-driven flow")
