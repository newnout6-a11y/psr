"""
Скрипт для парсинга всех фриланс-платформ.
Только сбор данных, без отправки откликов.
"""

import asyncio
import json
import os
from datetime import datetime
from typing import List, Dict, Any
from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

# Импортируем все парсеры
from src.parsers import (
    HHParser,
    RemoteOKParser,
    FreelancerComParser,
    FLRuParser,
    KworkParser,
    KworkAPIParser,
    FreelanceRuParser,
    PeoplePerHourParser,
    UpworkParser,
    WeblancerParser,
    OneCLancerParser,
    FiverrParser,
)
from src.parsers.base_parser import ProjectItem

console = Console()


class SimpleParser:
    """Простой парсер без отправки откликов."""

    def __init__(self):
        self.parsers = []
        self.results: Dict[str, List[ProjectItem]] = {}
        self._init_parsers()

    def _init_parsers(self):
        """Инициализация всех доступных парсеров."""
        parser_map = {
            "kwork": KworkAPIParser,  # Используем API версию
            "fl_ru": FLRuParser,
            "upwork": UpworkParser,
            "freelancer_com": FreelancerComParser,
            "hh_ru": HHParser,
            "remoteok": RemoteOKParser,
            "weblancer": WeblancerParser,
            "oneclancer": OneCLancerParser,
            "fiverr": FiverrParser,
            "freelance_ru": FreelanceRuParser,
            "peopleperhour": PeoplePerHourParser,
        }

        for name, parser_class in parser_map.items():
            try:
                self.parsers.append((name, parser_class()))
                logger.info(f"Инициализирован парсер: {name}")
            except Exception as e:
                logger.warning(f"Не удалось инициализировать {name}: {e}")

    async def parse_all(self, pages: int = 3, query: str = "python") -> Dict[str, List[ProjectItem]]:
        """Парсинг всех платформ."""
        console.print(f"\n[bold cyan]🚀 Начинаю парсинг {len(self.parsers)} платформ...[/bold cyan]")
        console.print(f"[dim]Запрос: '{query}', страниц: {pages}[/dim]\n")

        filters = {"query": query}
        all_results = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            for platform_name, parser in self.parsers:
                task = progress.add_task(f"Парсинг {platform_name}...", total=None)

                try:
                    projects = await self._parse_platform(parser, platform_name, pages, filters)
                    all_results[platform_name] = projects
                    progress.update(task, description=f"[green]✓ {platform_name}: {len(projects)} проектов[/green]")
                except Exception as e:
                    logger.error(f"Ошибка парсинга {platform_name}: {e}")
                    progress.update(task, description=f"[red]✗ {platform_name}: ошибка[/red]")

                await asyncio.sleep(1)  # Задержка между платформами

        return all_results

    async def _parse_platform(self, parser, platform_name: str, pages: int, filters: dict) -> List[ProjectItem]:
        """Парсинг одной платформы (несколько страниц)."""
        all_projects = []

        for page in range(1, pages + 1):
            try:
                projects = await parser.get_projects(page=page, per_page=20, filters=filters)
                if projects:
                    all_projects.extend(projects)
                else:
                    break  # Нет больше данных

                await asyncio.sleep(0.5)  # Rate limiting
            except Exception as e:
                logger.debug(f"Ошибка страницы {page} для {platform_name}: {e}")
                break

        return all_projects

    def save_results(self, results: Dict[str, List[ProjectItem]], output_dir: str = "data/parsing_results"):
        """Сохранение результатов в JSON и CSV."""
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # JSON формат
        json_path = os.path.join(output_dir, f"all_projects_{timestamp}.json")

        # Конвертируем в словари
        data = {}
        for platform, projects in results.items():
            data[platform] = [
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

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # CSV формат (все проекты в один файл)
        csv_path = os.path.join(output_dir, f"all_projects_{timestamp}.csv")
        import csv

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["platform", "id", "title", "budget", "currency", "skills", "url", "description"])

            for platform, projects in results.items():
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

        # Текстовый отчет
        report_path = os.path.join(output_dir, f"report_{timestamp}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"Отчет парсинга фриланс-платформ\n")
            f.write(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Запрос: {os.getenv('SEARCH_QUERY', 'python')}\n")
            f.write("=" * 50 + "\n\n")

            total = 0
            for platform, projects in results.items():
                count = len(projects)
                total += count
                f.write(f"{platform}: {count} проектов\n")
                for p in projects[:5]:  # Топ 5 для каждой платформы
                    f.write(f"  - {p.title[:60]}... ({p.budget} {p.currency})\n")
                f.write("\n")

            f.write(f"\nВСЕГО: {total} проектов\n")

        return json_path, csv_path, report_path

    def print_summary(self, results: Dict[str, List[ProjectItem]]):
        """Вывод красивой таблицы с результатами."""
        console.print("\n[bold cyan]📊 РЕЗУЛЬТАТЫ ПАРСИНГА[/bold cyan]\n")

        table = Table(title=f"Найдено проектов: {sum(len(p) for p in results.values())}")
        table.add_column("Платформа", style="magenta", no_wrap=True)
        table.add_column("Проектов", justify="right", style="green")
        table.add_column("Средний бюджет", style="yellow")
        table.add_column("Пример", style="dim", max_width=50)

        for platform, projects in sorted(results.items(), key=lambda x: len(x[1]), reverse=True):
            if projects:
                budgets = [p.budget for p in projects if p.budget]
                avg_budget = sum(budgets) / len(budgets) if budgets else 0
                sample = projects[0].title[:40] + "..." if len(projects[0].title) > 40 else projects[0].title

                table.add_row(
                    platform,
                    str(len(projects)),
                    f"{avg_budget:,.0f} {projects[0].currency}" if avg_budget else "N/A",
                    sample,
                )
            else:
                table.add_row(platform, "0", "-", "-")

        console.print(table)


async def main():
    load_dotenv()

    # Настройка логирования
    logger.add("data/parsing_logs.txt", mode="w", encoding="utf-8")

    # Параметры парсинга
    pages_to_parse = int(os.getenv("PAGES_TO_PARSE", "3"))
    search_query = os.getenv("SEARCH_QUERY", "python")

    # Создаем парсер
    parser = SimpleParser()

    # Запускаем парсинг
    results = await parser.parse_all(pages=pages_to_parse, query=search_query)

    # Выводим сводку
    parser.print_summary(results)

    # Сохраняем результаты
    json_path, csv_path, report_path = parser.save_results(results)

    console.print(f"\n[green]✅ Результаты сохранены:[/green]")
    console.print(f"  📄 JSON: {json_path}")
    console.print(f"  📊 CSV: {csv_path}")
    console.print(f"  📝 Отчет: {report_path}")

    # Закрываем парсеры
    for _, parser_instance in parser.parsers:
        try:
            parser_instance.close()
        except:
            pass

    console.print("\n[bold green]🎉 Парсинг завершен![/bold green]")


if __name__ == "__main__":
    asyncio.run(main())
