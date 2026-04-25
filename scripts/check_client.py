"""Ручная проверка заказчика через OSINT + пробив.

Запуск:
    python scripts/check_client.py <username>
    python scripts/check_client.py <username> --description "..."
    python scripts/check_client.py <username> --email foo@bar.com

Показывает:
  - репутацию 0-100
  - положительные сигналы и риски
  - извлечённые контакты
  - все находки с группировкой по источникам
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Делаем запуск из любого места рабочим
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.osint import OSINTAggregator


def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _severity_color(sev: str) -> str:
    return {
        "critical": "1;31",
        "high": "31",
        "medium": "33",
        "low": "36",
        "info": "37",
    }.get(sev, "37")


async def main() -> int:
    parser = argparse.ArgumentParser(description="OSINT-пробив заказчика")
    parser.add_argument("username", help="Ник заказчика (kwork/fl.ru/github/...)")
    parser.add_argument("--description", "-d", default="", help="Описание проекта / bio для извлечения контактов")
    parser.add_argument("--email", "-e", default=None)
    parser.add_argument("--phone", "-p", default=None)
    parser.add_argument("--telegram", "-t", default=None)
    parser.add_argument("--no-cache", action="store_true", help="Сбросить кэш для этого ника")
    args = parser.parse_args()

    agg = OSINTAggregator()

    if args.no_cache:
        import sqlite3
        with sqlite3.connect(agg.cache.db_path) as conn:
            conn.execute("DELETE FROM osint_results WHERE key LIKE ?", (f"user:{args.username.lower()}%",))
            conn.commit()
        print(f"Кэш для {args.username} очищен.\n")

    print(f"→ Пробиваю {args.username!r}...")
    result = await agg.gather(
        username=args.username,
        email=args.email,
        phone=args.phone,
        telegram=args.telegram,
        description=args.description,
    )

    print("=" * 78)
    print(f"  Заказчик:    {_color(result.username, '1;36')}")
    score = result.reputation_score
    score_color = "32" if score >= 60 else "33" if score >= 40 else "31"
    print(f"  Репутация:   {_color(f'{score}/100', score_color)}")
    print("=" * 78)

    if result.contacts:
        print("\n📇  ИЗВЛЕЧЁННЫЕ КОНТАКТЫ")
        for kind, values in result.contacts.items():
            if values:
                print(f"   {kind:10s}: {', '.join(values)}")

    if result.positive_signals:
        print(f"\n✅  ПЛЮСЫ ({len(result.positive_signals)})")
        for s in result.positive_signals:
            print(f"   + {s}")

    if result.red_flags:
        print(f"\n⚠️   РИСКИ ({len(result.red_flags)})")
        for s in result.red_flags:
            print(f"   - {_color(s, '31')}")

    # OSINT-находки
    osint_by_src: dict[str, list] = {}
    for f in result.findings:
        osint_by_src.setdefault(f.source, []).append(f)
    if osint_by_src:
        print(f"\n🔎  OSINT-НАХОДКИ ({len(result.findings)})")
        for src, items in osint_by_src.items():
            print(f"\n   [{_color(src, '1;34')}] {len(items)} шт.")
            for f in items[:6]:
                print(f"      · {f.title}")
                if f.url:
                    print(f"        {f.url}")
        if any(len(v) > 6 for v in osint_by_src.values()):
            print("      ... (обрезано до 6 на источник)")

    # Пробив-находки
    probiv_by_src: dict[str, list] = {}
    for f in result.probiv_findings:
        probiv_by_src.setdefault(f.source, []).append(f)
    if probiv_by_src:
        print(f"\n🎯  ПРОБИВ ({len(result.probiv_findings)})")
        for src, items in probiv_by_src.items():
            print(f"\n   [{_color(src, '1;35')}] {len(items)} шт.")
            for f in items[:10]:
                sev = _color(f.severity.upper(), _severity_color(f.severity))
                print(f"      [{sev}] {f.title}")
                if f.snippet:
                    print(f"             {f.snippet[:120]}")
                if f.url:
                    print(f"             {f.url}")

    if not result.findings and not result.probiv_findings:
        print("\n  ℹ️  Публичных следов не найдено.")

    print("\n" + "=" * 78)
    print("  Резюме для LLM:\n")
    for line in result.summary.splitlines():
        print(f"    {line}")
    print("=" * 78)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
