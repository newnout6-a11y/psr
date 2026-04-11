import asyncio
import os
import re
from contextlib import suppress
from typing import Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from loguru import logger

class TelegramNotifier:
    """Уведомления и подтверждения через Telegram."""
    
    _instance = None
    _approval_events: Dict[str, asyncio.Event] = {}
    _approval_results: Dict[str, Optional[str]] = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(TelegramNotifier, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, 'bot'): return
        
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
        """Настройка обработчиков сообщений."""
        
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            self.admin_id = str(message.chat.id)
            # Сохраняем в .env для будущего использования
            self._save_id_to_env(self.admin_id)
            logger.info(f"TelegramNotifier: Новый админ Chat ID: {self.admin_id}")
            await message.answer(f"Привет! Теперь я буду присылать сюда заказы.\nТвой ID: {self.admin_id}\n(Я запомнил его в .env)")

        @self.dp.message(F.text)
        async def handle_price(message: types.Message):
            if self.admin_id and str(message.chat.id) != str(self.admin_id):
                return

            if not self._approval_events:
                await message.answer("Пока нет активных запросов на подтверждение.")
                return

            text = (message.text or "").strip()
            normalized_price = self._extract_price(text)
            if not normalized_price:
                await message.answer("Пришли цену числом. Можно просто `1500` или `1 500`.", parse_mode="Markdown")
                return

            project_id = next(reversed(self._approval_events))
            self._approval_results[project_id] = normalized_price
            logger.info(f"TelegramNotifier: получена цена {normalized_price} для проекта {project_id}")
            try:
                await message.answer(f"Принято. Ставим цену {normalized_price} руб. для проекта {project_id}")
            finally:
                self._approval_events[project_id].set()

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

    async def start(self):
        """Запуск слушателя бота в фоновом режиме."""
        if self.running_task: return
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
        screenshot_path: Optional[str] = None
    ) -> Optional[str]:
        """Отправляет запрос на одобрение и ждет ответа."""
        if not self.admin_id:
            logger.warning("TelegramNotifier: ADMIN_CHAT_ID не установлен. Напиши /start боту!")
            return None

        event = asyncio.Event()
        self._approval_events[project_id] = event

        # Формируем сообщение
        msg = (
            f"🔔 *Новый проект на Kwork!*\n\n"
            f"📌 *Заголовок:* {project_title}\n"
            f"🆔 *ID:* {project_id}\n\n"
            f"📝 *Текст отклика:*\n`{proposal_text}`\n\n"
            f"❓ *Какую цену ставим?* (Пришли числом в ответ)"
        )

        try:
            if screenshot_path and os.path.exists(screenshot_path):
                photo = FSInputFile(screenshot_path)
                await self.bot.send_photo(self.admin_id, photo, caption=msg[:1024], parse_mode="Markdown")
            else:
                await self.bot.send_message(self.admin_id, msg, parse_mode="Markdown")
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
