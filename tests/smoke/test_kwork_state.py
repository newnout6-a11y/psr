"""Smoke tests for Kwork stateData parsing."""

from src.action.proposal_db import ProposalDB
from src.platforms.kwork import KworkStateDataParser


PROJECTS_HTML = """
<html><body>
<script>
window.stateData={
  "wants": [
    {
      "id": 3160848,
      "name": "Парсинг цен jd.com",
      "description": "Нужно собрать <b>150000</b> товаров",
      "priceLimit": "1000.00",
      "possiblePriceLimit": 3000,
      "date_create": "2026-04-25 10:36:02",
      "date_active": "2026-04-25 13:34:27",
      "date_expire": "2026-04-28 00:00:00",
      "category_id": "41",
      "kwork_count": 22,
      "views_dirty": "327",
      "availableDurations": [1, 2, 7, 10],
      "files": [{"name": "tz.pdf", "url": "/files/tz.pdf"}],
      "user": {
        "USERID": 16270186,
        "username": "irindra",
        "badges": [{"id": 25}],
        "data": {"wants_count": "3", "wants_hired_percent": "43"}
      }
    }
  ]
};window.nextChunk=true;
</script>
</body></html>
"""


DETAIL_HTML = """
<html><body>
<script>
window.stateData={
  "wantData": {
    "id": 3160293,
    "name": "Парсер цен Wildberries на n8n",
    "description": "Нужна система мониторинга цен",
    "priceLimit": "5000.00",
    "date_create": "2026-04-25 11:00:00",
    "category_id": "41",
    "kwork_count": 4,
    "user": {
      "USERID": 42,
      "username": "buyer",
      "data": {"wants_hired_percent": "57"}
    }
  }
};window.footer={};
</script>
</body></html>
"""


def test_kwork_state_projects_from_html():
    projects = KworkStateDataParser.projects_from_html(PROJECTS_HTML)
    assert len(projects) == 1

    project = projects[0]
    assert project.id == "3160848"
    assert project.title == "Парсинг цен jd.com"
    assert project.description == "Нужно собрать 150000 товаров"
    assert project.budget == 1000.0
    assert project.offers_count == 22
    assert project.client_user_id == "16270186"
    assert project.client_hired_percent == 43
    assert project.platform_data["category_id"] == "41"
    assert project.platform_data["files"][0]["name"] == "tz.pdf"
    assert project.platform_data["user"]["username"] == "irindra"


def test_kwork_state_project_detail_from_html():
    project = KworkStateDataParser.project_from_html(DETAIL_HTML, project_id="3160293")
    assert project is not None
    assert project.id == "3160293"
    assert project.title == "Парсер цен Wildberries на n8n"
    assert project.offers_count == 4
    assert project.client_hired_percent == 57
    assert project.url.endswith("/projects/3160293")


def test_candidate_persists_platform_data(tmp_path):
    db = ProposalDB(db_path=str(tmp_path / "psr.db"))
    project = KworkStateDataParser.projects_from_html(PROJECTS_HTML)[0]

    candidate_id = db.upsert_candidate(project, stage="parsed", status="parsed")
    candidate = db.get_candidate(candidate_id)

    assert candidate["platform_data"]["source"] == "kwork_state_data"
    assert candidate["platform_data"]["views_dirty"] == "327"
    assert candidate["platform_data"]["user"]["data"]["wants_count"] == "3"
