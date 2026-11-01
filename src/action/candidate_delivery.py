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


NOTIFIABLE_STATUSES = frozenset({"queued", "auto_ready"})

_delivery_lock = asyncio.Lock()


class CandidateNoLongerEligible(Exception):
    """Raised by ``send`` when the candidate left a notifiable status mid-send.

    Senders may await slow work (e.g. a project preview) before the provider
    call; they re-check the status just before it and raise this so the event
    is cancelled instead of retried or acknowledged.
    """


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

    async def cancel(event_id: int, lease_token: str) -> None:
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

    # A lease covers exactly one send. Claiming a whole batch up front would
    # let slow earlier sends exhaust later leases, so another process could
    # reclaim and resend those events while this worker still holds them.
    lease_seconds = max(120, int(timeout_seconds) + 30)

    # Serialize delivery inside one FastAPI event loop even when an orchestrator
    # cycle and API recovery loop are both active. Cross-process exclusion is
    # protected separately by SQLite fenced leases.
    async with _delivery_lock:
        accepted: set[int] = set()
        for _ in range(max(1, min(limit, 32))):
            events = await asyncio.to_thread(
                db.claim_due_notifications, worker_id, limit=1, lease_seconds=lease_seconds,
            )
            if not events:
                break
            event = events[0]
            event_id = int(event["event_id"])
            candidate_id = int(event["candidate_id"])
            lease_token = str(event["lease_token"])
            try:
                candidate = await asyncio.to_thread(db.get_candidate, candidate_id)
                if not candidate or candidate.get("status") not in NOTIFIABLE_STATUSES:
                    # User may have skipped/sent it while a pending event was
                    # waiting. Recording provider acceptance would be untrue.
                    await cancel(event_id, lease_token)
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
            except CandidateNoLongerEligible:
                await cancel(event_id, lease_token)
            except Exception as exc:
                logger.warning(
                    "Candidate notification outbox event {} failed ({}): {}",
                    event_id, type(exc).__name__, exc,
                )
                await asyncio.to_thread(
                    db.fail_notification, event_id, lease_token, str(exc), worker_id=worker_id,
                )
        return accepted
