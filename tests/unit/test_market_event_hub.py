from __future__ import annotations

from src.platforms.kwork_supply.events import MarketEventHub


def test_event_hub_fans_out_and_unsubscribes():
    hub = MarketEventHub()
    first = hub.subscribe("job_01")
    second = hub.subscribe("job_01")

    hub.publish({"job_id": "job_01", "seq": 1, "type": "job.snapshot"})

    assert first.get_nowait()["seq"] == 1
    assert second.get_nowait()["type"] == "job.snapshot"
    hub.unsubscribe("job_01", second)
    hub.publish({"job_id": "job_01", "seq": 2, "type": "job.metrics"})
    assert first.get_nowait()["seq"] == 2
    assert second.empty()


def test_event_hub_keeps_a_bounded_live_buffer():
    hub = MarketEventHub()
    queue = hub.subscribe("job_01", maxsize=1)

    hub.publish({"job_id": "job_01", "seq": 1})
    hub.publish({"job_id": "job_01", "seq": 2})

    assert queue.get_nowait()["seq"] == 2
