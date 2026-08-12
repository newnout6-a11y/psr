from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.shadow import (
    BuyerShadowRolloutConfig,
    BuyerWorkerIdentityEvidence,
    canonical_buyer_project_id,
    compare_buyer_endpoint_drift,
    compare_buyer_shadow,
    evaluate_buyer_shadow_rollout_gate,
    summarize_buyer_shadow_acceptance,
)


def project(
    project_id: int | str,
    *,
    new: bool = False,
    title: str = "CRM integration",
    description: str = "Build a CRM integration",
    budget_min: int = 1_000,
    budget_max: int = 2_000,
    offers: int = 2,
    views: int = 10,
    url: str | None = None,
) -> dict[str, object]:
    if new:
        return {
            "canonical": {
                "platform": "kwork",
                "remote_project_id": str(project_id),
                "latest_title": title,
                "latest_description": description,
                "canonical_url": url or f"https://kwork.ru/projects/{project_id}",
            },
            "observation": {
                "budget_min": budget_min,
                "budget_max": budget_max,
                "offers": offers,
                "views": views,
            },
        }
    return {
        "platform": "Kwork",
        "id": project_id,
        "title": title,
        "description": description,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "offers": offers,
        "views": views,
        "url": url or f"https://kwork.ru/projects/{project_id}",
    }


def accepted_shadow_summary() -> object:
    comparison = compare_buyer_shadow(
        [project(101)],
        [project(101, new=True)],
        legacy_endpoints=[{"endpoint": "/projects", "status": 200, "result_count": 1}],
        new_endpoints=[{"endpoint": "/projects", "status": 200, "result_count": 1}],
    )
    summary = summarize_buyer_shadow_acceptance(
        comparison,
        operator_accepted=True,
        rollback_documented=True,
    )
    assert summary.accepted
    return summary


def evidence(count: int, *, duplicate_account: bool = False) -> list[BuyerWorkerIdentityEvidence]:
    return [
        BuyerWorkerIdentityEvidence(
            worker_id=f"worker-{index}",
            account_registration_id="account-0" if duplicate_account else f"account-{index}",
            transport_id=f"slot-{index}",
            egress_ip=f"203.0.113.{index + 1}",
        )
        for index in range(count)
    ]


def test_canonical_project_id_prefers_remote_project_identity_and_nested_canonical_shape() -> None:
    assert canonical_buyer_project_id({"platform": "KWORK", "id": 101}) == "kwork:101"
    assert canonical_buyer_project_id({"project_id": "kwork:101"}) == "kwork:101"
    assert canonical_buyer_project_id(project(101, new=True)) == "kwork:101"


def test_shadow_comparison_reports_canonical_data_loss_missing_fields_and_mismatches() -> None:
    legacy = [project(101), project(102)]
    new = [project(101, new=True, title="CRM Integration", description="Changed description", views=10), project(103, new=True)]
    del new[0]["observation"]["views"]  # type: ignore[index]

    comparison = compare_buyer_shadow(legacy, new, fields=("title", "description", "views"))

    assert comparison.matched_project_ids == ("kwork:101",)
    assert comparison.legacy_only_project_ids == ("kwork:102",)
    assert comparison.new_only_project_ids == ("kwork:103",)
    assert comparison.parity_for("title").mismatches[0].project_id == "kwork:101"
    assert comparison.parity_for("description").mismatches[0].new_value == "Changed description"
    assert comparison.parity_for("views").missing_in_new_project_ids == ("kwork:101",)
    assert comparison.has_silent_data_loss


def test_shadow_comparison_is_order_independent_and_surfaces_duplicate_canonical_ids() -> None:
    legacy = [project(101, title="Z title"), project(101, title="A title")]
    new = [project(101, new=True, title="A title")]

    forward = compare_buyer_shadow(legacy, new, fields=("title",))
    reverse = compare_buyer_shadow(list(reversed(legacy)), new, fields=("title",))

    assert forward.as_dict() == reverse.as_dict()
    assert forward.duplicate_projects[0].side == "legacy"
    assert forward.parity_for("title").matched_project_ids == ("kwork:101",)


def test_endpoint_drift_keeps_call_volume_separate_from_status_regressions() -> None:
    equal_status_different_volume = compare_buyer_endpoint_drift(
        [{"endpoint": "/projects?legacy=true", "status": 200, "result_count": 1}],
        [
            {"endpoint": "/projects", "status": 200, "result_count": 1},
            {"endpoint": "/projects", "status": 200, "result_count": 1},
        ],
    )[0]
    regression = compare_buyer_endpoint_drift(
        [{"endpoint": "legacy-projects", "status": 200, "result_count": 2}],
        [{"endpoint": "new-projects", "status": 429, "result_count": 0, "error": "rate_limited"}],
        endpoint_aliases={"legacy-projects": "projects", "new-projects": "projects"},
    )[0]

    assert equal_status_different_volume.call_count_mismatch
    assert not equal_status_different_volume.status_code_mismatch
    assert not equal_status_different_volume.is_critical
    assert regression.status_code_mismatch
    assert regression.is_critical


def test_acceptance_requires_operator_rollback_required_field_parity_and_no_legacy_loss() -> None:
    clean = compare_buyer_shadow([project(101)], [project(101, new=True)])
    missing_project = compare_buyer_shadow([project(101), project(102)], [project(101, new=True)])

    declined = summarize_buyer_shadow_acceptance(clean, operator_accepted=False, rollback_documented=False)
    accepted = summarize_buyer_shadow_acceptance(clean, operator_accepted=True, rollback_documented=True)
    loss = summarize_buyer_shadow_acceptance(missing_project, operator_accepted=True, rollback_documented=True)

    assert not declined.accepted
    assert "operator acceptance is required" in declined.blockers
    assert accepted.accepted
    assert not loss.accepted
    assert any("missing 1 legacy canonical project" in blocker for blocker in loss.blockers)


@pytest.mark.parametrize(
    ("target", "completed"),
    [
        (2, ()),
        (5, (2,)),
        (10, (2, 5)),
        (20, (2, 5, 10)),
        (30, (2, 5, 10, 20)),
    ],
)
def test_every_canary_target_requires_unique_account_transport_and_egress_evidence(
    target: int,
    completed: tuple[int, ...],
) -> None:
    config = BuyerShadowRolloutConfig(
        enabled=True,
        live_discovery=True,
        max_workers=30,
        completed_canary_targets=completed,
    )
    acceptance = accepted_shadow_summary()

    denied = evaluate_buyer_shadow_rollout_gate(target, evidence(target - 1), config=config, acceptance=acceptance)
    allowed = evaluate_buyer_shadow_rollout_gate(target, evidence(target), config=config, acceptance=acceptance)

    assert not denied.allowed
    assert any("unique account" in blocker for blocker in denied.blockers)
    assert any("unique transport" in blocker for blocker in denied.blockers)
    assert any("unique verified egress IP" in blocker for blocker in denied.blockers)
    assert allowed.allowed
    assert len(allowed.account_registration_ids) == target
    assert len(allowed.transport_ids) == target
    assert len(allowed.egress_ips) == target


def test_rollout_gate_rejects_collisions_and_skipped_canary_even_with_enough_records() -> None:
    acceptance = accepted_shadow_summary()
    collision_config = BuyerShadowRolloutConfig(
        enabled=True,
        live_discovery=True,
        max_workers=5,
        completed_canary_targets=(2,),
    )
    skipped_config = BuyerShadowRolloutConfig(enabled=True, live_discovery=True, max_workers=5)

    collision = evaluate_buyer_shadow_rollout_gate(
        5,
        evidence(5, duplicate_account=True),
        config=collision_config,
        acceptance=acceptance,
    )
    skipped = evaluate_buyer_shadow_rollout_gate(5, evidence(5), config=skipped_config, acceptance=acceptance)

    assert not collision.allowed
    assert any("identity collision" in blocker for blocker in collision.blockers)
    assert not skipped.allowed
    assert "canary 2 must be completed before 5" in skipped.blockers
