from __future__ import annotations

import pytest

from src.platforms.kwork_buyer import (
    BuyerQuery,
    BuyerQueryCandidate,
    BuyerQueryCollisionKind,
    BuyerQueryOrigin,
    BuyerQueryPlanRequest,
    BuyerQueryPlanner,
    BuyerQueryState,
    BuyerRunMode,
    InsufficientBuyerQueriesError,
)


def candidate(text: str, **kwargs: object) -> BuyerQueryCandidate:
    return BuyerQueryCandidate(text=text, **kwargs)


def request(
    *,
    mode: BuyerRunMode = BuyerRunMode.MANUAL,
    worker_count: int = 2,
    queries_per_worker: int = 2,
    candidates: tuple[BuyerQueryCandidate, ...],
) -> BuyerQueryPlanRequest:
    return BuyerQueryPlanRequest(
        run_id="run-1",
        mode=mode,
        worker_count=worker_count,
        queries_per_worker=queries_per_worker,
        category_id=11,
        category_path=(1, 11),
        filters={"price_to": 20_000},
        candidates=candidates,
    )


def test_manual_plan_deduplicates_exact_collisions_and_exposes_durable_rows() -> None:
    plan = BuyerQueryPlanner().plan(
        request(
            candidates=(
                candidate("Telegram Bot", rationale="Manual seed"),
                candidate(" telegram\u00a0bot ", rationale="Duplicate spelling"),
                candidate("Telegram CRM", rationale="Manual seed"),
                candidate("Telegram Mini App", rationale="Manual seed"),
                candidate("Telegram payment bot", rationale="Manual seed"),
            )
        )
    )

    assert [query.normalized_text for query in plan.queries] == [
        "telegram bot",
        "telegram crm",
        "telegram mini app",
        "telegram payment bot",
    ]
    assert all(query.origin is BuyerQueryOrigin.MANUAL for query in plan.queries)
    assert all(query.approved and query.state is BuyerQueryState.APPROVED for query in plan.queries)
    assert [(item.kind, item.first_candidate_index, item.second_candidate_index) for item in plan.collisions] == [
        (BuyerQueryCollisionKind.EXACT, 0, 1)
    ]
    assert plan.query_payloads()[0]["filter_json"] == '{"price_to":20000}'
    assert plan.assignment_payloads()[0]["worker_index"] == 1
    assert plan.to_durable_payload()["minimum_query_count"] == 4


@pytest.mark.parametrize(
    ("mode", "expected_origin"),
    [
        (BuyerRunMode.BRIEF, BuyerQueryOrigin.BRIEF),
        (BuyerRunMode.CATEGORY, BuyerQueryOrigin.CATEGORY),
        (BuyerRunMode.MANUAL, BuyerQueryOrigin.MANUAL),
        (BuyerRunMode.HYBRID, BuyerQueryOrigin.HYBRID_SEED),
    ],
)
def test_modes_assign_auditable_default_origins(mode: BuyerRunMode, expected_origin: BuyerQueryOrigin) -> None:
    plan = BuyerQueryPlanner().plan(
        request(mode=mode, worker_count=1, queries_per_worker=1, candidates=(candidate("Telegram bot"),))
    )

    assert plan.queries[0].origin is expected_origin
    assert plan.queries[0].rationale


def test_hybrid_expansion_keeps_explicit_ai_origin_and_parent_reference() -> None:
    plan = BuyerQueryPlanner().plan(
        request(
            mode=BuyerRunMode.HYBRID,
            worker_count=1,
            queries_per_worker=1,
            candidates=(candidate("Telegram CRM", parent_query_id="seed-query"),),
        )
    )

    assert plan.queries[0].origin is BuyerQueryOrigin.AI_EXPANSION
    assert plan.queries[0].parent_query_id == "seed-query"


def test_plan_requires_workers_times_queries_per_worker_after_deduplication() -> None:
    with pytest.raises(InsufficientBuyerQueriesError) as exc_info:
        BuyerQueryPlanner().plan(
            request(
                candidates=(
                    candidate("Telegram bot"),
                    candidate("telegram bot"),
                    candidate("Telegram CRM"),
                )
            )
        )

    assert exc_info.value.required == 4
    assert exc_info.value.available == 2


def test_semantic_scorer_reports_collisions_without_removing_distinct_queries() -> None:
    def scorer(first: BuyerQuery, second: BuyerQuery) -> float:
        return 0.95 if {first.normalized_text, second.normalized_text} == {
            "telegram bot",
            "telegram automation",
        } else 0.0

    plan = BuyerQueryPlanner(semantic_scorer=scorer, semantic_threshold=0.9).plan(
        request(
            worker_count=1,
            queries_per_worker=2,
            candidates=(candidate("Telegram bot"), candidate("Telegram automation")),
        )
    )

    assert len(plan.queries) == 2
    assert [(collision.kind, collision.score) for collision in plan.collisions] == [
        (BuyerQueryCollisionKind.SEMANTIC, 0.95)
    ]


def test_round_robin_assignment_gives_each_worker_the_requested_initial_batch() -> None:
    plan = BuyerQueryPlanner().plan(
        request(
            worker_count=3,
            queries_per_worker=2,
            candidates=tuple(candidate(f"query {index}") for index in range(6)),
        )
    )

    assert [assignment.worker_index for assignment in plan.assignments] == [1, 2, 3, 1, 2, 3]
    assert [assignment.assignment_order for assignment in plan.assignments] == [1, 1, 1, 2, 2, 2]


def test_query_identifiers_and_distribution_are_stable_for_the_same_input() -> None:
    planner = BuyerQueryPlanner()
    first = planner.plan(request(candidates=tuple(candidate(f"query {index}") for index in range(4))))
    second = planner.plan(request(candidates=tuple(candidate(f"query {index}") for index in range(4))))

    assert [query.query_id for query in first.queries] == [query.query_id for query in second.queries]
    assert first.assignment_payloads() == second.assignment_payloads()
