import asyncio
import os
import re
from contextlib import suppress
from typing import Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from loguru import logger


class TelegramNotifier:
    """Уведомления и подтверждения через Telegram."""

    _instance = None
    _approval_events: Dict[str, asyncio.Event] = {}
    _approval_results: Dict[str, Optional[str]] = {}
    _awaiting_custom_price: Dict[str, bool] = {}
    _awaiting_edit: Dict[str, bool] = {}
    _edited_proposals: Dict[str, str] = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TelegramNotifier, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "bot"):
            return

        self.token = os.getenv("TELEGRAM_TOKEN")
        self.admin_id = os.getenv("ADMIN_CHAT_ID")
        self.timeout = int(os.getenv("APPROVAL_TIMEOUT", "900"))

        if not self.token:
            logger.error("TelegramNotifier: TELEGRAM_TOKEN не найден в .env")
            return

        self.bot = Bot(token=self.token)
        self.dp = Dispatcher()
        self._setup_handlers()
        self.running_task = None

    def _setup_handlers(self):
        """Настройка обработчиков сообщений и inline-кнопок."""

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            self.admin_id = str(message.chat.id)
            self._save_id_to_env(self.admin_id)
            logger.info(f"TelegramNotifier: Новый админ Chat ID: {self.admin_id}")
            await message.answer(
                f"Привет! Теперь я буду присылать сюда заказы.\nТвой ID: {self.admin_id}\n(Я запомнил его в .env)"
            )

        @self.dp.callback_query(F.data.startswith("price:"))
        async def handle_price_button(callback: CallbackQuery):
            """Обработка нажатия inline-кнопки с ценой."""
            if self.admin_id and str(callback.from_user.id) != str(self.admin_id):
                await callback.answer("Не авторизован", show_alert=True)
                return

            parts = callback.data.split(":")
            project_id = parts[1]
            action = parts[2]

            if project_id not in self._approval_events:
                await callback.answer("Запрос уже обработан или устарел", show_alert=True)
                return

            if action == "skip":
                self._approval_results[project_id] = None
                self._approval_events[project_id].set()
                await callback.answer("Пропущено")
                await callback.message.edit_reply_markup(reply_markup=None)
                return

            if action == "custom":
                self._awaiting_custom_price[project_id] = True
                await callback.answer("Пришли цену числом в чат")
                await callback.message.reply("Введи свою цену (числом):")
                return

            # action = конкретная цена
            price = action
            self._approval_results[project_id] = price
            logger.info(f"TelegramNotifier: выбрана цена {price} для проекта {project_id}")
            try:
                await callback.answer(f"Принято: {price} руб.")
                await callback.message.edit_reply_markup(reply_markup=None)
            finally:
                self._approval_events[project_id].set()

        @self.dp.callback_query(F.data.startswith("edit:"))
        async def handle_edit_button(callback: CallbackQuery):
            """Обработка нажатия кнопки Изменить отклик."""
            if self.admin_id and str(callback.from_user.id) != str(self.admin_id):
                await callback.answer("Не авторизован", show_alert=True)
                return

            parts = callback.data.split(":")
            project_id = parts[1]

            await callback.answer("Введи новый текст отклика")
            await callback.message.reply("✏️ Введи отредактированный текст отклика:")

            # Устанавливаем флаг ожидания редактирования
            self._awaiting_edit[project_id] = True

        @self.dp.message(F.text)
        async def handle_text_input(message: types.Message):
            """Единый обработчик текстового ввода: редактирование отклика или кастомная цена."""
            if self.admin_id and str(message.chat.id) != str(self.admin_id):
                return

            text = (message.text or "").strip()

            # Приоритет 1: редактирование текста отклика
            for pid, waiting in list(self._awaiting_edit.items()):
                if waiting:
                    if len(text) < 50:
                        await message.answer("Текст слишком короткий. Минимум 50 символов.")
                        return
                    self._awaiting_edit.pop(pid, None)
                    self._edited_proposals[pid] = text
                    logger.info(f"TelegramNotifier: текст отклика отредактирован для проекта {pid}")
                    await message.answer(
                        f"✅ Текст отклика обновлен для проекта {pid}\nТеперь выбери цену кнопками выше."
                    )
                    return

            # Приоритет 2: кастомная цена
            waiting_project = None
            for pid, waiting in self._awaiting_custom_price.items():
                if waiting and pid in self._approval_events:
                    waiting_project = pid
                    break

            if not waiting_project:
                return

            normalized_price = self._extract_price(text)
            if not normalized_price:
                await message.answer("Пришли цену числом, например: 1500")
                return

            self._awaiting_custom_price.pop(waiting_project, None)
            self._approval_results[waiting_project] = normalized_price
            logger.info(f"TelegramNotifier: кастомная цена {normalized_price} для проекта {waiting_project}")
            try:
                await message.answer(f"Принято. Ставим цену {normalized_price} руб. для проекта {waiting_project}")
            finally:
                self._approval_events[waiting_project].set()

    def _save_id_to_env(self, chat_id: str):
        """Вспомогательный метод для сохранения Chat ID в .env"""
        try:
            with open(".env", "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            found = False
            for line in lines:
                if line.startswith("ADMIN_CHAT_ID="):
                    new_lines.append(f"ADMIN_CHAT_ID={chat_id}\n")
                    found = True
                else:
                    new_lines.append(line)

            if not found:
                new_lines.append(f"ADMIN_CHAT_ID={chat_id}\n")

            with open(".env", "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            logger.warning(f"Не удалось сохранить ADMIN_CHAT_ID в .env: {e}")

    def _extract_price(self, text: str) -> Optional[str]:
        """Достаёт цену из ответа пользователя."""
        digits = re.sub(r"[^\d]", "", text)
        if not digits:
            return None
        return str(int(digits))

    def get_edited_proposal(self, project_id: str) -> Optional[str]:
        """Получить отредактированный текст отклика если есть."""
        return self._edited_proposals.pop(project_id, None)

    async def start(self):
        """Запуск слушателя бота в фоновом режиме."""
        if self.running_task:
            return
        logger.info("TelegramNotifier: Запуск бота...")
        self.running_task = asyncio.create_task(self.dp.start_polling(self.bot))

    async def stop(self):
        if self.running_task:
            self.running_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.running_task
            await self.bot.session.close()
            self.running_task = None
            logger.info("TelegramNotifier: Бот остановлен")

    async def request_approval(
        self,
        project_id: str,
        project_title: str,
        proposal_text: str,
        budget: Optional[float] = None,
        screenshot_path: Optional[str] = None,
    ) -> Optional[str]:
        """Отправляет запрос на одобрение с inline-кнопками и ждет ответа."""
        if not self.admin_id:
            logger.warning("TelegramNotifier: ADMIN_CHAT_ID не установлен. Напиши /start боту!")
            return None

        event = asyncio.Event()
        self._approval_events[project_id] = event

        # Формируем сообщение
        budget_str = f"{int(budget)} руб." if budget else "не указан"
        msg = (
            f"🔔 *Новый проект на Kwork!*\n\n"
            f"📌 *Заголовок:* {project_title}\n"
            f"🆔 *ID:* {project_id}\n"
            f"💰 *Бюджет:* {budget_str}\n\n"
            f"📝 *Текст отклика:*\n{proposal_text}\n\n"
            f"Выбери цену:"
        )

        # Строим inline-кнопки с вариантами цен
        buttons = []
        if budget and budget > 0:
            base = int(budget)
            buttons.append(
                [
                    InlineKeyboardButton(text=f"{base} руб.", callback_data=f"price:{project_id}:{base}"),
                    InlineKeyboardButton(
                        text=f"{int(base * 1.2)} руб. (+20%)", callback_data=f"price:{project_id}:{int(base * 1.2)}"
                    ),
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{int(base * 1.5)} руб. (+50%)", callback_data=f"price:{project_id}:{int(base * 1.5)}"
                    ),
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(text="Своя цена", callback_data=f"price:{project_id}:custom"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"price:{project_id}:skip"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(text="✏️ Изменить отклик", callback_data=f"edit:{project_id}"),
            ]
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            if screenshot_path and os.path.exists(screenshot_path):
                photo = FSInputFile(screenshot_path)
                await self.bot.send_photo(
                    self.admin_id, photo, caption=msg[:1024], parse_mode="Markdown", reply_markup=keyboard
                )
                # Удаляем скриншот после отправки
                try:
                    os.remove(screenshot_path)
                except OSError:
                    pass
            else:
                await self.bot.send_message(self.admin_id, msg, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"TelegramNotifier: Ошибка отправки: {e}")
            self._approval_events.pop(project_id, None)
            self._approval_results.pop(project_id, None)
            return None

        try:
            await asyncio.wait_for(event.wait(), timeout=self.timeout)
            price = self._approval_results.get(project_id)
            return price
        except asyncio.TimeoutError:
            logger.warning(f"TelegramNotifier: Таймаут ожидания для проекта {project_id}")
            return None
        finally:
            self._approval_events.pop(project_id, None)
            self._approval_results.pop(project_id, None)
            self._awaiting_custom_price.pop(project_id, None)

    async def notify_response(self, project_title: str, platform: str, response_text: str):
        """Уведомить о получении ответа от заказчика."""
        if not self.admin_id:
            return

        msg = (
            f"💬 *Ответ от заказчика!*\n\n"
            f"📌 *Проект:* {project_title}\n"
            f"🌐 *Платформа:* {platform}\n\n"
            f"📨 *Сообщение:*\n`{response_text[:500]}`"
        )

        if len(response_text) > 500:
            msg += "\n\n_(сообщение обрезано)_"

        try:
            await self.bot.send_message(self.admin_id, msg, parse_mode="Markdown")
            logger.info(f"TelegramNotifier: уведомление об ответе отправлено для {project_title}")
        except Exception as e:
            logger.error(f"TelegramNotifier: ошибка отправки уведомления: {e}")

    async def notify_new_project_with_prices(
        self,
        project_title: str,
        project_url: str,
        budget: float,
        platform: str,
        proposal_text: str,
        screenshot_path: Optional[str] = None,
        project_id: str = "",
    ):
        """Отправить уведомление с кнопками цен (для dry-run режима)."""
        if not self.admin_id:
            logger.warning("TelegramNotifier: ADMIN_CHAT_ID не задан, уведомление не отправлено")
            return

        budget_str = f"{int(budget)} руб." if budget else "не указан"
        msg = (
            f"🔔 *Новый проект ({platform})*\n\n"
            f"📌 *{project_title}*\n"
            f"🆔 *ID:* {project_id}\n"
            f"💰 *Бюджет:* {budget_str}\n\n"
            f"📝 *Текст отклика:*\n{proposal_text}\n\n"
            f"*Выбери цену:*"
        )

        # Кнопки с ценами (как в реальном режиме)
        buttons = []
        if budget and budget > 0:
            base = int(budget)
            buttons.append(
                [
                    InlineKeyboardButton(text=f"{base} руб.", callback_data=f"price:{project_id}:{base}"),
                    InlineKeyboardButton(
                        text=f"{int(base * 1.2)} руб. (+20%)", callback_data=f"price:{project_id}:{int(base * 1.2)}"
                    ),
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{int(base * 1.5)} руб. (+50%)", callback_data=f"price:{project_id}:{int(base * 1.5)}"
                    ),
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton(text="Своя цена", callback_data=f"price:{project_id}:custom"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"price:{project_id}:skip"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(text="✏️ Изменить отклик", callback_data=f"edit:{project_id}"),
            ]
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            if screenshot_path and os.path.exists(screenshot_path):
                photo = FSInputFile(screenshot_path)
                await self.bot.send_photo(
                    self.admin_id,
                    photo,
                    caption=msg,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                await self.bot.send_message(
                    self.admin_id,
                    msg,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            logger.info(f"TelegramNotifier: уведомление с ценами отправлено: {project_title}")
            # Удаляем скриншот после отправки
            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    os.remove(screenshot_path)
                except OSError:
                    pass
        except Exception as e:
            logger.error(f"TelegramNotifier: ошибка отправки уведомления: {e}")
