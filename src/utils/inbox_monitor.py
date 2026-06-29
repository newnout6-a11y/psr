"""
Проверка входящих сообщений на фриланс-платформах.
Сохранение полной истории диалогов в БД, уведомление в Telegram.
Watermark персистится в runtime_state — нет дублирования между циклами.
"""

import os
import asyncio
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

    def __init__(self, db=None):
        self.kwork_service = get_kwork_service()
        self.db = db

    def _get_watermark(self) -> str:
        if self.db:
            return self.db.get_runtime_state("inbox.kwork.watermark", "1970-01-01 00:00:00")
        return "1970-01-01 00:00:00"

    def _set_watermark(self, ts: str) -> None:
        if self.db:
            self.db.set_runtime_state("inbox.kwork.watermark", ts)

    async def _get_api(self):
        return await self.kwork_service.get_api()

    async def check_new_messages(self) -> List[Dict]:
        """Проверить новые сообщения в чатах Kwork.

        Использует watermark из runtime_state для фильтрации уже обработанных диалогов.
        Watermark = timestamp последней успешной проверки.
        """
        api = await self._get_api()
        if not api:
            return []

        new_responses = []

        try:
            watermark = self._get_watermark()
            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            inbox = await api.get_all_dialogs()
            dialogs = inbox if isinstance(inbox, list) else inbox.get("data", [])

            for dialog in dialogs:
                if isinstance(dialog, dict):
                    project_id = str(dialog.get("project_id", ""))
                    unread = dialog.get("unread", 0)
                    last_message = dialog.get("last_message", "")
                    project_title = dialog.get("project_name", "Неизвестный проект")
                    username = dialog.get("username", dialog.get("user_name", ""))
                    dialog_id = str(dialog.get("id", dialog.get("dialog_id", "")))
                    last_message_at = dialog.get("last_message_at", dialog.get("updated_at", ""))
                else:
                    project_id = str(getattr(dialog, "project_id", ""))
                    unread = getattr(dialog, "unread", 0)
                    last_message = getattr(dialog, "last_message", "")
                    project_title = getattr(dialog, "project_name", "Неизвестный проект")
                    username = getattr(dialog, "username", getattr(dialog, "user_name", ""))
                    dialog_id = str(getattr(dialog, "id", getattr(dialog, "dialog_id", "")))
                    last_message_at = getattr(dialog, "last_message_at", getattr(dialog, "updated_at", ""))

                if not (unread > 0 and last_message and project_id):
                    continue

                if last_message_at and last_message_at <= watermark:
                    continue

                response = {
                    "platform": "kwork",
                    "project_id": project_id,
                    "project_title": project_title,
                    "message": last_message,
                    "timestamp": now_ts,
                    "sender": "customer",
                    "sender_username": username,
                    "dialog_id": dialog_id,
                    "last_message_at": str(last_message_at),
                }
                new_responses.append(response)

            max_ts = max((r.get("last_message_at", now_ts) for r in new_responses), default=now_ts)
            self._set_watermark(max_ts)
            logger.info(f"KworkInbox: найдено {len(new_responses)} новых сообщений (watermark → {max_ts})")
            return new_responses

        except Exception as e:
            logger.error(f"KworkInbox: ошибка проверки: {e}")
            return []

    async def get_full_dialog(self, username: str) -> list[dict]:
        """Получить полную историю диалога с клиентом через KworkExtensions."""
        try:
            from src.platforms.kwork_ext import KworkExtensions

            api = await self._get_api()
            if not api:
                return []
            return await KworkExtensions.get_dialog_history(api, username)
        except Exception as e:
            logger.debug(f"KworkInbox: не удалось получить историю диалога: {e}")
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
    """Мониторинг входящих сообщений со всех платформ.

    Сохраняет полную историю сообщений в conversations/conversation_messages.
    Уведомляет в Telegram через notifier.
    Персистит watermark в runtime_state — нет дублирования между циклами.

    Поддерживает fast polling — отдельный режим для частой проверки (5 мин)
    без запуска полного цикла оркестратора.
    """

    def __init__(self, notifier=None, db=None):
        self.notifier = notifier
        self.db = db
        self.checkers: Dict[str, InboxChecker] = {}
        self._fast_polling_task = None
        self._init_checkers()

    def _init_checkers(self):
        """Инициализация чекеров для активных платформ."""
        platforms = os.getenv("PLATFORMS", "kwork").lower().split(",")

        if "kwork" in platforms:
            try:
                self.checkers["kwork"] = KworkInboxChecker(db=self.db)
                logger.info("InboxMonitor: инициализирован Kwork чекер")
            except Exception as e:
                logger.error(f"InboxMonitor: ошибка инициализации Kwork: {e}")

    async def start_fast_polling(self, interval: float = 300.0) -> None:
        """Запустить fast polling — проверка входящих каждые N секунд.

        Работает независимо от цикла оркестратора.
        Предотвращает 'упущенные заказы' — Kwork штрафует за медленные ответы.
        Также проверяет auto-assignments.
        """
        if self._fast_polling_task is not None:
            return
        interval = float(os.getenv("KWORK_INBOX_POLL_INTERVAL", str(interval)))
        logger.info(f"InboxMonitor: fast polling запущен (интервал {interval}s)")

        async def _poll_loop():
            while True:
                try:
                    await self.check_all()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"InboxMonitor: fast polling error: {e}")
                await asyncio.sleep(interval)

        import asyncio as _asyncio

        self._fast_polling_task = _asyncio.create_task(_poll_loop())

    async def stop_fast_polling(self) -> None:
        if self._fast_polling_task is not None:
            self._fast_polling_task.cancel()
            try:
                await self._fast_polling_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._fast_polling_task = None
            logger.info("InboxMonitor: fast polling остановлен")

    async def check_all(self) -> list[dict]:
        """Проверить все платформы, сохранить сообщения в БД, отправить уведомления.

        Также проверяет назначенные заказы (auto-assignment) — если клиент
        назначил заказ фрилансеру без обсуждения, отправляется urgent alert.
        """
        all_responses = []

        for platform, checker in self.checkers.items():
            try:
                responses = await checker.check_new_messages()
                all_responses.extend(responses)

                for resp in responses:
                    is_new = self._persist_message(resp)
                    if is_new and self.notifier:
                        await self.notifier.notify_response(
                            project_title=resp["project_title"],
                            platform=resp["platform"],
                            response_text=resp["message"],
                            project_id=resp["project_id"],
                        )
                        if resp.get("dialog_id") and resp["platform"] == "kwork":
                            try:
                                from src.platforms.kwork import get_kwork_service as _get_svc

                                _svc = _get_svc()
                                dialog_id = int(resp["dialog_id"])
                                await _svc.mark_inbox_read(dialog_id)
                                logger.debug(f"InboxMonitor: диалог {dialog_id} помечен прочитанным")
                            except Exception:
                                pass
            except Exception as e:
                logger.error(f"InboxMonitor: ошибка проверки {platform}: {e}")

        await self._check_assignments()

        return all_responses

    async def _check_assignments(self) -> None:
        """Проверить назначенные заказы на Kwork (auto-assignment trap).

        Клиент может назначить заказ без обсуждения. У фрилансера 24ч на подтверждение.
        Отказ = -50% рейтинга, потеря всех connects.

        Также проверяет response time — если входящее сообщение не отвечено >2ч,
        отправляет alert (предотвращает 'упущенные заказы' metric).
        """
        if not self.db or not self.notifier:
            return

        await self._check_response_times()
        await self._check_review_reminders()

        last_check = self.db.get_runtime_state("inbox.assignments.watermark", "1970-01-01 00:00:00")

        try:
            from src.platforms.kwork import get_kwork_service
            from src.platforms.kwork_ext import KworkExtensions

            service = get_kwork_service()
            api = await service.get_api()
            if not api:
                return

            orders = await KworkExtensions.get_worker_orders(api, status_filter="all")
            if not orders:
                return

            now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_assignments = []

            for order in orders:
                if not isinstance(order, dict):
                    continue

                order_status = str(order.get("status", "")).lower()
                order_id = str(order.get("id", order.get("order_id", "")))
                created_at = str(order.get("date_create", order.get("created_at", "")))

                if (
                    order_status in {"new", "assigned", "pending", "wait_payment", "wait_confirm"}
                    and created_at
                    and created_at > last_check
                ):
                    project_title = order.get("project_name", order.get("title", "Неизвестный проект"))
                    project_id = str(order.get("project_id", order.get("want_id", "")))
                    deadline = order.get("date_deadline", order.get("deadline", ""))

                    new_assignments.append(
                        {
                            "order_id": order_id,
                            "project_id": project_id,
                            "project_title": project_title,
                            "status": order_status,
                            "deadline": str(deadline),
                            "created_at": created_at,
                        }
                    )

            if new_assignments:
                auto_approve = os.getenv("KWORK_AUTO_APPROVE_ORDERS", "false").lower() in {"1", "true", "yes", "on"}
                for assignment in new_assignments:
                    await self.notifier.notify_assignment(
                        project_title=assignment["project_title"],
                        order_id=assignment["order_id"],
                        deadline=assignment["deadline"],
                    )
                    if auto_approve:
                        order_id_int = int(assignment["order_id"]) if str(assignment["order_id"]).isdigit() else None
                        if order_id_int:
                            result = await service.approve_order(order_id_int)
                            if result:
                                logger.info(f"InboxMonitor: заказ #{order_id_int} автоматически принят!")
                                await self.notifier.send_text(f"Заказ #{order_id_int} автоматически принят!")
                            else:
                                logger.error(f"InboxMonitor: не удалось автоматически принять заказ #{order_id_int}")

                self.db.set_runtime_state("inbox.assignments.watermark", now_ts)
                logger.warning(f"InboxMonitor: обнаружено {len(new_assignments)} новых назначенных заказов!")

        except Exception as e:
            logger.debug(f"InboxMonitor: не удалось проверить назначения: {e}")

    async def _check_response_times(self) -> None:
        """Проверить время ответа на сообщения клиентов.

        Kwork penalizes 'упущенные заказы' — если клиент написал, а вы не ответили
        и он заказал у другого в течение 5 дней → штраф рейтингу.
        Alert при непрочитанных сообщениях старше 2 часов.
        """
        if not self.db or not self.notifier:
            return

        try:
            from datetime import datetime

            threshold_hours = int(os.getenv("KWORK_RESPONSE_TIME_ALERT_HOURS", "2"))
            now = datetime.utcnow()

            convs = self.db.get_active_conversations(limit=50)
            slow_responses = []

            for conv in convs:
                if not isinstance(conv, dict):
                    continue
                status = conv.get("status", "")
                last_msg_at = conv.get("last_message_at", "")

                if status != "awaiting_reply" or not last_msg_at:
                    continue

                try:
                    msg_time = datetime.fromisoformat(last_msg_at.replace(" ", "T"))
                    elapsed = (now - msg_time).total_seconds() / 3600

                    if elapsed > threshold_hours:
                        project_title = conv.get("project_title", "Без названия")
                        platform = conv.get("platform", "kwork")
                        project_id = conv.get("project_id", "")
                        slow_responses.append(
                            {
                                "title": project_title,
                                "platform": platform,
                                "project_id": project_id,
                                "elapsed_hours": round(elapsed, 1),
                            }
                        )
                except Exception:
                    continue

            if slow_responses:
                last_alert = self.db.get_runtime_state("inbox.response_alert_ts", "1970-01-01")
                try:
                    last_alert_dt = datetime.fromisoformat(last_alert.replace(" ", "T"))
                    if (now - last_alert_dt).total_seconds() < 3600:
                        return
                except Exception:
                    pass

                lines = []
                for sr in slow_responses[:5]:
                    lines.append(f"  {sr['title'][:40]} — {sr['elapsed_hours']}ч без ответа")

                await self.notifier.send_text(
                    f"SLOW RESPONSE: {len(slow_responses)} диалогов без ответа >{threshold_hours}ч!\n"
                    + "\n".join(lines)
                    + "\nОтветьте в чате Kwork чтобы избежать 'упущенных заказов'."
                )
                self.db.set_runtime_state("inbox.response_alert_ts", now.strftime("%Y-%m-%d %H:%M:%S"))
                logger.warning(f"InboxMonitor: {len(slow_responses)} диалогов с медленным ответом")

        except Exception as e:
            logger.debug(f"InboxMonitor: не удалось проверить response times: {e}")

    async def _check_review_reminders(self) -> None:
        """Напомнить об отзыве для completed заказов без отзыва.

        Kwork: отзывы = главный фактор ранкинга.
        После completed заказа у клиента 30 дней на отзыв.
        Напоминаем фрилансеру через 24ч после completed.
        """
        if not self.db or not self.notifier:
            return

        try:
            from datetime import datetime

            now = datetime.utcnow()
            last_alert = self.db.get_runtime_state("inbox.review_alert_ts", "1970-01-01")
            try:
                last_alert_dt = datetime.fromisoformat(last_alert.replace(" ", "T"))
                if (now - last_alert_dt).total_seconds() < 7200:
                    return
            except Exception:
                pass

            with self.db._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT candidate_id, project_id, platform, title, sent_at
                    FROM candidates
                    WHERE status = 'completed'
                    AND sent_at IS NOT NULL
                    AND candidate_id NOT IN (
                        SELECT candidate_id FROM candidate_actions
                        WHERE action = 'review_sent'
                    )
                    ORDER BY sent_at DESC LIMIT 10
                    """
                ).fetchall()

            pending = []
            for row in rows:
                sent_at = row["sent_at"] if row["sent_at"] else ""
                try:
                    sent_dt = datetime.fromisoformat(sent_at.replace(" ", "T"))
                    elapsed_hours = (now - sent_dt).total_seconds() / 3600
                    if elapsed_hours > 24:
                        pending.append(
                            {
                                "candidate_id": row["candidate_id"],
                                "title": row["title"][:50],
                                "hours": round(elapsed_hours),
                            }
                        )
                except Exception:
                    continue

            if pending:
                lines = [f"  #{p['candidate_id']} {p['title']} — {p['hours']}ч назад" for p in pending[:5]]
                await self.notifier.send_text(
                    f"REVIEW REMINDER: {len(pending)} завершённых заказов без отзыва!\n"
                    + "\n".join(lines)
                    + "\nОтзывы = главный фактор ранкинга на Kwork.\n"
                    "Используй /complete <id> для авто-отзыва."
                )
                self.db.set_runtime_state("inbox.review_alert_ts", now.strftime("%Y-%m-%d %H:%M:%S"))
                logger.info(f"InboxMonitor: {len(pending)} заказов ожидают отзыв")

        except Exception as e:
            logger.debug(f"InboxMonitor: review reminder check failed: {e}")

    def _persist_message(self, resp: dict) -> bool:
        """Сохранить сообщение в conversations + conversation_messages.

        Возвращает True если сообщение новое, False если дубликат.
        """
        if not self.db:
            return True
        try:
            project_id = resp["project_id"]
            platform = resp["platform"]

            candidate = self.db.get_candidate_by_project(project_id, platform)
            candidate_id = candidate.get("candidate_id") if candidate else None

            conv_id = self.db.get_or_create_conversation(
                project_id=project_id,
                platform=platform,
                candidate_id=candidate_id,
                project_title=resp.get("project_title", ""),
            )

            is_new = self.db.add_conversation_message(
                conv_id,
                sender=resp.get("sender", "customer"),
                message_text=resp.get("message", ""),
                platform_message_id=resp.get("dialog_id"),
            )

            if is_new and candidate_id:
                current_status = candidate["status"]
                if current_status in ("auto_sent", "manual_sent", "queued", "auto_ready"):
                    self.db.update_candidate_status(
                        candidate_id,
                        "responded",
                        actor="inbox",
                        reason="client responded",
                        record_action=True,
                    )
            return is_new
        except Exception as e:
            logger.debug(f"InboxMonitor: не удалось сохранить сообщение в БД: {e}")
            return False

    def close(self):
        for checker in self.checkers.values():
            try:
                checker.close()
            except Exception:
                pass
