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
            queues = set(self.log_queues)
        self._safe_broadcast(queues, msg)

    def log_snapshot(self) -> list[dict[str, Any]]:
        return list(self.log_history)

    def clear_logs(self) -> None:
        self.log_history.clear()
        self._broadcast_to_queues(self.log_queues, {"type": "logs_cleared"})

    def _broadcast_to_queues(self, queues: set[asyncio.Queue], msg: dict[str, Any]) -> None:
        dead: set[asyncio.Queue] = set()
        for q in queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(q)
        with self._log_lock:
            self.log_queues -= dead
            self.status_queues -= dead

    def _safe_broadcast(self, queues: set[asyncio.Queue], msg: dict[str, Any]) -> None:
        """Thread-safe broadcast: schedule put_nowait on the event loop."""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self._broadcast_to_queues, queues, msg)
        except RuntimeError:
            self._broadcast_to_queues(queues, msg)

    def broadcast_status_sync(self, data: dict) -> None:
        msg = {"type": "status", **data}
        dead: set[asyncio.Queue] = set()
        for q in self.status_queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(q)
        self.status_queues -= dead

    def to_status_dict(self) -> dict:
        return {
            "cycle_running": self.cycle_running,
            "last_cycle_stats": self.last_cycle_stats,
            "last_error": self.last_error,
        }


app_state = AppState()
