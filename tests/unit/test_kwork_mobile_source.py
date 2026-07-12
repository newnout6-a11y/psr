from __future__ import annotations

from typing import Any

import pytest

from src.platforms.kwork_supply import ContractState, SourceCursor
from src.platforms.kwork_supply.sources.mobile_kworks import KworkMobileKworksAdapter


class FakeMobileClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, int | None]] = []

    async def get_kworks(
        self,
        *,
        category_id: int | None = None,
        classifier_id: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "category_id": category_id,
                "classifier_id": classifier_id,
                "page": page,
            }
        )
        return self.responses.pop(0)


@pytest.fixture
def first_page_catalog() -> dict[str, object]:
    return {
        "kworks_count": 1000,
        "paging": {"page": 1, "total": 1000},
        "kworks": [
            {"id": 201, "title": "First mobile offer"},
            {"id": 202, "title": "Second mobile offer"},
        ],
        "_request_params": {"categoryId": 38, "classifierId": 1271, "page": 1},
    }


@pytest.fixture
def adapter(first_page_catalog: dict[str, object]) -> KworkMobileKworksAdapter:
    return KworkMobileKworksAdapter(client=FakeMobileClient([first_page_catalog]))


@pytest.mark.asyncio
async def test_mobile_adapter_fetches_only_page_one_and_exposes_reported_paging(
    adapter: KworkMobileKworksAdapter,
    first_page_catalog: dict[str, object],
):
    request = adapter.build_request(category_id=38, classifier_id=1271)

    result = await adapter.fetch_batch(request)
    verdict = adapter.validate_batch(request, result, previous=None)

    client = adapter.client
    assert isinstance(client, FakeMobileClient)
    assert client.calls == [{"category_id": 38, "classifier_id": 1271, "page": 1}]
    assert [card["id"] for card in result.cards] == [201, 202]
    assert result.requested_cursor == SourceCursor.page_cursor(1)
    assert result.reported_cursor == SourceCursor.page_cursor(1)
    assert result.actual_item_count == 2
    assert result.source_total == 1000
    assert result.source_total_found == 1000
    assert result.next_cursor is None
    assert result.metadata["shape_valid"] is True
    assert result.metadata["shape_error"] is None
    assert result.metadata["reported_page"] == 1
    assert result.metadata["request_params"] == {"categoryId": 38, "classifierId": 1271, "page": 1}
    assert result.metadata["_raw_payload"] == first_page_catalog
    assert verdict.state is ContractState.ACCEPTED


@pytest.mark.asyncio
async def test_missing_reported_paging_becomes_a_contract_violation():
    client = FakeMobileClient(
        [
            {
                "kworks_count": 1000,
                "kworks": [{"id": 201}],
            }
        ]
    )
    adapter = KworkMobileKworksAdapter(client=client)
    request = adapter.build_request(category_id=38)

    result = await adapter.fetch_batch(request)
    verdict = adapter.validate_batch(request, result, previous=None)

    assert result.reported_cursor is None
    assert result.metadata["reported_page"] is None
    assert verdict.state is ContractState.CONTRACT_VIOLATION
    assert "reported_cursor_missing" in verdict.reason_codes


def test_mobile_adapter_rejects_continuation_before_client_call(adapter: KworkMobileKworksAdapter):
    with pytest.raises(ValueError):
        adapter.build_request(category_id=38, cursor=SourceCursor.page_cursor(2))

    client = adapter.client
    assert isinstance(client, FakeMobileClient)
    assert client.calls == []
