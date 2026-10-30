"""Bounded, recoverable delivery of persisted candidate notifications.

This worker shares no SQLite connections and never holds a DB transaction
across Telegram/network I/O. Leases ensure each event has at most one active
owner; losing a process after provider acceptance and before ACK can cause a
repeat (at-least-once rather than exactly-once delivery).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


_delivery_lock = asyncio.Lock()


async def deliver_candidate_notifications(
    db: Any,
    send: Callable[[dict[str, Any]], Awaitable[bool | None]],
    *,
    worker_id: str,
    limit: int = 8,
    timeout_seconds: float = 90.0,
) -> set[int]:
    """Process at most ``limit`` due events; return ACKed candidate IDs.

    ``send`` must return False on provider rejection, True on acceptance. None
    is accepted only for legacy mock callbacks; real transports report bool.
    """
    if not hasattr(db, "claim_due_notifications"):
        return set()

    # Serialize delivery inside one FastAPI event loop even when an orchestrator
    # cycle and API recovery loop are both active. Cross-process exclusion is
    # protected separately by SQLite fenced leases.
    async with _delivery_lock:
        events = await asyncio.to_thread(
            db.claim_due_notifications,
            worker_id,
            limit=max(1, min(limit, 32)),
            lease_seconds=max(120, int(timeout_seconds) + 30),
        )
        accepted: set[int] = set()
        for event in events:
            event_id = int(event["event_id"])
            candidate_id = int(event["candidate_id"])
            lease_token = str(event["lease_token"])
            try:
                candidate = await asyncio.to_thread(db.get_candidate, candidate_id)
                if not candidate or candidate.get("status") not in {"queued", "auto_ready"}:
                    # User may have skipped/sent it while a pending event was
                    # waiting. Recording provider acceptance would be untrue.
                    if hasattr(db, "cancel_notification"):
                        await asyncio.to_thread(
                            db.cancel_notification, event_id, lease_token,
                            worker_id=worker_id, reason="candidate no longer eligible",
                        )
                    else:
                        await asyncio.to_thread(
                            db.fail_notification, event_id, lease_token,
                            "candidate no longer eligible", worker_id=worker_id,
                        )
                    continue
                result = await asyncio.wait_for(send(candidate), timeout=timeout_seconds)
                if result is False:
                    raise RuntimeError("notification provider did not accept the message")
                if await asyncio.to_thread(
                    db.ack_notification, event_id, lease_token, worker_id=worker_id,
                ):
                    accepted.add(candidate_id)
            except asyncio.CancelledError:
                # No in-memory rescheduling: expired leases are reclaimed by
                # the next worker after restart.
                raise
            except Exception as exc:
                logger.warning(
                    "Candidate notification outbox event {} failed ({}): {}",
                    event_id, type(exc).__name__, exc,
                )
                await asyncio.to_thread(
                    db.fail_notification, event_id, lease_token, str(exc), worker_id=worker_id,
                )
        return accepted
