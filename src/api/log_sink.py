"""Loguru sink that broadcasts log records to all WebSocket subscribers."""
from __future__ import annotations

from typing import Any


def make_ws_sink(app_state: Any):
    """Return a loguru-compatible sink function that broadcasts to WS clients."""
    def _sink(message) -> None:
        record = message.record
        level = record["level"].name
        text = message.strip()
        app_state.broadcast_log_sync(text, level)

    return _sink
