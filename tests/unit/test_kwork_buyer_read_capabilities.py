from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.sources.capabilities import (
    BuyerMutationForbiddenError,
    BuyerReadCapabilities,
    BuyerReadProvenance,
)


class FakeBuyerClient:
    async def fetch_projects(self, *, query: str) -> dict[str, str]:
        return {"query": query}

    def fetch_want_detail(self, want_id: str) -> dict[str, str]:
        return {"want_id": want_id}

    async def send_offer(self, _: str) -> None:
        raise AssertionError("a discovery client must never reach this method")


@pytest.fixture
def capabilities() -> BuyerReadCapabilities:
    return BuyerReadCapabilities(
        FakeBuyerClient(),
        BuyerReadProvenance(
            worker_id="worker-01",
            account_registration_id="account-01",
            transport_id="vpnte-slot-01",
            egress_ip="203.0.113.10",
            source="mobile_projects",
        ),
    )


@pytest.mark.asyncio
async def test_read_capabilities_preserve_worker_scoped_provenance(capabilities: BuyerReadCapabilities):
    assert await capabilities.fetch_projects(query="python") == {"query": "python"}
    assert await capabilities.fetch_want_detail("want-1") == {"want_id": "want-1"}
    assert capabilities.provenance.as_dict()["account_registration_id"] == "account-01"


def test_read_capabilities_do_not_expose_mutation_methods(capabilities: BuyerReadCapabilities):
    with pytest.raises(BuyerMutationForbiddenError):
        capabilities.send_offer("offer")


@pytest.mark.asyncio
async def test_read_capabilities_fail_closed_when_source_is_unavailable(capabilities: BuyerReadCapabilities):
    with pytest.raises(RuntimeError, match="project_detail"):
        await capabilities.fetch_project_detail("project-1")
