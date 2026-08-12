from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.ai_scoring import (
    BUYER_FINAL_SCORING_TASK,
    BuyerFinalScoringError,
    score_buyer_project_final,
)


class _Gateway:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str) -> object:
        self.calls.append({"prompt": prompt, "task": task})
        return self.response


@pytest.mark.asyncio
async def test_final_scoring_persists_versioned_model_evidence() -> None:
    gateway = _Gateway(
        {
            "total": 83,
            "breakdown": {"fit": 90, "risk": 72, "value": 87},
            "rationale": "Clear scope and enough budget.",
            "provider": "openai",
            "model": "gpt-5.6-sol",
        }
    )

    result = await score_buyer_project_final(
        {
            "project_id": "project-1",
            "title": "Telegram automation",
            "description": "Need a Telegram bot integration.",
            "budget_max": 25_000,
            "attachments": [{"filename": "brief.pdf", "detected_type": "pdf", "state": "parsed"}],
        },
        gateway=gateway,  # type: ignore[arg-type]
        profile_id="buyer-ai",
        profile_version="2",
    )

    payload = result.to_payload()
    assert gateway.calls[0]["task"] == BUYER_FINAL_SCORING_TASK
    assert payload["score_kind"] == "final"
    assert payload["profile_id"] == "buyer-ai"
    assert payload["profile_version"] == "2"
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["input_context_hash"].startswith("sha256:")
    assert payload["prompt_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_final_scoring_rejects_unbounded_or_non_json_model_values() -> None:
    gateway = _Gateway('{"total": 101, "breakdown": {"fit": 80}, "rationale": "x", "provider": "test", "model": "test"}')

    with pytest.raises(BuyerFinalScoringError, match="0 to 100"):
        await score_buyer_project_final({"project_id": "project-1"}, gateway=gateway)  # type: ignore[arg-type]
