"""
Проверка входящих сообщений на фриланс-платформах.
Уведомление в Telegram при получении новых ответов.
"""

import os
import re
from typing import Optional, List, Dict
from datetime import datetime
from loguru import logger
import httpx


class InboxChecker:
    """Базовый класс для проверки входящих сообщений."""

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения и вернуть список ответов."""
        raise NotImplementedError


class KworkInboxChecker(InboxChecker):
    """Проверка входящих сообщений на Kwork через API."""

    def __init__(self):
        self._api = None
        self._last_check = None

    def _get_api(self):
        if self._api is not None:
            return self._api

        from kwork import Kwork

        email = os.getenv("KWORK_EMAIL")
        password = os.getenv("KWORK_PASSWORD")
        proxy_url = os.getenv("PROXY_URL") or None

        if not email or not password:
            logger.warning("KworkInbox: нет KWORK_EMAIL/KWORK_PASSWORD")
            return None

        self._api = Kwork(
            login=email,
            password=password,
            timeout=30.0,
            retry_max_attempts=2,
            proxy=proxy_url,
        )
        return self._api

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения в чатах Kwork."""
        api = self._get_api()
        if not api:
            return []

        new_responses = []

        try:
            # Получаем список диалогов
            inbox = await api.inbox_get_dialogs()
            
            for dialog in inbox.get("data", []):
                project_id = dialog.get("project_id", "")
                unread = dialog.get("unread", 0)
                
                if unread > 0:
                    # Есть непрочитанные сообщения
                    last_message = dialog.get("last_message", "")
                    project_title = dialog.get("project_name", "Неизвестный проект")
                    
                    if last_message:
                        new_responses.append({
                            "platform": "kwork",
                            "project_id": str(project_id),
                            "project_title": project_title,
                            "message": last_message,
                            "timestamp": datetime.now().isoformat(),
                            "sender": "customer",
                        })

            logger.info(f"KworkInbox: найдено {len(new_responses)} новых сообщений")
            return new_responses

        except Exception as e:
            logger.error(f"KworkInbox: ошибка проверки: {e}")
            return []

    def close(self):
        if self._api:
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(self._api.close())
                else:
                    loop.run_until_complete(self._api.close())
            except Exception:
                pass


class InboxMonitor:
    """Мониторинг входящих сообщений со всех платформ."""

    def __init__(self, notifier=None):
        self.notifier = notifier
        self.checkers: Dict[str, InboxChecker] = {}
        self._init_checkers()

    def _init_checkers(self):
        """Инициализация чекеров для активных платформ."""
        platforms = os.getenv("PLATFORMS", "kwork").lower().split(",")
        
        if "kwork" in platforms:
            try:
                self.checkers["kwork"] = KworkInboxChecker()
                logger.info("InboxMonitor: инициализирован Kwork чекер")
            except Exception as e:
                logger.error(f"InboxMonitor: ошибка инициализации Kwork: {e}")

    async def check_all(self):
        """Проверить все платформы и отправить уведомления."""
        all_responses = []
        
        for platform, checker in self.checkers.items():
            try:
                responses = await checker.check_new_messages()
                all_responses.extend(responses)
                
                if responses and self.notifier:
                    for resp in responses:
                        await self.notifier.notify_response(
                            project_title=resp["project_title"],
                            platform=resp["platform"],
                            response_text=resp["message"],
                        )
            except Exception as e:
                logger.error(f"InboxMonitor: ошибка проверки {platform}: {e}")

        return all_responses

    def close(self):
        for checker in self.checkers.values():
            try:
                checker.close()
            except Exception:
                pass
