"""Быстрый smoke-тест OSINT-модуля.

Запуск: python -m pytest tests/integration/test_osint_integration.py -s
или:    python tests/integration/test_osint_integration.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.osint import OSINTAggregator  # noqa: E402


async def main() -> None:
    agg = OSINTAggregator()
    # Тестируем на заведомо существующем github-пользователе
    result = await agg.gather("torvalds", context="linux kernel")

    print("=" * 60)
    print(f"Username:   {result.username}")
    print(f"Reputation: {result.reputation_score}/100")
    print(f"Positive:   {result.positive_signals}")
    print(f"Red flags:  {result.red_flags}")
    print(f"Findings:   {len(result.findings)}")
    print("-" * 60)
    for f in result.findings[:10]:
        print(f"  [{f.source:16s}] {f.kind:10s} {f.title[:60]:60s} conf={f.confidence}")
    print("=" * 60)
    print(f"Summary:\n{result.summary}")

    # 2-й вызов должен прийти из кэша
    print("\n--- Второй вызов (ожидаем кэш) ---")
    result2 = await agg.gather("torvalds", context="linux kernel")
    print(f"Findings из кэша: {len(result2.findings)}")


if __name__ == "__main__":
    asyncio.run(main())
