from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.outreach_preflight import BuyerAccountBoundPreflightVerifier
from src.platforms.kwork_buyer.sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_project_detail(self, remote_id: str):
        self.calls.append(("project", remote_id))
        return {"status": "active", "hasOffer": False, "connects": {"free_amount": 3}}

    async def fetch_want_detail(self, remote_id: str):
        self.calls.append(("want", remote_id))
        return {"alreadyWork": False}


class _Capabilities(BuyerReadCapabilities):
    def __init__(self, client: _Client) -> None:
        super().__init__(
            client,
            BuyerReadProvenance(
                worker_id="worker-1",
                account_registration_id="account-1",
                transport_id="vpnte-1",
                egress_ip="198.51.100.1",
                source="attachment_enrichment",
            ),
        )
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_preflight_verifier_uses_only_account_bound_read_capabilities_and_closes_them() -> None:
    client = _Client()
    capabilities = _Capabilities(client)

    async def factory(run_id: str, project_id: str, account_id: str):
        assert (run_id, project_id, account_id) == ("run-1", "project-1", "account-1")
        return capabilities

    evidence = await BuyerAccountBoundPreflightVerifier(factory)(
        "account-1",
        "run-1",
        "project-1",
        {"remote_project_id": "remote-1"},
    )

    assert client.calls == [("project", "remote-1"), ("want", "remote-1")]
    assert capabilities.closed == 1
    assert evidence == {
        "project_is_active": True,
        "has_offer": False,
        "already_work": False,
        "sender_account_eligible": True,
        "account_session_valid": True,
        "connects_sufficient": True,
        "attachment_upload_capability_verified": False,
    }
