"""Live fan-out for events already persisted by the market job repository."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from typing import Any


class MarketEventHub:
    """Deliver fresh events without making in-memory queues the source of truth."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, job_id: str, *, maxsize: int = 256) -> asyncio.Queue[dict[str, Any]]:
        if not job_id:
            raise ValueError("job_id is required")
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers[job_id].add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(job_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(job_id, None)

    def publish(self, event: Mapping[str, Any]) -> None:
        """Publish one serialized event, evicting only stale in-memory notices."""

        job_id = str(event.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("event must include job_id")
        payload = dict(event)
        for queue in tuple(self._subscribers.get(job_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A subsequent durable replay handles any event this client missed.
                continue
