from __future__ import annotations

from typing import Any

import pytest

from src.platforms.kwork_supply.handoff import MarketRecommendationHandoffService
from src.platforms.kwork_supply.models import MarketJobCreate, MarketScope, Operation, OperationKind, ShardSpec
from src.platforms.kwork_supply.repository import MarketJobRepository, MarketJobRepositoryError


async def _job_with_dossier(repository: MarketJobRepository) -> int:
    await repository.create_job(
        MarketJobCreate(
            scope=MarketScope(category_id=41, canonical_alias="scripts-bots"),
            target_unique_cards=10,
            desired_workers=1,
        ),
        job_id="job_handoff",
    )
    await repository.create_shard(
        ShardSpec(shard_id="shard_handoff", job_id="job_handoff", source="web_catalog", alias="scripts-bots")
    )
    await repository.enqueue_operation(
        Operation(
            operation_id="fetch_handoff",
            job_id="job_handoff",
            shard_id="shard_handoff",
            kind=OperationKind.FETCH_BATCH,
        )
    )
    leased = await repository.lease_operation("worker_handoff", job_id="job_handoff")
    assert leased is not None
    await repository.commit_accepted_batch(
        job_id="job_handoff",
        shard_id="shard_handoff",
        operation_id="fetch_handoff",
        attempt_id=leased["attempt_id"],
        listings=[{"id": 1001, "gtitle": "Telegram lead bot", "price": 4900, "userName": "alice"}],
        source="web_catalog",
        idempotency_key="handoff-listing",
    )
    listing = (await repository.list_listings("job_handoff"))[0]
    listing_id = int(listing["listing_id"])
    await repository.replace_semantic_analysis(
        "job_handoff",
        {
            "embedding_model": "test-handoff",
            "embeddings": [{"listing_id": listing_id, "text_hash": "handshake", "vector": [1.0, 0.0]}],
            "clusters": [
                {
                    "cluster_id": "cluster_bot",
                    "label": "telegram lead bot",
                    "state": "promising_for_entry",
                    "confidence": 82,
                    "member_listing_ids": [listing_id],
                    "metrics": {},
                }
            ],
            "dossier": {
                "schema_version": 1,
                "coverage": {"eligible_listing_count": 1, "cluster_count": 1},
                "required_response_schema": {"verdict": "promising_for_entry"},
                "groups": [
                    {
                        "cluster_id": "cluster_bot",
                        "label": "telegram lead bot",
                        "verdict_input": "promising_for_entry",
                        "confidence": 82,
                        "reasons": ["fresh_demand_signal"],
                        "evidence_ids": [f"listing_{listing_id}"],
                        "metrics": {},
                        "representatives": [{"evidence_id": f"listing_{listing_id}", "listing_id": listing_id}],
                    }
                ],
            },
        },
    )
    return listing_id


class _DraftService:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.publish_requests: list[dict[str, Any]] = []

    async def generate_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ok": True,
            "draft": {
                "category_id": request["category_id"],
                "title": request["service_summary"],
                "description": "Draft description",
                "price": request["price"],
                "work_time": request["work_time"],
            },
            "image": None,
        }

    @staticmethod
    def draft_hash(_draft: dict[str, Any]) -> str:
        return "test-draft-hash"

    async def publish_draft(self, draft: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.publish_requests.append({"draft": draft, **kwargs})
        return {"ok": True, "code": "verified", "verify_result": {"kwork_id": "9001"}}


@pytest.mark.asyncio
async def test_recommendation_handoff_requires_confirmed_fields_and_keeps_server_selection(tmp_path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    listing_id = await _job_with_dossier(repository)
    calls: list[dict[str, Any]] = []

    async def manifest_loader(
        category_id: int,
        classifier_id: int | None,
        selection: dict[str, Any],
        lang: str,
    ) -> dict[str, Any]:
        calls.append({"category_id": category_id, "classifier_id": classifier_id, "selection": selection, "lang": lang})
        controls: list[dict[str, Any]] = [
            {
                "name": "attribute[208]",
                "group_id": 208,
                "type": "radio",
                "required": True,
                "options": [
                    {"id": 3587, "label": "Bots", "has_child": True},
                    {"id": 9999, "label": "Other"},
                ],
            }
        ]
        if selection.get("attribute[208]") == 3587:
            controls.append(
                {
                    "name": "attribute[3610][]",
                    "group_id": 3610,
                    "type": "checkbox",
                    "multiple": True,
                    "required": True,
                    "parent_option_ids": [3587],
                    "options": [
                        {"id": 3612, "label": "Telegram"},
                        {"id": 5273361, "label": "Disabled", "disabled": True},
                    ],
                }
            )
        return {"success": True, "category_id": category_id, "classifier_id": classifier_id, "lang": lang, "controls": controls}

    draft_service = _DraftService()
    service = MarketRecommendationHandoffService(
        repository,
        manifest_loader,
        autopublish_service=draft_service,  # type: ignore[arg-type]
    )
    terra = {
        "verdict": "recommend",
        "confidence": 0.82,
        "reasons": ["Fresh listing-level reviews"],
        "evidence_ids": [f"listing_{listing_id}"],
        "recommended_offer": {"service_summary": "Telegram lead bot"},
        "rejected_cases": [],
    }
    recommendation = await service.propose(
        "job_handoff",
        category_id=41,
        classifier_id=3587,
        service_summary="Telegram lead bot for requests",
        price=4900,
        work_time=5,
        source_cluster_id="cluster_bot",
        terra_result=terra,
    )

    confirmed = await service.confirm_recommendation(recommendation["recommendation_id"])
    handoff = confirmed["handoff"]
    assert handoff is not None and handoff["state"] == "mapping"

    initial = await service.refresh_manifest(handoff["handoff_id"])
    selected_parent = await service.update_selection(
        initial["handoff_id"],
        selection={"attribute[208]": 3587},
        expected_manifest_hash=initial["attribute_manifest_hash"],
    )
    loaded_children = await service.refresh_manifest(selected_parent["handoff_id"])
    confirmed_fields = await service.update_selection(
        loaded_children["handoff_id"],
        selection={
            "attribute[208]": 3587,
            "attribute[3610][]": [3612, 5273361],
            "untrusted": 1,
        },
        expected_manifest_hash=loaded_children["attribute_manifest_hash"],
        confirm=True,
    )

    assert confirmed_fields["state"] == "fields_confirmed"
    assert confirmed_fields["attribute_selection"] == {
        "attribute[208]": 3587,
        "attribute[3610][]": [3612],
    }
    assert confirmed_fields["generator_request"] == {"status": "ready"}
    assert confirmed_fields["validation"]["issues"]["unknown_fields"] == ["untrusted"]
    assert len(calls) == 2

    generated = await service.generate_draft(confirmed_fields["handoff_id"], generation_options={"use_llm": False})
    assert generated["handoff"]["state"] == "draft_generated"
    assert generated["draft"]["attribute_selection"] == confirmed_fields["attribute_selection"]
    assert draft_service.requests[0]["attribute_selection"] == confirmed_fields["attribute_selection"]

    published = await service.publish_draft(
        confirmed_fields["handoff_id"],
        dry_run=False,
        confirm_token="fresh-token",
        confirmation="PUBLISH",
    )
    assert published["publish"]["ok"] is True
    assert published["published_listing"]["kwork_id"] == "9001"
    assert published["published_listing"]["source_cluster_id"] == "cluster_bot"
    assert draft_service.publish_requests[0]["draft"]["handoff_id"] == confirmed_fields["handoff_id"]
    assert (await repository.list_published_listings("job_handoff"))[0]["published_listing_id"] == published["published_listing"]["published_listing_id"]

    with pytest.raises(MarketJobRepositoryError):
        await service.update_selection(
            confirmed_fields["handoff_id"],
            selection={"attribute[208]": 9999},
            expected_manifest_hash="sha256:stale",
        )

    changed = await service.update_selection(
        confirmed_fields["handoff_id"],
        selection={"attribute[208]": 9999, "attribute[3610][]": [3612]},
        expected_manifest_hash=confirmed_fields["attribute_manifest_hash"],
        confirm=True,
    )
    assert changed["state"] == "fields_confirmed"
    assert changed["attribute_selection"] == {"attribute[208]": 9999}
    assert changed["draft"] == {}


@pytest.mark.asyncio
async def test_handoff_rejects_terra_evidence_outside_durable_dossier(tmp_path):
    repository = MarketJobRepository(tmp_path / "market.sqlite3")
    await _job_with_dossier(repository)

    async def unused_loader(*_args: Any) -> dict[str, Any]:
        raise AssertionError("manifest should not be loaded")

    service = MarketRecommendationHandoffService(repository, unused_loader)
    with pytest.raises(ValueError, match="durable dossier"):
        await service.propose(
            "job_handoff",
            category_id=41,
            service_summary="Invalid evidence",
            price=4900,
            work_time=5,
            terra_result={
                "verdict": "recommend",
                "confidence": 0.8,
                "reasons": ["bad"],
                "evidence_ids": ["listing_not_found"],
                "recommended_offer": {},
                "rejected_cases": [],
            },
        )
