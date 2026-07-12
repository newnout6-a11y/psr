from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from src.platforms.kwork_supply import BatchState, ContractState, ProtectionStatus
from src.platforms.kwork_supply.sources.web_catalog import KworkWebCatalogAdapter


class FakeWebCatalogClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def get_web_catalog_filters(
        self,
        alias: str,
        *,
        page: int = 1,
        page_size: int = 10,
        filters: Mapping[str, object] | None = None,
        include_raw: bool = False,
        cookies: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "alias": alias,
                "page": page,
                "page_size": page_size,
                "filters": dict(filters or {}),
                "include_raw": include_raw,
                "cookies": dict(cookies or {}),
            }
        )
        return self.responses.pop(0)


def make_raw_payload(cards: list[Mapping[str, object]], *, category_id: int = 38) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            "stateData": {
                "viewData": {
                    "filters": {"activeCategoryId": category_id, "kworksCount": 13000},
                    "kworks": {
                        "total": 1000,
                        "total_found": 13000,
                        "posts": {"data": cards},
                    },
                }
            }
        },
    }


def make_helper_response(
    raw: Mapping[str, object] | None,
    *,
    status_code: int = 200,
    request_params: Mapping[str, object] | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "alias": "website-repair",
        "endpoint": "/catalog_kworks_filters/website-repair",
        "url": "https://kwork.ru/catalog_kworks_filters/website-repair",
        "request_params": dict(request_params or {"page": 1, "pageSize": 10}),
        "status_code": status_code,
        "content_type": "application/json",
        "bytes": 4321,
        "protection_status": "blocked" if status_code == 403 else "ok",
    }
    if raw is not None:
        response["raw"] = dict(raw)
    return response


@pytest.fixture
def first_helper_response() -> dict[str, object]:
    return make_helper_response(make_raw_payload([{"id": 101}, {"id": 102}]))


@pytest.fixture
def session_cookies() -> dict[str, str]:
    return {"slrememberme": "present"}


@pytest.mark.asyncio
async def test_adapter_fetches_first_batch_with_raw_response_and_metadata(
    first_helper_response: dict[str, object],
    session_cookies: dict[str, str],
):
    client = FakeWebCatalogClient([first_helper_response])
    adapter = KworkWebCatalogAdapter(client=client, cookies=session_cookies)
    request = adapter.build_request(alias="website-repair", category_id=38)

    result = await adapter.fetch_batch(request)

    assert client.calls == [
        {
            "alias": "website-repair",
            "page": 1,
            "page_size": 10,
            "filters": {},
            "include_raw": True,
            "cookies": {"slrememberme": "present"},
        }
    ]
    assert [card["id"] for card in result.cards] == [101, 102]
    assert result.actual_item_count == 2
    assert result.metadata["adapter"] == {
        "endpoint": "/catalog_kworks_filters/website-repair",
        "url": "https://kwork.ru/catalog_kworks_filters/website-repair",
        "status_code": 200,
            "content_type": "application/json",
            "protection_status": "ok",
            "retry_after": None,
            "request_params": {"page": 1, "pageSize": 10},
        "continuation_params": {},
    }


@pytest.mark.asyncio
async def test_adapter_sends_exact_exclude_ids_and_one_page_for_continuation(
    first_helper_response: dict[str, object],
):
    second_helper_response = make_helper_response(
        make_raw_payload([{"id": 103}, {"id": 104}]),
        request_params={
            "page": 1,
            "pageSize": 10,
            "excludeIds": "101,102",
            "onePage": 1,
        },
    )
    client = FakeWebCatalogClient([first_helper_response, second_helper_response])
    adapter = KworkWebCatalogAdapter(client=client)
    first_request = adapter.build_request(alias="website-repair", category_id=38)
    first_result = await adapter.fetch_batch(first_request)
    previous = BatchState().accept(first_result)
    continuation_request = adapter.build_request(
        alias="website-repair",
        category_id=38,
        cursor=first_result.next_cursor,
    )

    second_result = await adapter.fetch_batch(continuation_request)
    verdict = adapter.validate_batch(continuation_request, second_result, previous)

    assert client.calls[1]["filters"] == {"excludeIds": "101,102", "onePage": 1}
    assert verdict.state is ContractState.ACCEPTED
    assert second_result.next_cursor is not None
    assert second_result.next_cursor.exclude_ids == ("101", "102", "103", "104")


@pytest.mark.asyncio
async def test_adapter_preserves_shard_filters_across_continuation(
    first_helper_response: dict[str, object],
):
    second_helper_response = make_helper_response(make_raw_payload([{"id": 103}]))
    client = FakeWebCatalogClient([first_helper_response, second_helper_response])
    adapter = KworkWebCatalogAdapter(client=client)
    initial = adapter.build_request(
        alias="website-repair",
        category_id=38,
        filters={"price_from": 1000, "attribute[10]": "python"},
    )

    first = await adapter.fetch_batch(initial)
    continuation = adapter.build_request(
        alias="website-repair",
        category_id=38,
        cursor=first.next_cursor,
        filters=initial.scope["filters"],
    )
    await adapter.fetch_batch(continuation)

    assert client.calls[0]["filters"] == {"price_from": 1000, "attribute[10]": "python"}
    assert client.calls[1]["filters"] == {
        "price_from": 1000,
        "attribute[10]": "python",
        "excludeIds": "101,102",
        "onePage": 1,
    }


@pytest.mark.asyncio
async def test_adapter_accepts_three_chained_batches_with_72_unique_cards():
    responses = [
        make_helper_response(make_raw_payload([{"id": item} for item in range(start, start + 24)]))
        for start in (1, 25, 49)
    ]
    client = FakeWebCatalogClient(responses)
    adapter = KworkWebCatalogAdapter(client=client)
    request = adapter.build_request(alias="website-repair", category_id=38)
    previous = None
    seen_ids: set[int] = set()

    for _ in range(3):
        result = await adapter.fetch_batch(request)
        verdict = adapter.validate_batch(request, result, previous)

        assert verdict.state is ContractState.ACCEPTED
        assert result.actual_item_count == 24
        seen_ids.update(int(card["id"]) for card in result.cards)
        previous = (previous or BatchState()).accept(result)
        request = adapter.build_request(
            alias="website-repair",
            category_id=38,
            cursor=result.next_cursor,
        )

    assert len(seen_ids) == 72
    assert client.calls[1]["filters"] == {"excludeIds": ",".join(str(item) for item in range(1, 25)), "onePage": 1}
    assert client.calls[2]["filters"] == {"excludeIds": ",".join(str(item) for item in range(1, 49)), "onePage": 1}


@pytest.mark.asyncio
async def test_adapter_maps_403_to_blocked_result_and_keeps_helper_metadata():
    client = FakeWebCatalogClient([make_helper_response(None, status_code=403)])
    adapter = KworkWebCatalogAdapter(client=client)
    request = adapter.build_request(alias="website-repair", category_id=38)

    result = await adapter.fetch_batch(request)
    verdict = adapter.validate_batch(request, result, previous=None)

    assert result.protection_status is ProtectionStatus.BLOCKED
    assert result.metadata["adapter"]["status_code"] == 403
    assert result.metadata["adapter"]["protection_status"] == "blocked"
    assert verdict.state is ContractState.BLOCKED


def test_adapter_allows_nested_canonical_paths_and_rejects_unsafe_aliases():
    adapter = KworkWebCatalogAdapter(client=FakeWebCatalogClient([]))

    nested = adapter.build_request(alias="/parent/child/", category_id=38)

    assert nested.scope["alias"] == "parent/child"
    for value in ("../child", "https://kwork.ru/parent/child", "//kwork.ru/parent", "parent?x=1", "parent#tab"):
        with pytest.raises(ValueError):
            adapter.build_request(alias=value, category_id=38)
