import asyncio

from src.api.state import AppState


def test_log_history_keeps_records_without_websocket_clients():
    state = AppState()

    state.broadcast_log_sync("first", "INFO")
    state.broadcast_log_sync("second", "ERROR")

    snapshot = state.log_snapshot()

    assert [item["message"] for item in snapshot] == ["first", "second"]
    assert [item["level"] for item in snapshot] == ["INFO", "ERROR"]
    assert snapshot[0]["seq"] < snapshot[1]["seq"]


def test_clear_logs_clears_history_and_notifies_clients():
    state = AppState()
    queue: asyncio.Queue = asyncio.Queue()
    state.log_queues.add(queue)
    state.broadcast_log_sync("first", "INFO")

    state.clear_logs()

    assert state.log_snapshot() == []
    assert queue.get_nowait()["type"] == "log"
    assert queue.get_nowait()["type"] == "logs_cleared"
