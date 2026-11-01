import asyncio
import threading

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


def test_threaded_log_and_status_delivery_stays_on_websocket_loop():
    async def exercise():
        state = AppState()
        loop_thread = threading.get_ident()
        delivery_threads = []

        class TrackedQueue(asyncio.Queue):
            def put_nowait(self, item):
                delivery_threads.append(threading.get_ident())
                return super().put_nowait(item)

        logs = TrackedQueue()
        status = TrackedQueue()
        state.register_queue(logs)
        state.register_queue(status, status=True)

        def publish():
            state.broadcast_log_sync("from background thread")
            state.broadcast_status_sync({"cycle_running": True})
            state.clear_logs()

        await asyncio.to_thread(publish)
        await asyncio.sleep(0)
        assert [logs.get_nowait()["type"] for _ in range(2)] == ["log", "logs_cleared"]
        assert status.get_nowait()["cycle_running"] is True
        assert delivery_threads == [loop_thread] * 3
        state.unregister_queue(logs)
        state.unregister_queue(status)

    asyncio.run(exercise())


def test_full_log_queue_retains_recent_events():
    state = AppState()
    queue = asyncio.Queue(maxsize=1)
    state.log_queues.add(queue)
    state.broadcast_log_sync("old")
    state.broadcast_log_sync("new")
    assert queue.get_nowait()["message"] == "new"
