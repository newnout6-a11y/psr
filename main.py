"""
Главный входной файл системы.
Автономный агент (парсинг -> фильтр -> отклик).
"""

import argparse
import asyncio
import os
import signal
import sys

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table

# Загружаем окружение ПЕРЕД всеми импортами.
load_dotenv()

if sys.platform == "win32":
    try:
        import asyncio.base_subprocess as base_subprocess
        import asyncio.proactor_events as proactor_events

        transport_cls = getattr(proactor_events, "_ProactorBasePipeTransport", None) or getattr(
            proactor_events, "ProactorBasePipeTransport", None
        )
        if transport_cls and not getattr(transport_cls, "_psr_del_patched", False):
            original_del = transport_cls.__del__

            def _patched_del(self):
                try:
                    original_del(self)
                except (ValueError, OSError, RuntimeError):
                    pass

            transport_cls.__del__ = _patched_del
            transport_cls._psr_del_patched = True

        subprocess_cls = getattr(base_subprocess, "BaseSubprocessTransport", None)
        if subprocess_cls and not getattr(subprocess_cls, "_psr_del_patched", False):
            original_subprocess_del = subprocess_cls.__del__

            def _patched_subprocess_del(self):
                try:
                    original_subprocess_del(self)
                except (ValueError, OSError, RuntimeError):
                    pass

            subprocess_cls.__del__ = _patched_subprocess_del
            subprocess_cls._psr_del_patched = True
    except Exception:
        pass

from src.orchestrator import FreelanceOrchestrator
from src.paths import GENERATED_PROPOSALS_FILE, LOGS_TXT_FILE, ensure_layout


console = Console()


def _register_signal_handlers(stop_event: asyncio.Event):
    def _handle_signal(signum, _frame):
        try:
            signal_name = signal.Signals(signum).name
        except Exception:
            signal_name = str(signum)
        logger.info(f"Получен сигнал {signal_name}, начинаем корректную остановку...")
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except Exception as e:
            logger.debug(f"Не удалось зарегистрировать {signal_name}: {e}")


async def main():
    # Очищаем логи перед каждым запуском
    ensure_layout()
    LOGS_TXT_FILE.write_text("", encoding="utf-8")

    parser = argparse.ArgumentParser(description="Автономный фрилансер.")
    parser.add_argument("--continuous", action="store_true", help="Режим 24/7")
    parser.add_argument("--dry-run", action="store_true", help="Парсинг + LLM, без реальной отправки")
    parser.add_argument("--limit", type=int, default=5, help="Лимит откликов на одну платформу за цикл")
    parser.add_argument("--search", type=str, default=None, help="Описание поиска своими словами (AI сгенерирует запросы)")
    parser.add_argument("--top", type=int, default=None, help="Кол-во топ-проектов для обработки (default: 3)")
    args = parser.parse_args()

    logger.add(str(LOGS_TXT_FILE), mode="w", encoding="utf-8")
    GENERATED_PROPOSALS_FILE.write_text("", encoding="utf-8")

    if args.search:
        os.environ["SEARCH_BRIEF"] = args.search
        console.print(f"[cyan]Поиск:[/cyan] {args.search}")
    if args.top:
        os.environ["TOP_PROJECTS"] = str(args.top)

    stop_event = asyncio.Event()
    _register_signal_handlers(stop_event)

    orchestrator = FreelanceOrchestrator()
    await orchestrator.notifier.start()

    console.print("[dim]Синхронизация с Telegram... (5 сек)[/dim]")
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass

    cycle_interval = int(os.getenv("CYCLE_INTERVAL", "1800"))
    continuous = args.continuous or os.getenv("CONTINUOUS_MODE", "false").lower() == "true"

    try:
        while not stop_event.is_set():
            console.print("\n[bold cyan]=== Запуск цикла Orchestrator ===[/bold cyan]")
            stats = await orchestrator.run_cycle(dry_run=args.dry_run, limit_per_platform=args.limit)

            table = Table(title="Итоги цикла")
            table.add_column("Метрика", style="magenta")
            table.add_column("Значение", style="green", justify="right")
            table.add_row("Спарсено проектов", str(stats["parsed"]))
            table.add_row("Прошло keyword-фильтры", str(stats["filtered"]))
            table.add_row("Прошло AI-скоринг", f"[cyan]{stats.get('ai_passed', '?')}[/cyan]")
            table.add_row("Сгенерировано/Отправлено откликов", str(stats["sent"]))

            if stats["sent"] > 0:
                for platform, count in stats["per_platform"].items():
                    table.add_row(f"  └─ {platform}", str(count))
            console.print(table)

            if not continuous or stop_event.is_set():
                break

            console.print(f"[dim]Спим {cycle_interval} секунд до следующего цикла...[/dim]")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=cycle_interval)
            except asyncio.TimeoutError:
                pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Остановка пользователем...")
    finally:
        stop_event.set()
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
