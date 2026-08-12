from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.mapper import (
    MOBILE_PROJECT_SOURCE,
    WEB_PROJECT_SOURCE,
    BuyerProjectMappingError,
    BuyerProjectObservationContext,
    merge_canonical_projects,
)
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadProvenance
from src.platforms.kwork_buyer.sources.mobile_projects import normalize_mobile_project, normalize_mobile_projects
from src.platforms.kwork_buyer.sources.web_projects import normalize_web_project, normalize_web_projects


def context(source: str, *, position: int = 0) -> BuyerProjectObservationContext:
    return BuyerProjectObservationContext(
        run_id="run-1",
        query_id="query-1",
        task_id="task-1",
        attempt_id="attempt-1",
        observed_at="2026-07-16T12:00:00Z",
        page=1,
        response_position=position,
        provenance=BuyerReadProvenance(
            worker_id="worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="203.0.113.10",
            source=source,
        ),
        raw_artifact_id="artifact-1",
        route_generation=2,
    )


def test_mobile_normalizer_maps_canonical_project_and_required_observation_provenance() -> None:
    mapped = normalize_mobile_project(
        {
            "id": 101,
            "title": "&lt;b&gt;Telegram bot&lt;/b&gt;",
            "description": "  CRM\u00a0integration ",
            "status": "active",
            "category_id": 11,
            "parent_category_id": 1,
            "price": 2_000,
            "possible_price_limit": 3_000,
            "offers": 2,
            "user_id": 77,
            "username": "buyer",
            "user_hired_percent": 45,
            "user_projects_count": 12,
            "user_active_projects_count": 3,
        },
        context=context(MOBILE_PROJECT_SOURCE),
    )

    assert mapped.canonical.remote_project_id == "101"
    assert mapped.canonical.latest_title == "Telegram bot"
    assert mapped.canonical.canonical_url == "https://kwork.ru/projects/101"
    assert mapped.observation.budget_min == 2_000.0
    assert mapped.observation.budget_max == 3_000.0
    assert mapped.observation.to_payload()["account_registration_id"] == "account-1"
    assert len(mapped.canonical.canonical_hash) == 64
    assert len(mapped.observation.normalized_hash) == 64


def test_web_normalizer_maps_web_fields_and_drops_volatile_url_query() -> None:
    mapped = normalize_web_project(
        {
            "wantId": "101",
            "title": "Web project",
            "description": "Full web description",
            "priceLimit": 4_000,
            "possiblePriceLimit": 5_000,
            "kwork_count": 3,
            "views_dirty": 9,
            "categoryId": 11,
            "parentCategoryId": 1,
            "url": "https://KWORK.ru/projects/101/?token=secret#ignored",
            "user": {"id": 77, "username": "buyer"},
        },
        context=context(WEB_PROJECT_SOURCE),
    )

    assert mapped.canonical.canonical_url == "https://kwork.ru/projects/101"
    assert mapped.observation.offers == 3
    assert mapped.observation.views == 9
    assert mapped.observation.category_id == 11
    assert "secret" not in mapped.canonical.canonical_url


def test_merge_uses_alternative_ids_or_urls_to_unify_mobile_and_web_records() -> None:
    mobile = normalize_mobile_project(
        {"id": "101", "title": "Mobile title", "description": "short"},
        context=context(MOBILE_PROJECT_SOURCE),
    )
    web = normalize_web_project(
        {
            "projectId": "web-legacy-101",
            "title": "Web title",
            "description": "full",
            "url": "https://kwork.ru/projects/101",
        },
        context=context(WEB_PROJECT_SOURCE),
    )

    merged = merge_canonical_projects(mobile.canonical, web.canonical)

    assert merged.remote_project_id == "101"
    assert "web-legacy-101" in merged.alternative_remote_ids
    assert merged.latest_title == "Web title"
    assert len(merged.canonical_hash) == 64


def test_source_provenance_is_required_and_must_match_the_mapper_source() -> None:
    with pytest.raises(BuyerProjectMappingError, match="match provenance"):
        normalize_mobile_project(
            {"id": "101", "title": "Project"},
            context=context(WEB_PROJECT_SOURCE),
        )


def test_batch_mobile_normalizer_preserves_response_positions() -> None:
    mapped = normalize_mobile_projects(
        {"response": [{"id": "101", "title": "First"}, {"id": "102", "title": "Second"}]},
        context=context(MOBILE_PROJECT_SOURCE, position=99),
    )

    assert [item.observation.context.response_position for item in mapped] == [0, 1]
    assert [item.canonical.remote_project_id for item in mapped] == ["101", "102"]


def test_batch_mobile_normalizer_skips_empty_cards_and_preserves_valid_position() -> None:
    mapped = normalize_mobile_projects(
        {"response": [{"placeholder": True}, {"id": "102", "title": "Valid"}]},
        context=context(MOBILE_PROJECT_SOURCE),
    )

    assert [item.canonical.remote_project_id for item in mapped] == ["102"]
    assert [item.observation.context.response_position for item in mapped] == [1]


def test_batch_web_normalizer_skips_empty_cards_and_preserves_valid_position() -> None:
    mapped = normalize_web_projects(
        {"projects": [{"placeholder": True}, {"wantId": "102", "title": "Valid"}]},
        context=context(WEB_PROJECT_SOURCE),
    )

    assert [item.canonical.remote_project_id for item in mapped] == ["102"]
    assert [item.observation.context.response_position for item in mapped] == [1]
