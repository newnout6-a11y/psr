from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.shadow_persistence import SQLiteBuyerShadowStore


@pytest.mark.asyncio
async def test_shadow_reports_and_approved_gates_are_durable_per_run(tmp_path) -> None:
    store = SQLiteBuyerShadowStore(tmp_path / "buyer-shadow.sqlite3")
    report = await store.save_report(
        {"shared_project_count": 1, "field_parity": []},
        run_id="run-1",
        acceptance={"accepted": True},
        operator_accepted=True,
        rollback_documented=True,
    )
    gate = await store.save_gate(
        {"target_workers": 2, "allowed": True, "blockers": []},
        run_id="run-1",
        evidence=[{"worker_id": "worker-1", "egress_ip": "198.51.100.1"}],
        report_id=report["report_id"],
    )

    restored = await store.latest_approved_gate("run-1", target_workers=2)

    assert restored is not None
    assert restored["gate_id"] == gate["gate_id"]
    assert restored["report_id"] == report["report_id"]
    assert await store.latest_approved_gate("run-1", target_workers=5) is None
