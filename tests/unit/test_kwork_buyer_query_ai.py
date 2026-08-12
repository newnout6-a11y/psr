from __future__ import annotations

import pytest

from src.platforms.kwork_buyer.models import BuyerRunMode
from src.platforms.kwork_buyer.query_ai import (
    QUERY_GENERATION_TASK,
    generate_buyer_query_candidates_with_ai,
)
from src.platforms.kwork_buyer.query_generation import BuyerQueryGenerationInput


class _Gateway:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    async def generate(self, *, prompt: str, task: str) -> object:
        self.calls.append({"prompt": prompt, "task": task})
        return self.response


@pytest.mark.asyncio
async def test_ai_query_generation_returns_only_reviewable_structured_candidates() -> None:
    gateway = _Gateway(
        {
            "queries": [
                {"text": "telegram bot integration", "rationale": "Specific service need", "priority": 5, "predicted_total": 17},
                {"text": "crm automation telegram", "rationale": "Adjacent intent"},
            ]
        }
    )
    request = BuyerQueryGenerationInput(
        mode=BuyerRunMode.HYBRID,
        brief="Telegram automation",
        category_id=42,
        category_path=(10, 42),
        minimum_count=3,
    )

    result = await generate_buyer_query_candidates_with_ai(request, gateway=gateway)  # type: ignore[arg-type]

    assert result.used_model is True
    assert gateway.calls[0]["task"] == QUERY_GENERATION_TASK
    assert result.generation.generator == "llm_structured"
    assert result.generation.candidates[0].text == "telegram bot integration"
    assert result.generation.candidates[0].predicted_total == 17


@pytest.mark.asyncio
async def test_ai_query_generation_surfaces_invalid_model_output_without_template_queries() -> None:
    request = BuyerQueryGenerationInput(mode=BuyerRunMode.BRIEF, brief="landing page", minimum_count=2)
    result = await generate_buyer_query_candidates_with_ai(request, gateway=_Gateway("not json"))  # type: ignore[arg-type]

    assert result.used_model is False
    assert result.error is not None
    assert result.generation.used_fallback is False
    assert result.generation.generator == "llm_error"
    assert result.generation.candidates == ()
