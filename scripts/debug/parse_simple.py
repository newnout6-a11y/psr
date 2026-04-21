"""
Упрощенный парсинг активных фриланс-платформ.
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

from src.paths import PARSING_RESULTS_DIR, ensure_layout
from src.filter.project_filter import ProjectFilter
from src.parsers import FreelanceRuParser, HHParser, KworkAPIParser
from src.parsers.base_parser import ProjectItem


class SimpleAPIParser:
    """Простой API-парсер без браузерной отправки."""

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
                print(f"[OK] {name} initialized")
            except Exception as e:
                print(f"[ERR] {name}: {e}")

    async def parse_all(self, pages: int = 2, query: str = "python") -> Dict[str, List[ProjectItem]]:
        print(f"\n>>> Parsing {len(self.parsers)} platforms...")
        print(f">>> Query: '{query}', Pages: {pages}, Per page: {self.per_page}\n")

        filters = {"query": query}
        all_results = {}

        for platform_name, parser in self.parsers:
            try:
                projects = await self._parse_platform(parser, platform_name, pages, filters)
                all_results[platform_name] = projects
                print(f"[OK] {platform_name}: {len(projects)} projects")
            except Exception as e:
                print(f"[ERR] {platform_name}: {str(e)[:50]}")
            await asyncio.sleep(0.5)

        return all_results

    async def _parse_platform(self, parser, name: str, pages: int, filters: dict) -> List[ProjectItem]:
        all_projects = []
        for page in range(1, pages + 1):
            try:
                projects = await asyncio.wait_for(
                    parser.get_projects(page=page, per_page=self.per_page, filters=filters),
                    timeout=30.0,
                )
                if not projects:
                    break
                all_projects.extend(projects)
            except asyncio.TimeoutError:
                print(f"  [WARN] {name} page {page}: timeout")
                break
            except Exception as e:
                print(f"  [ERR] {name} page {page}: {str(e)[:40]}")
                break
            await asyncio.sleep(0.3)
        return all_projects

    def save_results(self, results: Dict[str, List[ProjectItem]], output_dir: str = str(PARSING_RESULTS_DIR)):
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = os.path.join(output_dir, f"projects_{timestamp}.json")
        data = {
            platform: [
                {
                    "id": p.id,
                    "title": p.title,
                    "description": p.description[:150] + "..." if len(p.description) > 150 else p.description,
                    "budget": p.budget,
                    "currency": p.currency,
                    "skills": p.skills,
                    "url": p.url,
                    "platform": p.platform,
                }
                for p in projects
            ]
            for platform, projects in results.items()
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        csv_path = os.path.join(output_dir, f"projects_{timestamp}.csv")
        import csv

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["platform", "id", "title", "budget", "currency", "skills", "url"])
            for projects in results.values():
                for p in projects:
                    writer.writerow(
                        [
                            p.platform,
                            p.id,
                            p.title.replace("\n", " ")[:80],
                            p.budget,
                            p.currency,
                            ", ".join(p.skills[:5]),
                            p.url,
                        ]
                    )

        report_path = os.path.join(output_dir, f"report_{timestamp}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"ОТЧЕТ ПАРСИНГА\n{'=' * 50}\n")
            f.write(f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Запрос: {os.getenv('SEARCH_QUERY', 'python')}\n\n")

            total = 0
            for platform, projects in sorted(results.items(), key=lambda x: len(x[1]), reverse=True):
                count = len(projects)
                total += count
                f.write(f"\n[{platform}] {count} проектов\n")
                for i, p in enumerate(projects[:3], 1):
                    budget_str = f"{p.budget} {p.currency}" if p.budget else "не указан"
                    f.write(f"  {i}. {p.title[:60]}... ({budget_str})\n")
                    f.write(f"     -> {p.url}\n")
            f.write(f"\n{'=' * 50}\nВСЕГО: {total} проектов\n")

        return json_path, csv_path, report_path

    def print_summary(self, results: Dict[str, List[ProjectItem]]):
        print("\n" + "=" * 60)
        print("СВОДКА РЕЗУЛЬТАТОВ")
        print("=" * 60)

        total = 0
        for platform, projects in sorted(results.items(), key=lambda x: len(x[1]), reverse=True):
            count = len(projects)
            total += count
            if projects:
                budgets = [p.budget for p in projects if p.budget]
                avg = sum(budgets) / len(budgets) if budgets else 0
                sample = projects[0].title[:40] + "..." if len(projects[0].title) > 40 else projects[0].title
                currency = projects[0].currency if projects else ""
                print(f"{platform:15} | {count:4} | {avg:8.0f} {currency:3} | {sample}")
            else:
                print(f"{platform:15} | {count:4} | - | -")

        print("=" * 60)
        print(f"ВСЕГО: {total} проектов")
        print("=" * 60)


async def main():
    load_dotenv()
    ensure_layout()

    pages = int(os.getenv("PAGES_TO_PARSE", "2"))
    query = os.getenv("SEARCH_QUERY", "python")

    parser = SimpleAPIParser()
    if not parser.parsers:
        print("[ERR] Не удалось инициализировать парсеры")
        return

    results = await parser.parse_all(pages=pages, query=query)
    parser.print_summary(results)

    if any(results.values()):
        json_path, csv_path, report_path = parser.save_results(results)
        print("\n[OK] Результаты сохранены:")
        print(f"     JSON: {json_path}")
        print(f"     CSV: {csv_path}")
        print(f"     Отчет: {report_path}")
    else:
        print("\n[WARN] Ничего не найдено")

    for _, parser_instance in parser.parsers:
        try:
            parser_instance.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
