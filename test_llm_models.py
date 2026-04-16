"""
Test all LLM models on same request.
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from src.brain.llm_router import get_llm_router

test_project = {
    "title": "Parser for real estate website",
    "description": "Need to collect data from avito and cian: prices, addresses, area, photos. Export to Excel. Daily updates.",
    "skills": ["python", "parsing", "excel"],
    "budget": 5000,
}

system_prompt = """
You are a professional developer. Write a short response to client.
RULES:
1. Start with "Hello!"
2. No emojis or markdown
3. 3-4 sentences max
4. Mention relevant experience
5. Ask one technical question
"""

prompt = f"""
PROJECT: {test_project["title"]}
DESCRIPTION: {test_project["description"]}
BUDGET: {test_project["budget"]} rub
Skills: {", ".join(test_project["skills"])}

Write a response to client.
"""


async def test_model(provider: str, model: str, router):
    print(f"\n{'=' * 60}")
    print(f"[TEST] {provider.upper()} | Model: {model}")
    print(f"{'=' * 60}")

    try:
        import time

        start = time.time()

        result = await router.generate(
            prompt=f"{system_prompt}\n\n{prompt}",
            provider=provider,
            model=model,
            temperature=0.75,
            max_tokens=300,
        )

        elapsed = time.time() - start

        print(f"Time: {elapsed:.2f} sec")
        print(f"Response ({len(result)} chars):")
        print(f"{result}")

        return {
            "provider": provider,
            "model": model,
            "time": elapsed,
            "result": result,
            "success": True,
        }

    except Exception as e:
        print(f"[ERROR] {e}")
        return {
            "provider": provider,
            "model": model,
            "time": 0,
            "result": str(e),
            "success": False,
        }


async def main():
    print("\n" + "=" * 60)
    print("TESTING ALL LLM MODELS")
    print("=" * 60)

    router = get_llm_router()

    # Берем модели из .env или используем defaults
    groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    google_model = os.getenv("GOOGLE_MODEL", "gemini-2.0-flash")
    glm_model = os.getenv("GLM_MODEL", "glm-4v-flash")

    tasks = [
        test_model("groq", groq_model, router),
        test_model("google", google_model, router),
        test_model("glm", glm_model, router),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    print("\n" + "=" * 60)
    print("FINAL STATISTICS")
    print("=" * 60)

    for r in results:
        if isinstance(r, dict) and r.get("success"):
            print(f"OK {r['provider']:<10} | {r['time']:.2f}s | {len(r['result'])} chars")
        else:
            print(f"FAIL: {r}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
