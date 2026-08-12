"""Workflow registry for durable market-control-plane job kinds.

The existing seller-market workflow remains the default.  Buyer Search can
register its own operation handlers without inheriting seller listing semantics.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.platforms.kwork_buyer.models import BuyerOperationKind

from .models import JobKind, OperationKind


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Declarative namespace accepted by the common job runtime."""

    job_kind: JobKind
    operation_kinds: frozenset[str]
    config_schema_version: int = 1

    def supports(self, operation_kind: str | OperationKind) -> bool:
        value = operation_kind.value if isinstance(operation_kind, OperationKind) else str(operation_kind)
        return value in self.operation_kinds


class WorkflowRegistry:
    """Small explicit registry with fail-closed lookup for job dispatch."""

    def __init__(self, definitions: Iterable[WorkflowDefinition] = ()) -> None:
        self._definitions: dict[JobKind, WorkflowDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: WorkflowDefinition) -> None:
        if definition.job_kind in self._definitions:
            raise ValueError(f"workflow already registered: {definition.job_kind.value}")
        if not definition.operation_kinds:
            raise ValueError("workflow must declare at least one operation kind")
        self._definitions[definition.job_kind] = definition

    def get(self, job_kind: JobKind | str) -> WorkflowDefinition | None:
        return self._definitions.get(JobKind(job_kind))

    def require(self, job_kind: JobKind | str) -> WorkflowDefinition:
        definition = self.get(job_kind)
        if definition is None:
            raise KeyError(f"workflow is not registered: {job_kind}")
        return definition

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            kind.value: {
                "operation_kinds": sorted(definition.operation_kinds),
                "config_schema_version": definition.config_schema_version,
            }
            for kind, definition in self._definitions.items()
        }


SUPPLY_WORKFLOW = WorkflowDefinition(
    job_kind=JobKind.SUPPLY,
    operation_kinds=frozenset(kind.value for kind in OperationKind),
)
BUYER_SEARCH_WORKFLOW = WorkflowDefinition(
    job_kind=JobKind.BUYER_SEARCH,
    operation_kinds=frozenset(kind.value for kind in BuyerOperationKind),
)
DEFAULT_WORKFLOW_REGISTRY = WorkflowRegistry((SUPPLY_WORKFLOW, BUYER_SEARCH_WORKFLOW))


__all__ = [
    "DEFAULT_WORKFLOW_REGISTRY",
    "BUYER_SEARCH_WORKFLOW",
    "SUPPLY_WORKFLOW",
    "WorkflowDefinition",
    "WorkflowRegistry",
]
