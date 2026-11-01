"""Durable candidate notification recovery while the API is running.

This is deliberately separate from Kwork Buyer Search's own event stream.
The API can recover pending cross-platform notifications after a restart even
when no orchestrator scan has been started by the desktop UI.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from loguru import logger

from src.action.candidate_delivery import deliver_candidate_notifications
from src.action.proposal_db import ProposalDB
from src.utils.notifier import TelegramNotifier


async def recover_candidate_notifications(*, interval_seconds: float = 10.0) -> None:
    """Drain bounded batches periodically; cancellation retains pending rows.

    When no Telegram configuration exists, *do not* claim pending messages:
    they remain durable until the provider becomes available. Actual provider
    acceptance is recorded, not device receipt or reading the message.
    """
    worker_id = f"api-{os.getpid()}-{uuid4().hex}"
    interval = max(1.0, min(float(interval_seconds), 60.0))
    db = ProposalDB()
    last_cleanup = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            # The deployment may enable Telegram later via the cycle control.
            # Never emit a 'delivered' ACK merely because the provider is off.
            if os.getenv("TELEGRAM_TOKEN") and os.getenv("ADMIN_CHAT_ID"):
                notifier = TelegramNotifier(db=db)
                if notifier.bot and notifier.admin_id:
                    await deliver_candidate_notifications(
                        db, notifier.notify_candidate, worker_id=worker_id, limit=16,
                    )
            now = loop.time()
            if now - last_cleanup >= 86400 or last_cleanup == 0.0:
                # Cleanup is scoped to terminal history; active pending work
                # and dedup tombstones must survive process restarts.
                await asyncio.to_thread(db.cleanup_notification_history, retention_days=30)
                last_cleanup = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Candidate notification recovery paused: {}: {}", type(exc).__name__, exc)
        await asyncio.sleep(interval)
