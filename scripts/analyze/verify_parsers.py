import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.paths import ensure_layout
from src.filter.project_filter import ProjectFilter
from src.parsers import FreelanceRuParser, HHParser, KworkAPIParser


console = Console()
PER_PAGE = ProjectFilter().per_page

PARSERS = {
    "kwork": KworkAPIParser,
    "freelance_ru": FreelanceRuParser,
    "hh_ru": HHParser,
}


async def verify_platform(platform_name: str, parser_class):
    console.print(f"\n[cyan]Тестируем парсер {platform_name}...[/cyan]")
    parser = parser_class()
    try:
        projects = await parser.get_projects(page=1, per_page=PER_PAGE, filters={"query": "python"})

        table = Table(title=f"Проекты с {platform_name}")
        table.add_column("Title", style="magenta", max_width=40)
        table.add_column("Budget", justify="right", style="green")
        table.add_column("Currency", style="green")
        table.add_column("Skills", style="blue")

        for project in projects:
            title = project.title[:37] + "..." if len(project.title) > 40 else project.title
            skills = ", ".join(project.skills)
            skills = skills[:30] + "..." if len(skills) > 30 else skills
            table.add_row(title, str(project.budget), project.currency, skills)

        console.print(table)
        console.print(f"[bold green][+] {platform_name}: найдено {len(projects)} проектов[/bold green]")
    except Exception as e:
        console.print(f"[bold red]x {platform_name}: ошибка {e}[/bold red]")
    finally:
        parser.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", type=str, help="Платформа для тестирования")
    parser.add_argument("--all", action="store_true", help="Тестировать все")
    args = parser.parse_args()

    ensure_layout()

    if args.platform:
        if args.platform in PARSERS:
            await verify_platform(args.platform, PARSERS[args.platform])
        else:
            console.print(f"[bold red]Неизвестная платформа: {args.platform}[/bold red]")
    elif args.all:
        for name, parser_class in PARSERS.items():
            await verify_platform(name, parser_class)
    else:
        console.print("Укажите --platform или --all")


if __name__ == "__main__":
    asyncio.run(main())
