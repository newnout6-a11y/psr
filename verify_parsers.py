import asyncio
import os
import argparse
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.parsers import (
    HHParser,
    RemoteOKParser,
    FreelancerComParser,
    FLRuParser,
    KworkParser,
    FreelanceRuParser,
    PeoplePerHourParser,
    UpworkParser,
    WeblancerParser,
    OneCLancerParser,
)

console = Console()

PARSERS = {
    "hh_ru": HHParser,
    "remoteok": RemoteOKParser,
    "freelancer_com": FreelancerComParser,
    "fl_ru": FLRuParser,
    "kwork": KworkParser,
    "freelance_ru": FreelanceRuParser,
    "pph": PeoplePerHourParser,
    "upwork": UpworkParser,
    "weblancer": WeblancerParser,
    "oneclancer": OneCLancerParser,
}

async def verify_platform(platform_name: str, ParserClass):
    console.print(f"\n[cyan]Тестируем парсер {platform_name}...[/cyan]")
    parser = ParserClass()
    try:
        # Pass a simple filter string that's valid to all
        projects = await parser.get_projects(page=1, per_page=10, filters={"query": "python"})
        
        table = Table(title=f"Проекты с {platform_name}")
        table.add_column("Title", style="magenta", max_width=40)
        table.add_column("Budget", justify="right", style="green")
        table.add_column("Currency", style="green")
        table.add_column("Skills", style="blue")
        
        for p in projects:
            title = p.title[:37] + "..." if len(p.title) > 40 else p.title
            skills = ", ".join(p.skills)[:30] + "..." if len(", ".join(p.skills)) > 30 else ", ".join(p.skills)
            table.add_row(title, str(p.budget), p.currency, skills)
            
        console.print(table)
        console.print(f"[bold green][+] {platform_name}: найдено {len(projects)} проектов[/bold green]")
    except Exception as e:
        logger.exception(f"Ошибка при тестировании {platform_name}: {e}")
        console.print(f"[bold red]x {platform_name}: ошибка (см. логи)[/bold red]")
    finally:
        parser.close()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", type=str, help="Платформа для тестирования")
    parser.add_argument("--all", action="store_true", help="Тестировать все")
    args = parser.parse_args()

    # Create dummy filter setup just to ensure paths exist
    os.makedirs("data", exist_ok=True)
    os.makedirs("config", exist_ok=True)

    if args.platform:
        if args.platform in PARSERS:
            await verify_platform(args.platform, PARSERS[args.platform])
        else:
            console.print(f"[bold red]Неизвестная платформа: {args.platform}[/bold red]")
    elif args.all:
        for name, pclass in PARSERS.items():
            await verify_platform(name, pclass)
    else:
        console.print("Укажите --platform или --all")

if __name__ == "__main__":
    asyncio.run(main())
