import asyncio

from src.filter.project_filter import ProjectFilter
from src.parsers.base_parser import ProjectItem


class _Converter:
    async def to_rub(self, amount, _currency):
        return amount


class _DB:
    def __init__(self, sent_urls=None):
        self.sent_urls = sent_urls or set()

    def get_sent_proposals(self, limit=1000):
        return [{"url": url} for url in self.sent_urls]


def _project(project_id: str, title: str, description: str, *, url: str | None = None):
    return ProjectItem(
        id=project_id,
        title=title,
        description=description,
        budget=1000,
        currency="RUB",
        skills=[],
        url=url or f"https://kwork.ru/projects/{project_id}",
        platform="kwork",
        created_at="",
        offers_count=0,
        client_hired_percent=0,
        search_query="автоматизация",
    )


def test_project_filter_keeps_automation_with_soft_stop_words(tmp_path):
    config = tmp_path / "filters.yaml"
    config.write_text(
        """
min_budget: 500
max_budget: 10000
max_age_hours: 72
max_proposals: 0
per_page: 20
required_skills:
  - скрипт
  - парсинг
  - crm
  - n8n
stop_words:
  - рерайт
  - менеджер
""",
        encoding="utf-8",
    )
    project_filter = ProjectFilter(config_path=str(config))
    project_filter.db = _DB(sent_urls={"https://kwork.ru/projects/sent"})
    project_filter._converter = _Converter()

    projects = [
        _project("n8n", "Скрипт на n8n", "Нужно автоматизировать рерайт и загрузку файлов"),
        _project("parse", "Парсинг сайта", "Нужно собрать данные по характеристикам"),
        _project("crm", "Доработка CRM", "Логика для менеджера продаж"),
        _project("miss", "Нарисовать логотип", "Нужен красивый знак"),
        _project("sent", "Скрипт", "Уже был draft", url="https://kwork.ru/projects/sent"),
    ]

    passed, decisions = asyncio.run(project_filter.filter_projects_with_reasons(projects))

    assert [project.id for project in passed] == ["n8n", "parse", "crm"]
    assert decisions[("kwork", "miss")].reason == "missing_required_skills"
    assert decisions[("kwork", "sent")].reason == "already_sent_or_draft"
