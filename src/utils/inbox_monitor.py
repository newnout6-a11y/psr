"""
Проверка входящих сообщений на фриланс-платформах.
Уведомление в Telegram при получении новых ответов.
"""

import os
from datetime import datetime
from typing import Any, Dict, List

from loguru import logger

from src.platforms.kwork import get_kwork_service


class InboxChecker:
    """Базовый класс для проверки входящих сообщений."""

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения и вернуть список ответов."""
        raise NotImplementedError


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Универсальный доступ: pydantic-модель / dict / объект."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class KworkInboxChecker(InboxChecker):
    """Проверка входящих сообщений на Kwork через мобильный API.

    Использует реальные поля `kwork.schema.DialogMessage`:
    `username`, `last_message`, `unread_count`, `time`, `is_online`,
    `has_active_order`, `link`. Поля `project_id`/`project_name`
    у этого ответа НЕТ — они доступны только внутри отдельных
    `InboxMessage.inbox_order/custom_request`, поэтому проектный
    контекст пробрасываем как `Чат с @username` и оставляем
    `project_id` пустым; точное связывание с кандидатом делается
    вызывающей стороной по `username`.
    """

    def __init__(self) -> None:
        self.kwork_service = get_kwork_service()
        self._seen: set[tuple[str, int]] = set()

    def _get_api(self):
        return self.kwork_service.get_api()

    async def check_new_messages(self) -> List[Dict]:
        api = self._get_api()
        if not api:
            return []

        try:
            dialogs = await api.get_all_dialogs()
        except Exception as e:
            logger.error(f"KworkInbox: ошибка проверки: {e}")
            return []

        # API может вернуть list[DialogMessage] или dict с ключом "data" в очень
        # старых версиях библиотеки — поддерживаем оба варианта.
        if isinstance(dialogs, dict):
            dialogs = dialogs.get("data") or []

        new_responses: List[Dict] = []
        for dialog in dialogs or []:
            username = (_attr(dialog, "username") or "").strip()
            if not username:
                continue

            unread_count = int(_attr(dialog, "unread_count") or 0)
            unread_flag = bool(_attr(dialog, "unread"))
            last_message = (_attr(dialog, "last_message") or "").strip()

            # `unread_count` обычно отражает фактическое число непрочитанных,
            # `unread` — bool-флаг «есть непрочитанное». Достаточно одного.
            if not last_message or (unread_count <= 0 and not unread_flag):
                continue

            time_value = _attr(dialog, "time")
            try:
                time_int = int(time_value) if time_value is not None else 0
            except (TypeError, ValueError):
                time_int = 0

            # Дедупликация: один и тот же `(username, time)` не уведомляем повторно.
            seen_key = (username, time_int)
            if seen_key in self._seen:
                continue
            self._seen.add(seen_key)

            link = _attr(dialog, "link") or f"https://kwork.ru/inbox/{username}"
            has_order = bool(_attr(dialog, "has_active_order"))
            blocked = bool(_attr(dialog, "blocked_by_user"))
            allowed = _attr(dialog, "allowed_dialog")
            is_online = bool(_attr(dialog, "is_online"))

            project_title = "Активный заказ" if has_order else f"Чат с @{username}"

            timestamp = (
                datetime.fromtimestamp(time_int).isoformat()
                if time_int
                else datetime.now().isoformat()
            )

            new_responses.append({
                "platform": "kwork",
                "project_id": "",
                "project_title": project_title,
                "username": username,
                "user_id": _attr(dialog, "user_id"),
                "message": last_message,
                "timestamp": timestamp,
                "sender": "customer",
                "link": link,
                "is_online": is_online,
                "has_active_order": has_order,
                "blocked": blocked,
                "allowed_dialog": allowed,
                "unread_count": unread_count,
            })

        if new_responses:
            logger.info(f"KworkInbox: найдено {len(new_responses)} новых сообщений")
        else:
            logger.debug("KworkInbox: новых сообщений нет")
        return new_responses

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
