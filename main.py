"""
Главный входной файл системы.
Автономный агент (парсинг -> фильтр -> отклик).
"""

import os
import sys
import argparse
from dotenv import load_dotenv

# Загружаем окружение ПЕРЕД всеми импортами!
load_dotenv()

import asyncio
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.orchestrator import FreelanceOrchestrator

# Graceful shutdown: suppress Windows "I/O operation on closed pipe" errors
if sys.platform == "win32":
    from asyncio.proactor_events import BaseProactorEventLoop
    _orig_del = BaseProactorEventLoop.__del__

    def _patched_del(self):
        try:
            _orig_del(self)
        except (ValueError, OSError):
            pass

    BaseProactorEventLoop.__del__ = _patched_del

console = Console()


async def main():
    parser = argparse.ArgumentParser(description="Автономный фрилансер.")
    parser.add_argument("--continuous", action="store_true", help="Режим 24/7")
    parser.add_argument("--dry-run", action="store_true", help="Парсинг + LLM, без реальной отправки")
    parser.add_argument("--limit", type=int, default=5, help="Лимит откликов на одну платформу за цикл")
    parser.add_argument("--search", type=str, default=None, help="Описание поиска своими словами (AI сгенерирует запросы)")
    parser.add_argument("--top", type=int, default=None, help="Кол-во топ-проектов для обработки (default: 3)")
    args = parser.parse_args()

    # Настройка логера (перезапись при каждом новом запуске)
    logger.add("data/logs.txt", mode="w", encoding="utf-8")

    # Очищаем файл со сгенерированными откликами при каждом новом запуске
    open("data/generated_proposals.txt", "w", encoding="utf-8").close()

    # Передаём описание поиска из CLI в env (приоритет над .env)
    if args.search:
        os.environ["SEARCH_BRIEF"] = args.search
        console.print(f"[cyan]Поиск:[/cyan] {args.search}")
    if args.top:
        os.environ["TOP_PROJECTS"] = str(args.top)

    orchestrator = FreelanceOrchestrator()

    # Запускаем слушателя Telegram бота
    await orchestrator.notifier.start()

    # Даем боту 5 секунд, чтобы он успел вычитать /start и узнать ID админа
    console.print("[dim]Синхронизация с Telegram... (5 сек)[/dim]")
    await asyncio.sleep(5)

    cycle_interval = int(os.getenv("CYCLE_INTERVAL", "1800"))
    continuous = args.continuous or os.getenv("CONTINUOUS_MODE", "false").lower() == "true"

    try:
        while True:
            console.print("\n[bold cyan]=== Запуск цикла Orchestrator ===[/bold cyan]")
            stats = await orchestrator.run_cycle(dry_run=args.dry_run, limit_per_platform=args.limit)

            # Красивый вывод итогов
            t = Table(title="Итоги цикла")
            t.add_column("Метрика", style="magenta")
            t.add_column("Значение", style="green", justify="right")

            t.add_row("Спарсенено проектов", str(stats["parsed"]))
            t.add_row("Прошло keyword-фильтры", str(stats["filtered"]))
            t.add_row("Прошло AI-скоринг", f"[cyan]{stats.get('ai_passed', '?')}[/cyan]")
            t.add_row("Сгенерировано/Отправлено откликов", str(stats["sent"]))

            if stats["sent"] > 0:
                for plat, count in stats["per_platform"].items():
                    t.add_row(f"  └─ {plat}", str(count))
            console.print(t)

            if not continuous:
                break

            console.print(f"[dim]Спим {cycle_interval} секунд до следующего цикла...[/dim]")
            await asyncio.sleep(cycle_interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Остановка пользователем...")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
