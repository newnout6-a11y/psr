from __future__ import annotations

import asyncio
import os
import random
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


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
        self._awaiting_reply: dict[str, tuple[str, str]] = {}
        self._awaiting_msg: dict[str, int] = {}
        self._restore_awaiting_state()

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

    def _save_awaiting_state(self) -> None:
        if not self.db:
            return
        import json as _json

        state = {
            "text_edit": {k: v for k, v in self._awaiting_text_edit.items()},
            "price_edit": {k: v for k, v in self._awaiting_price_edit.items()},
            "price_apply": {k: v for k, v in self._awaiting_price_apply.items()},
            "reply": {k: list(v) for k, v in self._awaiting_reply.items()},
            "msg": {k: v for k, v in self._awaiting_msg.items()},
        }
        self.db.set_runtime_state("telegram.awaiting_state", _json.dumps(state, ensure_ascii=False))

    def _restore_awaiting_state(self) -> None:
        if not self.db:
            return
        import json as _json

        raw = self.db.get_runtime_state("telegram.awaiting_state")
        if not raw:
            return
        try:
            state = _json.loads(raw)
            self._awaiting_text_edit = {k: int(v) for k, v in state.get("text_edit", {}).items()}
            self._awaiting_price_edit = {k: int(v) for k, v in state.get("price_edit", {}).items()}
            self._awaiting_price_apply = {k: bool(v) for k, v in state.get("price_apply", {}).items()}
            self._awaiting_reply = {
                k: tuple(v) for k, v in state.get("reply", {}).items() if isinstance(v, list) and len(v) == 2
            }
            self._awaiting_msg = {k: int(v) for k, v in state.get("msg", {}).items()}
            if any([self._awaiting_text_edit, self._awaiting_price_edit, self._awaiting_reply, self._awaiting_msg]):
                logger.info("TelegramNotifier: восстановлено состояние awaiting из БД")
        except Exception:
            pass

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
            if self.admin_id and str(message.chat.id) != str(self.admin_id):
                await message.answer("Доступ запрещён: оператор уже назначен.")
                return
            if not self.admin_id:
                self.admin_id = str(message.chat.id)
                self._save_id_to_env(self.admin_id)
            await message.answer(
                "Операторский режим PSR подключен.\n"
                "Команды: /queue /chats /reply /msg /orders /offers /status /rate /health /connects /digest /stats /mode /pause /resume /rules\n"
                "Конверсия: /hire /decline /complete /earn /paid /earnings"
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

        @self.dp.message(Command("chats"))
        async def cmd_chats(message: types.Message):
            if not self._is_admin_message(message):
                return
            if not self.db:
                await message.answer("БД недоступна.")
                return
            convs = self.db.get_active_conversations(limit=10)
            if not convs:
                await message.answer("Активных диалогов нет.")
                return
            lines = []
            for c in convs:
                lines.append(
                    f"#{c['conversation_id']} [{c['platform']}] {c.get('project_title', 'Без названия')}"
                    f"\n  статус: {c['status']}, последнее: {c.get('last_message_at', '?')}"
                )
            await message.answer("Активные диалоги:\n\n" + "\n\n".join(lines[:10]))

        @self.dp.message(Command("reply"))
        async def cmd_reply(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg:
                await message.answer("Использование: /reply <project_id> <платформа>\nЗатем напиши текст ответа.")
                return
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Укажи project_id и платформу: /reply 12345 kwork")
                return
            project_id, platform = parts[0].strip(), parts[1].strip().lower()
            self._awaiting_reply[str(message.chat.id)] = (project_id, platform)
            await message.answer(
                f"Пришли текст ответа для проекта {project_id} ({platform}).\n"
                "Сообщение будет сохранено в историю диалога."
            )

        @self.dp.message(Command("hire"))
        async def cmd_hire(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg or not arg.isdigit():
                await message.answer("Использование: /hire <candidate_id>")
                return
            candidate_id = int(arg)
            if not self.db:
                return
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await message.answer(f"Кандидат #{candidate_id} не найден.")
                return
            self.db.mark_candidate_hired(candidate_id, actor="telegram")
            await message.answer(
                f"Кандидат #{candidate_id} отмечен как hired.\n"
                "Используй /earn <candidate_id> <сумма> для записи заработка."
            )

        @self.dp.message(Command("decline"))
        async def cmd_decline(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg or not arg.isdigit():
                await message.answer("Использование: /decline <candidate_id>")
                return
            candidate_id = int(arg)
            if not self.db:
                return
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await message.answer(f"Кандидат #{candidate_id} не найден.")
                return
            self.db.mark_candidate_declined(candidate_id, actor="telegram", reason="declined by operator")
            await message.answer(f"Кандидат #{candidate_id} отмечен как declined.")

        @self.dp.message(Command("complete"))
        async def cmd_complete(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg or not arg.isdigit():
                await message.answer("Использование: /complete <candidate_id>")
                return
            candidate_id = int(arg)
            if not self.db:
                return
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await message.answer(f"Кандидат #{candidate_id} не найден.")
                return
            self.db.mark_candidate_completed(candidate_id, actor="telegram")
            if (
                os.getenv("KWORK_AUTO_REVIEW", "false").lower() in {"1", "true", "yes", "on"}
                and candidate["platform"] == "kwork"
            ):
                try:
                    from src.platforms.kwork import get_kwork_service

                    service = get_kwork_service()
                    review_text = os.getenv(
                        "KWORK_AUTO_REVIEW_TEXT", "Спасибо за заказ! Буду рад сотрудничеству в будущем."
                    )
                    rating = int(os.getenv("KWORK_AUTO_REVIEW_RATING", "5"))

                    orders = await service.get_worker_orders(status_filter="all")
                    order_id = None
                    for o in orders:
                        if isinstance(o, dict) and str(o.get("project_id", "")) == str(candidate.get("project_id", "")):
                            order_id = o.get("id")
                            break

                    if order_id:
                        result = await service.auto_review_completed(int(order_id), rating=rating, text=review_text)
                    else:
                        logger.warning(
                            f"TelegramNotifier: order not found for project {candidate.get('project_id')}, review skipped"
                        )
                        result = None

                    if result:
                        await message.answer(
                            f"Кандидат #{candidate_id} отмечен как completed.\n"
                            f"Отзыв автоматически оставлен (рейтинг {rating})."
                        )
                    else:
                        await message.answer(
                            f"Кандидат #{candidate_id} отмечен как completed.\n"
                            f"Не удалось оставить отзыв автоматически (заказ не найден)."
                        )
                except Exception as e:
                    logger.error(f"TelegramNotifier: auto_review error: {e}")
                    await message.answer(f"Кандидат #{candidate_id} отмечен как completed.\nОшибка авто-отзыва: {e}")
            else:
                await message.answer(
                    f"Кандидат #{candidate_id} отмечен как completed (работа сдана).\n"
                    "Не забудьте оставить отзыв клиенту!"
                )

        @self.dp.message(Command("earn"))
        async def cmd_earn(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            parts = arg.split()
            if len(parts) < 2 or not parts[0].isdigit():
                await message.answer("Использование: /earn <candidate_id> <сумма> [валюта]")
                return
            candidate_id = int(parts[0])
            try:
                amount = float(parts[1])
            except ValueError:
                await message.answer("Сумма должна быть числом, например: /earn 42 15000")
                return
            currency = parts[2].upper() if len(parts) > 2 else "RUB"
            if not self.db:
                return
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await message.answer(f"Кандидат #{candidate_id} не найден.")
                return
            self.db.record_earning(
                candidate_id=candidate_id,
                project_id=candidate["project_id"],
                platform=candidate["platform"],
                amount=amount,
                currency=currency,
                status="pending",
            )
            await message.answer(
                f"Заработок записан: {amount} {currency} для #{candidate_id} (статус pending).\n"
                "Используй /paid <earning_id> после получения оплаты."
            )

        @self.dp.message(Command("paid"))
        async def cmd_paid(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg or not arg.isdigit():
                await message.answer("Использование: /paid <earning_id>")
                return
            earning_id = int(arg)
            if not self.db:
                return
            self.db.update_earning_status(earning_id, "paid")
            await message.answer(f"Заработок #{earning_id} отмечен как оплаченный.")

        @self.dp.message(Command("earnings"))
        async def cmd_earnings(message: types.Message):
            if not self._is_admin_message(message):
                return
            if not self.db:
                return
            summary = self.db.get_earnings_summary()
            text = (
                f"Заработок:\n"
                f"  Всего: {summary['total_amount']:.0f} ₽ ({summary['total']} записей)\n"
                f"  Оплачено: {summary['paid_amount']:.0f} ₽ ({summary['paid_count']})\n"
                f"  Ожидает: {summary['pending_amount']:.0f} ₽ ({summary['pending_count']})\n"
            )
            await message.answer(text)

        @self.dp.message(Command("connects"))
        async def cmd_connects(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service
            from src.platforms.kwork_ext import get_connects_monitor

            service = get_kwork_service()
            info = await service.check_connects()
            monitor = get_connects_monitor()
            free = monitor.free_amount
            can_send = "да" if monitor.can_send() else "нет"
            text = (
                f"Connects:\n"
                f"  Свободно: {free}\n"
                f"  Отправка возможна: {can_send}\n"
                f"  Предупреждение при: {monitor.warn_threshold}\n"
                f"  Блокировка при: {monitor.block_threshold}\n"
            )
            if info:
                text += f"  Всего: {info.get('total_amount', '?')}\n"
                text += f"  Валюта: {info.get('currency', '?')}\n"
            await message.answer(text)

        @self.dp.message(Command("orders"))
        async def cmd_orders(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            orders = await service.get_worker_orders(status_filter="all")
            if not orders:
                await message.answer("Заказов не найдено.")
                return
            lines = []
            for o in orders[:10]:
                if isinstance(o, dict):
                    lines.append(
                        f"#{o.get('id', '?')} {o.get('status', '?')} — {o.get('project_name', o.get('title', '?'))[:50]}"
                    )
            await message.answer("Заказы:\n\n" + "\n".join(lines))

        @self.dp.message(Command("offers"))
        async def cmd_offers(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            offers = await service.get_offers()
            if not offers:
                await message.answer("Откликов не найдено.")
                return
            lines = []
            for o in offers[:10]:
                if isinstance(o, dict):
                    lines.append(
                        f"#{o.get('id', '?')} {o.get('status', '?')} — {o.get('want_name', o.get('title', '?'))[:50]}"
                    )
            await message.answer("Отклики:\n\n" + "\n".join(lines))

        @self.dp.message(Command("health"))
        async def cmd_health(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service

            service = get_kwork_service()
            captcha = await service.get_captcha_status()
            badges = await service.get_badges_info()
            versions = await service.get_current_versions()
            exchange = await service.exchange_info()

            text = (
                f"Kwork Health:\n"
                f"  Капча требуется: {'да' if captcha else 'нет'}\n"
                f"  Непрочитанных уведомлений: {badges.get('notifications', '?')}\n"
                f"  Биржа активна: {'да' if exchange else 'неизвестно'}\n"
            )
            if versions:
                text += f"  Версия API: {versions.get('android_version', versions.get('version', '?'))}\n"
            await message.answer(text)

        @self.dp.message(Command("rate"))
        async def cmd_rate(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service
            from src.platforms.kwork_ext import get_success_rate_monitor

            service = get_kwork_service()
            await service.check_success_rate()
            monitor = get_success_rate_monitor()
            can_send = "да" if monitor.can_send() else "нет"
            throttle = "да" if monitor.should_throttle() else "нет"
            text = (
                f"Success Rate:\n"
                f"  Рейтинг: {monitor.success_rate:.1f}%\n"
                f"  Завершено: {monitor.completed}\n"
                f"  Отменено: {monitor.cancelled}\n"
                f"  Активных: {monitor.active_orders}\n"
                f"  Отправка возможна: {can_send}\n"
                f"  Throttle активен: {throttle}\n"
            )
            await message.answer(text)

        @self.dp.message(Command("status"))
        async def cmd_status(message: types.Message):
            if not self._is_admin_message(message):
                return
            from src.platforms.kwork import get_kwork_service
            from src.platforms.kwork_ext import get_account_health_monitor

            service = get_kwork_service()
            await service.check_account_health()
            monitor = get_account_health_monitor()
            await message.answer(monitor.get_summary_text())

        @self.dp.message(Command("msg"))
        async def cmd_msg(message: types.Message):
            if not self._is_admin_message(message):
                return
            arg = self._command_arg(message)
            if not arg or not arg.isdigit():
                await message.answer("Использование: /msg <candidate_id>\nЗатем напиши текст сообщения клиенту.")
                return
            candidate_id = int(arg)
            if not self.db:
                return
            candidate = self.db.get_candidate(candidate_id)
            if not candidate:
                await message.answer(f"Кандидат #{candidate_id} не найден.")
                return
            if candidate["platform"] != "kwork":
                await message.answer("Отправка сообщений поддерживается только для Kwork.")
                return
            self._awaiting_msg[str(message.chat.id)] = candidate_id
            await message.answer(
                f"Пришли текст сообщения для клиента по проекту #{candidate_id}.\n"
                "Сообщение будет отправлено через Kwork чат."
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
                confirm_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"Отправить за {price} руб.",
                                callback_data=f"candidate:{candidate_id}:confirm_send:{price}",
                            ),
                            InlineKeyboardButton(text="Отмена", callback_data=f"candidate:{candidate_id}:cancel_send"),
                        ],
                    ]
                )
                await callback.message.edit_reply_markup(reply_markup=confirm_kb)
                await callback.answer(f"Подтверди отправку за {price} руб.")
                return

            if action == "confirm_send":
                price = parts[3] if len(parts) > 3 else None
                await callback.answer("Отправляю...")
                payload = {"chosen_price": price} if price else None
                message = await self._run_candidate_executor(candidate_id, "approve", payload)
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.message.answer(message)
                return

            if action == "cancel_send":
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("Отменено")
                return

            if action == "skip":
                candidate = self.db.get_candidate(candidate_id)
                undo_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="↩️ Восстановить", callback_data=f"candidate:{candidate_id}:unskip")],
                    ]
                )
                self.db.update_candidate_status(
                    candidate_id,
                    "skipped",
                    actor="telegram",
                    reason="skipped by operator",
                    manual_override=True,
                )
                if candidate.get("search_query"):
                    self.db.record_query_signal(candidate["platform"], candidate["search_query"], "skipped")
                await callback.message.edit_reply_markup(reply_markup=undo_kb)
                await callback.answer("Пропущено (можно восстановить)")
                return

            if action == "unskip":
                self.db.update_candidate(
                    candidate_id,
                    status="queued",
                    manual_override=False,
                    last_actor="telegram",
                    reason="restored by operator",
                )
                await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("Восстановлен в очередь")
                return

            if action == "prefer":
                if candidate.get("search_query"):
                    self.db.mark_query_preferred(candidate["platform"], candidate["search_query"])
                self.db.record_candidate_action(candidate_id, "prefer_query", actor="telegram")
                await callback.answer("Похожий запрос отмечен как полезный")
                return

            if action == "edit_text":
                await callback.message.edit_reply_markup(reply_markup=None)
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
                await callback.message.answer(f"Пришли цену для #{candidate_id} числом, например 3000.")
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

            reply_target = self._awaiting_reply.pop(chat_id, None)
            if reply_target:
                project_id, platform = reply_target
                if not self.db:
                    await message.answer("БД недоступна.")
                    return
                if len(text) < 10:
                    await message.answer("Сообщение слишком короткое.")
                    self._awaiting_reply[chat_id] = reply_target
                    return
                conv = self.db.get_conversation(project_id, platform)
                if not conv:
                    conv_id = self.db.get_or_create_conversation(project_id, platform, project_title="")
                else:
                    conv_id = conv["conversation_id"]
                self.db.add_conversation_message(conv_id, sender="freelancer", message_text=text)
                await message.answer(
                    f"Ответ сохранён в диалог для проекта {project_id} ({platform}).\n"
                    "Отправь ответ на платформе вручную или используй /msg для отправки через API."
                )
                return

            msg_candidate_id = self._awaiting_msg.pop(chat_id, None)
            if msg_candidate_id is not None:
                if not self.db:
                    await message.answer("БД недоступна.")
                    return
                if len(text) < 10:
                    await message.answer("Сообщение слишком короткое.")
                    self._awaiting_msg[chat_id] = msg_candidate_id
                    return
                candidate = self.db.get_candidate(msg_candidate_id)
                if not candidate:
                    await message.answer(f"Кандидат #{msg_candidate_id} не найден.")
                    return
                client_context = candidate.get("client_context") or {}
                client_data = client_context.get("client") if isinstance(client_context, dict) else {}
                user_id_str = ""
                if isinstance(client_data, dict):
                    user_id_str = str(client_data.get("user_id", client_data.get("USERID", "")))
                if not user_id_str or not user_id_str.isdigit():
                    platform_data = candidate.get("platform_data") or {}
                    if isinstance(platform_data, dict):
                        user_obj = platform_data.get("user") or {}
                        if isinstance(user_obj, dict):
                            user_id_str = str(user_obj.get("USERID", user_obj.get("id", "")))
                if not user_id_str or not user_id_str.isdigit():
                    await message.answer(
                        "Не удалось определить user_id клиента для #{msg_candidate_id}.\n"
                        "Используй /reply для сохранения ответа в историю без отправки."
                    )
                    return
                from src.platforms.kwork import get_kwork_service

                service = get_kwork_service()
                await service.set_typing(int(user_id_str))
                await asyncio.sleep(random.uniform(1, 3))
                result = await service.send_message(int(user_id_str), text)
                if result is not None:
                    conv = self.db.get_conversation(candidate["project_id"], candidate["platform"])
                    if conv:
                        self.db.add_conversation_message(
                            conv["conversation_id"], sender="freelancer", message_text=text
                        )
                    await message.answer(f"Сообщение отправлено клиенту (user_id={user_id_str}) через Kwork чат.")
                else:
                    await message.answer(
                        "Не удалось отправить сообщение через Kwork API.\n"
                        "Ответ сохранён в историю. Отправь вручную на платформе."
                    )
                    conv = self.db.get_conversation(candidate["project_id"], candidate["platform"])
                    if conv:
                        self.db.add_conversation_message(
                            conv["conversation_id"], sender="freelancer", message_text=text
                        )
                return

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
                candidate = self.db.get_candidate(candidate_id)
                if candidate:
                    keyboard = self._candidate_keyboard(candidate)
                    await message.answer(
                        f"Текст отклика для #{candidate_id} обновлён.\nВыбери цену:",
                        reply_markup=keyboard,
                    )
                else:
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
            return False
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
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Отправить (без цены)", callback_data=f"candidate:{candidate_id}:confirm_send:none"
                    )
                ]
            )

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

        ai_score = candidate.get("ai_score")
        vet_score = candidate.get("vet_score")
        risk_level = candidate.get("risk_level") or ""
        status = candidate.get("status") or ""
        is_dry_run = bool(candidate.get("dry_run"))

        confidence = ""
        if status == "auto_ready":
            confidence = "🟢 Высокая уверенность"
        elif status == "queued":
            confidence = "🟡 На проверку"
        if risk_level == "high":
            confidence = "🔴 Высокий риск"
        elif risk_level == "medium" and confidence:
            confidence += " ⚠️"

        scores_line = ""
        if ai_score is not None:
            scores_line += f"AI: {ai_score}/10"
        if vet_score is not None:
            scores_line += f"  Vet: {vet_score}/100"
        if risk_level:
            scores_line += f"  Риск: {risk_level}"

        dry_run_prefix = "🧪 DRY-RUN " if is_dry_run else ""

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

        header = (
            f"{dry_run_prefix}🔔 Новый проект ({platform})\n\n"
            f"📌 {candidate.get('title', 'Без названия')}\n"
            f"🆔 ID: {project_id}\n"
            f"💰 Бюджет: {budget_str}"
            f"{competition}\n"
        )
        if confidence:
            header += f"\n{confidence}"
        if scores_line:
            header += f"\n{scores_line}"
        header += "\n\n"

        footer = "\n\nВыбери цену:" if not is_dry_run else "\n\nВыбери цену (draft — не будет отправлен):"
        available_for_proposal = 1024 - len(header) - len(footer)
        if len(proposal) > available_for_proposal:
            proposal = proposal[: available_for_proposal - 3] + "..."
        text = f"{header}📝 Текст отклика:\n{proposal}{footer}"
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
                logger.warning(f"TelegramNotifier: polling недоступен ({e}), повтор через {retry_delay} сек.")
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

        # Скриншот страницы проекта
        if screenshot_path and Path(str(screenshot_path)).exists():
            if await self._safe_send_photo(target, str(screenshot_path), text, reply_markup=keyboard):
                pass
            else:
                await self._safe_send_message(target, text, reply_markup=keyboard)
        else:
            await self._safe_send_message(target, text, reply_markup=keyboard)

        # Прикреплённые файлы проекта
        await self._send_project_attachments(target, candidate)

    async def _send_project_attachments(self, target: str, candidate: dict[str, Any]) -> None:
        """Отправить файлы проекта (фото, PDF, документы) в Telegram."""
        platform_data = candidate.get("platform_data") or {}
        if not isinstance(platform_data, dict):
            return

        files = platform_data.get("files") or []
        if not isinstance(files, list) or not files:
            return

        from src.utils.telegram_transport import bot_api_send_document

        _MIME_FIXES = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".zip": "application/zip",
            ".rar": "application/vnd.rar",
        }

        sent_count = 0
        max_files = int(os.getenv("TG_MAX_ATTACHMENTS", "5"))

        for file_info in files[:max_files]:
            if not isinstance(file_info, dict):
                continue
            url = str(file_info.get("url") or "").strip()
            name = str(
                file_info.get("fname") or file_info.get("name") or file_info.get("filename") or "attachment"
            ).strip()
            if not url:
                continue

            try:
                downloaded_path = await self._download_attachment(url, name, candidate.get("platform", "kwork"))
                if downloaded_path and Path(downloaded_path).exists():
                    file_size = Path(downloaded_path).stat().st_size
                    if file_size < 100:
                        logger.warning(
                            f"TelegramNotifier: файл {name} слишком маленький ({file_size} bytes), возможно ошибка"
                        )
                        continue
                    if file_size > 50_000_000:
                        logger.warning(f"TelegramNotifier: файл {name} слишком большой ({file_size} bytes), пропуск")
                        continue

                    # Fix MIME type based on extension
                    ext = Path(downloaded_path).suffix.lower()
                    correct_mime = _MIME_FIXES.get(ext)
                    if correct_mime:
                        logger.debug(f"TelegramNotifier: MIME fix for {ext} → {correct_mime}")

                    caption = f"📎 {name}"
                    result = await bot_api_send_document(downloaded_path, caption=caption, chat_id=target)
                    if result.ok:
                        sent_count += 1
                        logger.info(f"TelegramNotifier: файл {name} отправлен в Telegram ({file_size} bytes)")
                    else:
                        logger.warning(f"TelegramNotifier: не удалось отправить файл {name}: {result.detail}")
                else:
                    logger.warning(f"TelegramNotifier: файл {name} не скачан")
            except Exception as e:
                logger.debug(f"TelegramNotifier: ошибка отправки файла {name}: {e}")

        if sent_count > 0:
            logger.info(f"TelegramNotifier: отправлено {sent_count} файлов проекта в Telegram")

    async def _download_attachment(self, url: str, name: str, platform: str) -> Optional[str]:
        """Скачать файл проекта для отправки в Telegram.

        Cookies берутся из Session Hub (не из aiohttp session — там только API cookies,
        а файлы лежат на web-домене kwork.ru и требуют web-сессию).
        """
        try:
            import tempfile
            import httpx
            from src.utils.vpnte_proxy import kwork_http_proxy_url

            # Получаем cookies из Session Hub
            cookies = {}
            if platform == "kwork":
                hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
                try:
                    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                        resp = await client.get(f"{hub_url}?domain=kwork.ru")
                        if resp.status_code == 200:
                            data = resp.json()
                            for ck in data.get("cookies", []):
                                cookies[ck.get("name", "")] = ck.get("value", "")
                except Exception as e:
                    logger.debug(f"TelegramNotifier: Session Hub недоступен для cookies: {e}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0.0.0 Safari/537.36",
            }
            if platform == "kwork":
                headers["Referer"] = "https://kwork.ru/projects"

            # Делаем URL абсолютным
            if url.startswith("/"):
                url = f"https://kwork.ru{url}"

            tmp_dir = Path(tempfile.gettempdir()) / "psr_attachments"
            tmp_dir.mkdir(parents=True, exist_ok=True)

            safe_name = re.sub(r"[^\w.\-() ]", "_", name)
            file_path = tmp_dir / safe_name

            async with httpx.AsyncClient(
                timeout=60,
                follow_redirects=True,
                proxy=kwork_http_proxy_url(rotate=False) if platform == "kwork" else None,
                trust_env=False,
                cookies=cookies,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    if "text/html" in ct:
                        logger.warning(f"TelegramNotifier: файл {name} вернул HTML (auth fail)")
                        return None
                    file_path.write_bytes(resp.content)
                    logger.info(f"TelegramNotifier: файл {name} скачан ({len(resp.content)} bytes, type={ct})")
                    return str(file_path)
                else:
                    logger.warning(f"TelegramNotifier: не удалось скачать {url}: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.debug(f"TelegramNotifier: ошибка скачивания файла {name}: {e}")
            return None

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
        if not _env_bool("TELEGRAM_DIGEST_ENABLED", False):
            return
        interval = int(os.getenv("DIGEST_INTERVAL_MIN", "60"))
        if interval <= 0:
            return
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
            f"{item['platform']}: sent={item['total']}, replied={item['responded']}" for item in platform_stats
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

    async def notify_response(
        self,
        project_title: str,
        platform: str,
        response_text: str,
        *,
        project_id: str = "",
    ):
        if not self.bot or not self.admin_id:
            return
        header = f"Ответ от заказчика\n\nПроект: {project_title}\nПлатформа: {platform}"
        if project_id:
            header += f"\nID: {project_id}"
        msg = f"{header}\n\n{response_text[:1200]}"
        await self._safe_send_message(self.admin_id, msg)

    async def notify_assignment(
        self,
        project_title: str,
        order_id: str,
        deadline: str = "",
    ):
        """Urgent alert: клиент назначил заказ. У вас 24 часа на подтверждение.

        Отказ = -50% рейтинга, потеря всех connects.
        """
        if not self.bot or not self.admin_id:
            return
        msg = f"URGENT: Заказ назначен вам!\n\nПроект: {project_title}\nЗаказ #{order_id}\n"
        if deadline:
            msg += f"Дедлайн: {deadline}\n"
        msg += (
            "\nУ вас 24 часа на подтверждение.\n"
            "Отказ = -50% рейтинга + потеря connects.\n"
            "Подтвердите заказ на kwork.ru прямо сейчас!"
        )
        await self._safe_send_message(self.admin_id, msg)

    async def _run_candidate_executor(
        self, candidate_id: int, action: str, payload: dict[str, Any] | None = None
    ) -> str:
        if self._candidate_executor is None:
            return f"Исполнитель не привязан. Действие {action} не выполнено."
        try:
            return await self._candidate_executor(candidate_id, action, payload)
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

    async def notify_skipped(self, skipped_log: list[dict[str, str]], chat_id: Optional[str] = None) -> None:
        target = chat_id or self.admin_id
        if not target or not self.bot:
            return
        if not skipped_log:
            return

        by_stage: dict[str, int] = {}
        for entry in skipped_log:
            stage = entry.get("stage", "?")
            by_stage[stage] = by_stage.get(stage, 0) + 1
        stages_summary = ", ".join(f"{s}={c}" for s, c in sorted(by_stage.items()))

        max_show = 15
        lines: list[str] = []
        for entry in skipped_log[:max_show]:
            title = entry.get("title", "")[:50]
            pid = entry.get("project_id", "")[:12]
            stage = entry.get("stage", "?")
            reason = entry.get("reason", "")[:80]
            budget = entry.get("budget", "")
            budget_str = f" 💰{budget}" if budget else ""
            lines.append(f"❌ [{stage}] {title}{budget_str}\n   id={pid} | {reason}")
        if len(skipped_log) > max_show:
            lines.append(f"… и ещё {len(skipped_log) - max_show}")

        text = f"📋 Пропущено {len(skipped_log)} заказов ({stages_summary}):\n\n" + "\n\n".join(lines)
        await self._safe_send_message(target, text)
