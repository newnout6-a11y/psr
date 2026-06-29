"""Manual smoke test: generate 3 proposals with different LLM providers."""

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.brain.llm_router import get_llm_router


TEST_PROJECTS = [
    {
        "title": "Avito/Cian real estate parser",
        "description": "Collect prices, addresses, area, daily update, Excel export.",
        "skills": ["python", "scraping"],
        "budget": 5000,
    },
    {
        "title": "Telegram bot on Python",
        "description": "Auto publishing, stats collection, external API integration.",
        "skills": ["python", "telegram", "bot"],
        "budget": 8000,
    },
    {
        "title": "Product catalog scraper",
        "description": "Collect title, price, description and images for 5000 products.",
        "skills": ["python", "scraping"],
        "budget": 10000,
    },
]

SYSTEM_PROMPT = """
You are a professional freelance developer. Write a short client proposal.
Rules:
1. Start with "Здравствуйте!"
2. No emoji and no markdown.
3. 3-4 sentences.
4. Mention relevant experience.
5. Ask one technical question.
"""

PROVIDERS = ["openai", "deepseek", "groq"]


def model_for(provider: str) -> str:
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-5.5")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    if provider == "groq":
        return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


async def test_generation() -> None:
    router = get_llm_router()

    for index, project in enumerate(TEST_PROJECTS, start=1):
        provider = PROVIDERS[(index - 1) % len(PROVIDERS)]
        prompt = f"""
Order: {project["title"]}
Description: {project["description"]}
Budget: {project["budget"]} RUB.
"""
        started_at = time.time()
        try:
            result = await router.generate(
                prompt=f"{SYSTEM_PROMPT}\n\n{prompt}",
                provider=provider,
                model=model_for(provider),
                temperature=0.75,
                max_tokens=300,
            )
        except Exception as exc:
            print(f"[{index}] {provider}: ERROR {exc}")
            continue

        elapsed = time.time() - started_at
        print(f"[{index}] {provider}: {elapsed:.2f}s, {len(result)} chars")
        print(result)


if __name__ == "__main__":
    asyncio.run(test_generation())
