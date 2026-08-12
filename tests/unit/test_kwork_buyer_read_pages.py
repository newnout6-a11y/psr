from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance
from src.platforms.kwork_buyer.sources.read_pages import (
    BuyerMobileProjectsPageSource,
    BuyerReadPageSourceError,
    BuyerWebProjectsPageSource,
)


class _ReadClient:
    def __init__(self) -> None:
        self.mobile_params: dict[str, object] | None = None
        self.web_params: dict[str, object] | None = None

    async def fetch_buyer_projects(self, **params: object) -> dict[str, object]:
        self.mobile_params = params
        return {"response": [{"id": "1", "title": "Project"}], "paging": {"total": 1}}

    async def fetch_web_projects(self, **params: object) -> list[dict[str, object]]:
        self.web_params = params
        return [{"id": "2", "title": "Web project"}]


@pytest.fixture
def provenance() -> BuyerReadProvenance:
    return BuyerReadProvenance(
        worker_id="worker-1",
        account_registration_id="account-1",
        transport_id="slot-1",
        egress_ip="198.51.100.10",
        source="mobile_projects",
    )


def _identity() -> dict[str, object]:
    return {
        "worker_id": "worker-1",
        "account_registration_id": "account-1",
        "transport_id": "slot-1",
        "egress_ip": "198.51.100.10",
    }


@pytest.mark.asyncio
async def test_mobile_page_source_scopes_identity_and_strips_web_only_filters(provenance: BuyerReadProvenance) -> None:
    client = _ReadClient()
    source = BuyerMobileProjectsPageSource(BuyerReadCapabilities(client, provenance))

    response = await source.fetch_page(
        task={
            "page": 2,
            "query_text": "CRM integration",
            "category_id": 42,
            "filters": {"price_to": 100_000, "keyword": "ignored", "kworks_filters": {"x": 1}},
        },
        identity=_identity(),
    )

    assert client.mobile_params == {
        "price_to": 100_000,
        "page": 2,
        "query": "CRM integration",
        "categories": "42",
    }
    assert response["response"] == [{"id": "1", "title": "Project"}]
    assert response["source"] == "mobile_projects"


@pytest.mark.asyncio
async def test_page_source_rejects_cross_account_capability_use(provenance: BuyerReadProvenance) -> None:
    source = BuyerMobileProjectsPageSource(BuyerReadCapabilities(_ReadClient(), provenance))
    identity = _identity()
    identity["account_registration_id"] = "another-account"

    with pytest.raises(BuyerReadPageSourceError, match="identity mismatch"):
        await source.fetch_page(task={"page": 1}, identity=identity)


@pytest.mark.asyncio
async def test_web_page_source_uses_only_web_capability(provenance: BuyerReadProvenance) -> None:
    client = _ReadClient()
    source = BuyerWebProjectsPageSource(BuyerReadCapabilities(client, provenance))

    response = await source.fetch_page(
        task={"page": 3, "query": {"text": "landing", "category_id": 9}, "filters": {"status": "active", "bad": 1}},
        identity=_identity(),
    )

    assert client.web_params == {"status": "active", "page": 3, "query": "landing", "category_id": 9}
    assert response["response"] == [{"id": "2", "title": "Web project"}]
    assert response["source"] == "web_projects"


@pytest.mark.asyncio
async def test_category_browse_omits_query_text_but_keeps_category(provenance: BuyerReadProvenance) -> None:
    client = _ReadClient()
    mobile_source = BuyerMobileProjectsPageSource(BuyerReadCapabilities(client, provenance))
    web_source = BuyerWebProjectsPageSource(BuyerReadCapabilities(client, provenance))
    task = {
        "page": 2,
        "query_text": "Все активные заказы: Создание сайта",
        "query_origin": "category_browse",
        "category_id": 37,
    }

    await mobile_source.fetch_page(task=task, identity=_identity())
    await web_source.fetch_page(task=task, identity=_identity())

    assert client.mobile_params == {"page": 2, "categories": "37"}
    assert client.web_params == {"page": 2, "category_id": 37}
