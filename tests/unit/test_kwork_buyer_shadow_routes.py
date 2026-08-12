from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.kwork_buyer_shadow import router
from src.platforms.kwork_buyer.shadow_persistence import SQLiteBuyerShadowStore


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _persistent_client(tmp_path) -> TestClient:
    app = FastAPI()
    app.state.buyer_shadow = SQLiteBuyerShadowStore(tmp_path / "buyer-shadow.sqlite3")
    app.include_router(router)
    return TestClient(app)


def _project(remote_project_id: int) -> dict[str, object]:
    return {
        "platform": "kwork",
        "remote_project_id": remote_project_id,
        "title": "Landing page",
        "description": "Need a responsive page",
        "budget_min": 1_000,
        "budget_max": 2_000,
        "offers": 1,
        "views": 4,
        "canonical_url": f"https://kwork.ru/projects/{remote_project_id}",
    }


def test_compare_exposes_parity_and_endpoint_drift_without_app_state() -> None:
    with _client() as client:
        response = client.post(
            "/api/kwork/buyer-search/shadow/compare",
            json={
                "legacy_projects": [_project(101)],
                "new_projects": [_project(101)],
                "fields": ["title"],
                "legacy_endpoints": [{"endpoint": "/projects?page=1", "status_code": 200, "result_count": 1}],
                "new_endpoints": [{"endpoint": "/projects?page=7", "status_code": 200, "result_count": 1}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["matched_project_ids"] == ["kwork:101"]
    assert payload["field_parity"][0]["field"] == "title"
    assert payload["endpoint_drifts"][0]["endpoint"] == "/projects"
    assert payload["endpoint_drifts"][0]["is_critical"] is False


def test_endpoint_drift_maps_invalid_captured_sample_to_422() -> None:
    with _client() as client:
        response = client.post(
            "/api/kwork/buyer-search/shadow/endpoint-drift",
            json={
                "legacy_endpoints": [{"endpoint": "/projects", "status_code": 200}],
                "new_endpoints": [{"endpoint": "/projects", "status_code": 999}],
            },
        )

    assert response.status_code == 422
    assert "status_code" in response.json()["detail"]


def test_rollout_gate_derives_accepted_comparison_and_allows_a_completed_first_canary() -> None:
    comparison = {"legacy_projects": [_project(101)], "new_projects": [_project(101)]}
    evidence = [
        {
            "worker_id": "worker-1",
            "account_registration_id": "account-1",
            "transport_id": "vpnte-1",
            "egress_ip": "203.0.113.11",
            "healthy": True,
            "verified_egress": True,
        },
        {
            "worker_id": "worker-2",
            "account_registration_id": "account-2",
            "transport_id": "vpnte-2",
            "egress_ip": "203.0.113.12",
            "healthy": True,
            "verified_egress": True,
        },
    ]
    with _client() as client:
        response = client.post(
            "/api/kwork/buyer-search/shadow/rollout-gate",
            json={
                "target_workers": 2,
                "evidence": evidence,
                "config": {"enabled": True, "live_discovery": True, "max_workers": 2},
                "comparison": comparison,
                "operator_accepted": True,
                "rollback_documented": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["acceptance"]["accepted"] is True
    assert payload["gate"]["allowed"] is True


def test_rollout_gate_is_fail_closed_without_a_comparison() -> None:
    with _client() as client:
        response = client.post(
            "/api/kwork/buyer-search/shadow/rollout-gate",
            json={
                "target_workers": 2,
                "config": {"enabled": True, "live_discovery": True, "max_workers": 2},
            },
        )

    assert response.status_code == 200
    assert response.json()["gate"]["allowed"] is False
    assert "accepted shadow comparison is required" in response.json()["gate"]["blockers"]


def test_rollout_gate_persists_accepted_evidence_for_a_run(tmp_path) -> None:
    evidence = [
        {
            "worker_id": f"worker-{index}",
            "account_registration_id": f"account-{index}",
            "transport_id": f"vpnte-{index}",
            "egress_ip": f"203.0.113.{index}",
            "healthy": True,
            "verified_egress": True,
        }
        for index in (11, 12)
    ]
    with _persistent_client(tmp_path) as client:
        response = client.post(
            "/api/kwork/buyer-search/shadow/rollout-gate",
            json={
                "run_id": "run-1",
                "target_workers": 2,
                "evidence": evidence,
                "config": {"enabled": True, "live_discovery": True, "max_workers": 2},
                "comparison": {"legacy_projects": [_project(101)], "new_projects": [_project(101)]},
                "operator_accepted": True,
                "rollback_documented": True,
            },
        )
        restored = client.get("/api/kwork/buyer-search/shadow/runs/run-1/latest-approved-gate/2")

    assert response.status_code == 200
    assert response.json()["persisted_gate"]["allowed"] is True
    assert restored.status_code == 200
    assert restored.json()["gate"]["allowed"] is True
