"""
Скрипт для парсинга активных платформ без отправки откликов.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from src.paths import PARSING_LOGS_FILE, PARSING_RESULTS_DIR, ensure_layout
from src.filter.project_filter import ProjectFilter
from src.parsers import FreelanceRuParser, HHParser, KworkAPIParser
from src.parsers.base_parser import ProjectItem


console = Console()


class SimpleParser:
    """Простой парсер без отправки откликов."""

    def __init__(self):
        self.parsers = []
        self.results: Dict[str, List[ProjectItem]] = {}
        self.per_page = ProjectFilter().per_page
        self._init_parsers()

    def _init_parsers(self):
        parser_map = {
            "kwork": KworkAPIParser,
            "freelance_ru": FreelanceRuParser,
            "hh_ru": HHParser,
        }

        for name, parser_class in parser_map.items():
            try:
                self.parsers.append((name, parser_class()))
                logger.info(f"Инициализирован парсер: {name}")
            except Exception as e:
                logger.warning(f"Не удалось инициализировать {name}: {e}")

    async def parse_all(self, pages: int = 3, query: str = "python") -> Dict[str, List[ProjectItem]]:
        console.print(f"\n[bold cyan]Начинаю парсинг {len(self.parsers)} платформ...[/bold cyan]")
        console.print(f"[dim]Запрос: '{query}', страниц: {pages}, per_page: {self.per_page}[/dim]\n")

        filters = {"query": query}
        all_results = {}

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            for platform_name, parser in self.parsers:
                task = progress.add_task(f"Парсинг {platform_name}...", total=None)
                try:
                    projects = await self._parse_platform(parser, pages, filters)
                    all_results[platform_name] = projects
                    progress.update(task, description=f"[green]✓ {platform_name}: {len(projects)} проектов[/green]")
                except Exception as e:
                    logger.error(f"Ошибка парсинга {platform_name}: {e}")
                    progress.update(task, description=f"[red]✗ {platform_name}: ошибка[/red]")
                await asyncio.sleep(1)

        return all_results

    async def _parse_platform(self, parser, pages: int, filters: dict) -> List[ProjectItem]:
        all_projects = []
        for page in range(1, pages + 1):
            projects = await parser.get_projects(page=page, per_page=self.per_page, filters=filters)
            if not projects:
                break
            all_projects.extend(projects)
            await asyncio.sleep(0.5)
        return all_projects

    def save_results(self, results: Dict[str, List[ProjectItem]], output_dir: str = str(PARSING_RESULTS_DIR)):
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = os.path.join(output_dir, f"all_projects_{timestamp}.json")
        data = {
            platform: [
                {
                    "id": p.id,
                    "title": p.title,
                    "description": p.description[:200] + "..." if len(p.description) > 200 else p.description,
                    "budget": p.budget,
                    "currency": p.currency,
                    "skills": p.skills,
                    "url": p.url,
                    "platform": p.platform,
                    "created_at": p.created_at,
                }
                for p in projects
            ]
            for platform, projects in results.items()
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        csv_path = os.path.join(output_dir, f"all_projects_{timestamp}.csv")
        import csv

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["platform", "id", "title", "budget", "currency", "skills", "url", "description"])
            for projects in results.values():
                for p in projects:
                    writer.writerow(
                        [
                            p.platform,
                            p.id,
                            p.title.replace("\n", " ").replace("\r", ""),
                            p.budget,
                            p.currency,
                            ", ".join(p.skills),
                            p.url,
                            p.description.replace("\n", " ").replace("\r", "")[:150],
                        ]
                    )

        report_path = os.path.join(output_dir, f"report_{timestamp}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("Отчет парсинга активных платформ\n")
            f.write(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Запрос: {os.getenv('SEARCH_QUERY', 'python')}\n")
            f.write("=" * 50 + "\n\n")

            total = 0
            for platform, projects in results.items():
                count = len(projects)
                total += count
                f.write(f"{platform}: {count} проектов\n")
                for p in projects[:5]:
                    f.write(f"  - {p.title[:60]}... ({p.budget} {p.currency})\n")
                f.write("\n")
            f.write(f"\nВСЕГО: {total} проектов\n")

        return json_path, csv_path, report_path

    def print_summary(self, results: Dict[str, List[ProjectItem]]):
        console.print("\n[bold cyan]РЕЗУЛЬТАТЫ ПАРСИНГА[/bold cyan]\n")
        table = Table(title=f"Найдено проектов: {sum(len(p) for p in results.values())}")
        table.add_column("Платформа", style="magenta", no_wrap=True)
        table.add_column("Проектов", justify="right", style="green")
        table.add_column("Средний бюджет", style="yellow")
        table.add_column("Пример", style="dim", max_width=50)

        for platform, projects in sorted(results.items(), key=lambda x: len(x[1]), reverse=True):
            if not projects:
                table.add_row(platform, "0", "-", "-")
                continue

            budgets = [p.budget for p in projects if p.budget]
            avg_budget = sum(budgets) / len(budgets) if budgets else 0
            sample = projects[0].title[:40] + "..." if len(projects[0].title) > 40 else projects[0].title
            table.add_row(
                platform,
                str(len(projects)),
                f"{avg_budget:,.0f} {projects[0].currency}" if avg_budget else "N/A",
                sample,
            )

        console.print(table)


async def main():
    load_dotenv()
    ensure_layout()
    logger.add(str(PARSING_LOGS_FILE), mode="w", encoding="utf-8")

    pages_to_parse = int(os.getenv("PAGES_TO_PARSE", "3"))
    search_query = os.getenv("SEARCH_QUERY", "python")

    parser = SimpleParser()
    results = await parser.parse_all(pages=pages_to_parse, query=search_query)
    parser.print_summary(results)
    json_path, csv_path, report_path = parser.save_results(results)

    console.print("\n[green]Результаты сохранены:[/green]")
    console.print(f"  JSON: {json_path}")
    console.print(f"  CSV: {csv_path}")
    console.print(f"  Отчет: {report_path}")

    for _, parser_instance in parser.parsers:
        try:
            parser_instance.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
