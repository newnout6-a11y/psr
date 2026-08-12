from __future__ import annotations

import pytest

from src.platforms.kwork_supply.models import JobKind, MarketJobCreate, MarketScope
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.workflow import DEFAULT_WORKFLOW_REGISTRY, WorkflowDefinition, WorkflowRegistry


def test_supply_workflow_is_registered_without_changing_legacy_operation_support():
    workflow = DEFAULT_WORKFLOW_REGISTRY.require(JobKind.SUPPLY)
    assert workflow.supports("fetch_batch")
    buyer = DEFAULT_WORKFLOW_REGISTRY.require("buyer_search")
    assert buyer.supports("fetch_buyer_projects")
    assert not buyer.supports("fetch_batch")


def test_registry_rejects_duplicate_or_empty_definitions():
    registry = WorkflowRegistry()
    definition = WorkflowDefinition(JobKind.BUYER_SEARCH, frozenset({"fetch_buyer_projects"}))
    registry.register(definition)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValueError, match="at least one"):
        registry.register(WorkflowDefinition(JobKind.SUPPLY, frozenset()))


@pytest.mark.asyncio
async def test_job_kind_and_workflow_config_are_additive_and_default_to_supply(tmp_path):
    repository = MarketJobRepository(tmp_path / "market.db")
    scope = MarketScope(category_id=38, canonical_alias="website-repair")
    legacy = await repository.create_job(MarketJobCreate(scope=scope), job_id="legacy")
    buyer = await repository.create_job(
        MarketJobCreate(
            scope=scope,
            job_kind=JobKind.BUYER_SEARCH,
            workflow_config={"run_id": "buyer-run-1"},
        ),
        job_id="buyer",
    )

    assert legacy["job_kind"] == JobKind.SUPPLY.value
    assert legacy["workflow_config"] == {}
    assert buyer["job_kind"] == JobKind.BUYER_SEARCH.value
    assert buyer["workflow_config"] == {"run_id": "buyer-run-1"}
