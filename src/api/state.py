"""Global mutable application state shared across API routes."""
from __future__ import annotations

import asyncio
from typing import Any, Optional


class AppState:
    def __init__(self) -> None:
        self._orchestrator: Any = None
        self.cycle_running: bool = False
        self.cycle_task: Optional[asyncio.Task] = None
        self.last_cycle_stats: Optional[dict] = None
        self.last_error: Optional[str] = None
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
        msg = {"type": "log", "level": level, "message": record}
        dead: set[asyncio.Queue] = set()
        for q in self.log_queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(q)
        self.log_queues -= dead

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
