from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.platforms.kwork_buyer.runtime_composition import (
    AccountBoundBuyerKworkClient,
    AccountBoundBuyerTaxonomyCapabilitiesFactory,
)
from src.platforms.kwork_buyer.taxonomy_refresh import BuyerTaxonomyReadCapabilities
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext


def _context() -> MarketAccountContext:
    return MarketAccountContext(
        registration_id="account-1",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"X-Persona": "buyer-test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )


class _Supervisor:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple[str, str]] = []
        self.release_calls: list[str] = []
        self.identity = BuyerDiscoveryIdentity(
            worker_id="buyer-worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="198.51.100.10",
        )

    async def reserve_attachment_identity(self, run_id: str, account_registration_id: str) -> object:
        self.reserve_calls.append((run_id, account_registration_id))
        return SimpleNamespace(
            run_id=run_id,
            worker_id=self.identity.worker_id,
            identity=self.identity,
            token="taxonomy-reservation-1",
        )

    async def release_attachment_identity(self, reservation: object) -> None:
        self.release_calls.append(str(reservation.token))  # type: ignore[attr-defined]


class _IdentityPool:
    def __init__(self) -> None:
        self.context_calls: list[str] = []

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        self.context_calls.append(worker_id)
        return _context()


class _CatalogClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.close_calls = 0
        self.send_calls = 0

    async def fetch_catalog_rubrics(self) -> dict[str, object]:
        self.calls.append(("rubrics", {}))
        return {"response": []}

    async def fetch_catalog_categories(self, *, rubric_id: int) -> dict[str, object]:
        self.calls.append(("categories", {"rubric_id": rubric_id}))
        return {"response": []}

    async def fetch_category_attributes(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(("attributes", {"category_id": category_id}))
        return {"response": []}

    async def fetch_catalog_filters(self, *, category_id: int) -> dict[str, object]:
        self.calls.append(("filters", {"category_id": category_id}))
        return {"response": {}}

    async def fetch_catalog_main(self) -> dict[str, object]:
        self.calls.append(("catalog_main", {}))
        return {"response": {}}

    async def send_message(self) -> None:
        self.send_calls += 1
        raise AssertionError("taxonomy capability must not expose send")

    async def close(self) -> None:
        self.close_calls += 1


class _ClientFactory:
    def __init__(self, client: _CatalogClient) -> None:
        self.client = client
        self.calls: list[tuple[MarketAccountContext, BuyerDiscoveryIdentity]] = []

    async def __call__(self, context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> _CatalogClient:
        self.calls.append((context, identity))
        return self.client


@pytest.mark.asyncio
async def test_taxonomy_capability_borrows_the_requested_run_account_lease_and_exposes_only_catalog_reads() -> None:
    supervisor = _Supervisor()
    identity_pool = _IdentityPool()
    client = _CatalogClient()
    client_factory = _ClientFactory(client)
    factory = AccountBoundBuyerTaxonomyCapabilitiesFactory(
        supervisor,
        identity_pool,  # type: ignore[arg-type]
        client_factory,  # type: ignore[arg-type]
    )

    capabilities = await factory("run-1", "account-1")

    assert isinstance(capabilities, BuyerTaxonomyReadCapabilities)
    assert capabilities.provenance.as_dict() == {
        "worker_id": "buyer-worker-1",
        "account_registration_id": "account-1",
        "transport_id": "vpnte-slot-1",
        "egress_ip": "198.51.100.10",
        "source": "taxonomy_refresh",
    }
    assert not hasattr(capabilities, "send_message")

    await capabilities.fetch_rubrics()
    await capabilities.fetch_categories(rubric_id=11)
    await capabilities.fetch_attributes(category_id=38)
    await capabilities.fetch_filters(category_id=38)
    await capabilities.fetch_catalog_main()
    await capabilities.close()
    await capabilities.close()

    assert supervisor.reserve_calls == [("run-1", "account-1")]
    assert supervisor.release_calls == ["taxonomy-reservation-1"]
    assert identity_pool.context_calls == ["buyer-worker-1"]
    assert [identity.account_registration_id for _, identity in client_factory.calls] == ["account-1"]
    assert client.calls == [
        ("rubrics", {}),
        ("categories", {"rubric_id": 11}),
        ("attributes", {"category_id": 38}),
        ("filters", {"category_id": 38}),
        ("catalog_main", {}),
    ]
    assert client.close_calls == 1
    assert client.send_calls == 0


class _MobileCatalogClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request(self, endpoint: str, **params: object) -> dict[str, object]:
        self.calls.append((endpoint, params))
        return {"response": []}


@pytest.mark.asyncio
async def test_account_bound_client_uses_only_fixed_taxonomy_read_endpoints_with_required_parameter_casing() -> None:
    client = object.__new__(AccountBoundBuyerKworkClient)
    client._mobile_client = _MobileCatalogClient()

    await client.fetch_catalog_rubrics()
    await client.fetch_catalog_categories(rubric_id=11)
    await client.fetch_category_attributes(category_id=38)
    await client.fetch_catalog_filters(category_id=38)
    await client.fetch_catalog_main()

    assert client._mobile_client.calls == [  # type: ignore[attr-defined]
        ("catalogRubrics", {"use_token": True}),
        ("catalogCategories", {"rubricId": 11, "use_token": True}),
        ("categoryAttributes", {"category_id": 38, "use_token": True}),
        ("catalogFilters", {"categoryId": 38, "use_token": True}),
        ("catalogMainv2", {"use_token": True}),
    ]
