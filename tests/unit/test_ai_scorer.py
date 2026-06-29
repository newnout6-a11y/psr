import os
from unittest.mock import patch

from src.filter.ai_scorer import AIRelevanceScorer
from src.parsers.base_parser import ProjectItem


def _project(title: str, description: str) -> ProjectItem:
    return ProjectItem(
        id="design-1",
        title=title,
        description=description,
        budget=5000,
        currency="RUB",
        skills=[],
        url="https://kwork.ru/projects/design-1",
        platform="kwork",
        created_at="2026-06-09 12:00:00",
        offers_count=0,
        client_hired_percent=50,
    )


def test_design_is_allowed_when_search_brief_asks_for_design():
    scorer = AIRelevanceScorer()

    with patch.dict(os.environ, {"SEARCH_BRIEF": "фронтенд, дизайн сайта"}):
        result = scorer._heuristic_score(
            _project("Дизайн сайта в Figma", "Нужен UI дизайн сайта и адаптивная версия"),
            threshold=6,
        )

    assert result.project_type == "design"
    assert result.final_score >= 6
    assert not any("нерелевантные маркеры" in risk for risk in result.risks)


def test_design_is_penalized_when_search_brief_is_automation():
    scorer = AIRelevanceScorer()

    with patch.dict(os.environ, {"SEARCH_BRIEF": "автоматизация, скрипты"}):
        result = scorer._heuristic_score(
            _project("Дизайн сайта в Figma", "Нужен UI дизайн сайта и адаптивная версия"),
            threshold=6,
        )

    assert result.project_type == "design"
    assert result.final_score < 6
