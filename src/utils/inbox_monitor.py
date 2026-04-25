"""
Проверка входящих сообщений на фриланс-платформах.
Уведомление в Telegram при получении новых ответов.
"""

import os
from typing import List, Dict
from datetime import datetime
from loguru import logger
from src.platforms.kwork import get_kwork_service


class InboxChecker:
    """Базовый класс для проверки входящих сообщений."""

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения и вернуть список ответов."""
        raise NotImplementedError


class KworkInboxChecker(InboxChecker):
    """Проверка входящих сообщений на Kwork через API."""

    def __init__(self):
        self.kwork_service = get_kwork_service()
        self._last_check = None

    def _get_api(self):
        return self.kwork_service.get_api()

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения в чатах Kwork."""
        api = self._get_api()
        if not api:
            return []

        new_responses = []

        try:
            # Получаем список диалогов
            inbox = await api.get_all_dialogs()

            # API может вернуть list или dict с ключом "data"
            dialogs = inbox if isinstance(inbox, list) else inbox.get("data", [])

            for dialog in dialogs:
                # dialog может быть dict или объект с атрибутами
                if isinstance(dialog, dict):
                    project_id = dialog.get("project_id", "")
                    unread = dialog.get("unread", 0)
                    last_message = dialog.get("last_message", "")
                    project_title = dialog.get("project_name", "Неизвестный проект")
                else:
                    project_id = getattr(dialog, "project_id", "")
                    unread = getattr(dialog, "unread", 0)
                    last_message = getattr(dialog, "last_message", "")
                    project_title = getattr(dialog, "project_name", "Неизвестный проект")

                if unread > 0 and last_message:
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
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.kwork_service.close())
            else:
                loop.run_until_complete(self.kwork_service.close())
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
