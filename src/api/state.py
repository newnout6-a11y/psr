"""Global mutable application state shared across API routes."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Optional


class AppState:
    def __init__(self) -> None:
        self._orchestrator: Any = None
        self.cycle_running: bool = False
        self.cycle_task: Optional[asyncio.Task] = None
        self.last_cycle_stats: Optional[dict] = None
        self.last_error: Optional[str] = None
        self._log_seq: int = 0
        self._log_lock = threading.Lock()
        self.log_history: deque[dict[str, Any]] = deque(maxlen=2000)
        # Sets of per-client asyncio.Queues for broadcasting
        self.log_queues: set[asyncio.Queue] = set()
        self.status_queues: set[asyncio.Queue] = set()
        # A Loguru sink or a synchronous FastAPI route can run outside the
        # event-loop thread. asyncio.Queue must be written on its owning loop.
        self._queue_loops: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    def register_queue(self, queue: asyncio.Queue, *, status: bool = False) -> None:
        with self._log_lock:
            (self.status_queues if status else self.log_queues).add(queue)
            self._queue_loops[queue] = asyncio.get_running_loop()

    def unregister_queue(self, queue: asyncio.Queue) -> None:
        with self._log_lock:
            self.log_queues.discard(queue)
            self.status_queues.discard(queue)
            self._queue_loops.pop(queue, None)

    def get_orchestrator(self) -> Any:
        if self._orchestrator is None:
            from src.orchestrator import FreelanceOrchestrator
            from src.paths import ensure_layout

            ensure_layout()
            self._orchestrator = FreelanceOrchestrator()
        return self._orchestrator

    def reset_orchestrator(self) -> None:
        self._orchestrator = None

    def broadcast_log_sync(self, record: str, level: str = "INFO") -> None:
        with self._log_lock:
            self._log_seq += 1
            msg = {
                "type": "log",
                "seq": self._log_seq,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "message": record,
            }
            self.log_history.append(msg)
            # Schedule under the same lock as the sequence assignment: a
            # concurrent clear or second log cannot overtake this record.
            self._broadcast_to_queues(self.log_queues, msg)

    def log_snapshot(self) -> list[dict[str, Any]]:
        with self._log_lock:
            return list(self.log_history)

    def clear_logs(self) -> None:
        with self._log_lock:
            self.log_history.clear()
            self._broadcast_to_queues(self.log_queues, {"type": "logs_cleared"})

    def _broadcast_to_queues(self, queues: set[asyncio.Queue], msg: dict[str, Any]) -> None:
        for q in tuple(queues):
            loop = self._queue_loops.get(q)
            if loop is None:
                # Backward-compatible for local tests without a WS session.
                self._put_latest(q, msg)
            elif loop.is_closed():
                self.log_queues.discard(q)
                self.status_queues.discard(q)
                self._queue_loops.pop(q, None)
            else:
                try:
                    loop.call_soon_threadsafe(self._put_latest, q, msg)
                except RuntimeError:
                    # Called under _log_lock by the broadcasters.
                    self.log_queues.discard(q)
                    self.status_queues.discard(q)
                    self._queue_loops.pop(q, None)

    @staticmethod
    def _put_latest(q: asyncio.Queue, msg: dict[str, Any]) -> None:
        """Keep the stream responsive when a slow client fills its buffer."""
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except asyncio.QueueEmpty:
                pass

    def broadcast_status_sync(self, data: dict) -> None:
        msg = {"type": "status", **data}
        with self._log_lock:
            self._broadcast_to_queues(self.status_queues, msg)

    def to_status_dict(self) -> dict:
        return {
            "cycle_running": self.cycle_running,
            "last_cycle_stats": self.last_cycle_stats,
            "last_error": self.last_error,
        }


app_state = AppState()
