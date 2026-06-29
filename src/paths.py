"""Единая карта путей проекта.

Держим все часто используемые пути в одном месте, чтобы структура проекта
оставалась прозрачной и не было разбросанных строковых `data/...` по коду.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(os.getenv("PSR_ROOT") or Path(__file__).resolve().parents[1])
DATA_DIR = Path(os.getenv("PSR_DATA_DIR") or ROOT_DIR / "data")

if os.getenv("PSR_DATA_DIR"):
    _existing_data = DATA_DIR / "runtime"
    if _existing_data.exists() and not any(_existing_data.iterdir()):
        pass
    elif not DATA_DIR.exists():
        import warnings

        warnings.warn(
            f"PSR_DATA_DIR={DATA_DIR} не существует — будет создана пустая директория. "
            "Если это ошибка, проверьте переменную окружения.",
            stacklevel=2,
        )

REFERENCE_DIR = Path(os.getenv("PSR_REFERENCE_DIR") or DATA_DIR / "reference")
RUNTIME_DIR = DATA_DIR / "runtime"
DEBUG_DIR = DATA_DIR / "debug"

SCREENSHOTS_DIR = RUNTIME_DIR / "screenshots"
BROWSER_PROFILES_DIR = RUNTIME_DIR / "browser_profiles"
PARSING_RESULTS_DIR = RUNTIME_DIR / "parsing_results"
PROPOSAL_ASSETS_DIR = RUNTIME_DIR / "proposal_assets"

PORTFOLIO_FILE = REFERENCE_DIR / "portfolio.json"
CASES_FILE = REFERENCE_DIR / "cases.json"
CASES_FAISS_FILE = REFERENCE_DIR / "cases.faiss"
CASES_MAPPING_FILE = REFERENCE_DIR / "cases.mapping"
WMN_SITES_FILE = REFERENCE_DIR / "wmn_sites.json"

LOGS_DB_FILE = RUNTIME_DIR / "logs.db"
PROPOSALS_DB_FILE = RUNTIME_DIR / "proposals.db"
OSINT_CACHE_DB_FILE = RUNTIME_DIR / "osint_cache.db"
LOGS_TXT_FILE = RUNTIME_DIR / "logs.txt"
PARSING_LOGS_FILE = RUNTIME_DIR / "parsing_logs.txt"
GENERATED_PROPOSALS_FILE = RUNTIME_DIR / "generated_proposals.txt"
LAST_LLM_PROMPT_FILE = RUNTIME_DIR / "last_llm_prompt.txt"
LAST_LLM_RESPONSE_FILE = RUNTIME_DIR / "last_llm_response.txt"

FORM_STRUCTURE_FILE = DEBUG_DIR / "form_structure.txt"
PAGE_STRUCTURE_FILE = DEBUG_DIR / "page_structure.txt"
PAGE_DUMP_FILE = DEBUG_DIR / "page_dump.html"
KWORK_DEBUG_HTML_FILE = DEBUG_DIR / "kwork_debug.html"
KWORK_DEBUG_PNG_FILE = DEBUG_DIR / "kwork_debug.png"
KWORK_FULL_PAGE_PNG_FILE = DEBUG_DIR / "kwork_full_page.png"
KWORK_OFFER_FORM_PNG_FILE = DEBUG_DIR / "kwork_offer_form.png"
KWORK_PROJECT_CHECK_PNG_FILE = DEBUG_DIR / "kwork_project_check.png"


def ensure_layout() -> None:
    """Создать основные каталоги проекта, если их ещё нет."""
    for path in (
        DATA_DIR,
        REFERENCE_DIR,
        RUNTIME_DIR,
        DEBUG_DIR,
        SCREENSHOTS_DIR,
        BROWSER_PROFILES_DIR,
        PARSING_RESULTS_DIR,
        PROPOSAL_ASSETS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def ensure_parent(path: Path) -> Path:
    """Создать parent-каталог для файла и вернуть исходный путь."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
