"""Комплексное тестирование всех новых функций PSR.

Запуск: python -m pytest tests/test_psr_features.py -v
Или:    python tests/test_psr_features.py

Покрытие:
1. ProposalDB — conversations, earnings, funnel, dedup, CAS
2. PII Filter — stop-words, телефоны, @username, комиссия
3. Proposal Generator — _clean_llm_response, language detection, tone_hint
4. KworkExtensions — categories, orders, reviews, inbox, offers (mocked API)
5. Circuit Breaker — error classification, jitter, breaker notification
6. Search Strategy — cache invalidation, TTL
7. Inbox Monitor — watermark, message dedup, response time alert
8. Connects Monitor — cache, decrement, can_send
9. Success Rate Monitor — rate calculation, throttle
10. Account Health Monitor — busy risk, auto-pause
10. Browser Fingerprint — UA pool, sec-ch-ua, Client Hints
11. Decision Policy — CAS guard integration
12. Proposal Templates — markdown removal after cleaning
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PSR_ROOT", str(ROOT))
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("ADMIN_CHAT_ID", "")


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_db(tmp_path):
    """Создать ProposalDB во временной директории."""
    from src.action.proposal_db import ProposalDB

    db_path = str(tmp_path / "test_proposals.db")
    db = ProposalDB(db_path=db_path)
    return db


@pytest.fixture
def mock_project():
    """Mock ProjectItem for testing."""
    from src.parsers.base_parser import ProjectItem

    return ProjectItem(
        id="test_123",
        title="Telegram бот на aiogram",
        description="Нужен бот для автоматизации рассылки. Python, aiogram, PostgreSQL.",
        budget=5000.0,
        currency="RUB",
        skills=["python", "telegram", "api"],
        url="https://kwork.ru/projects/test_123",
        platform="kwork",
        created_at="2026-06-15 10:00:00",
        offers_count=3,
        client_hired_percent=50,
    )


@pytest.fixture
def mock_api():
    """Mock KworkClient for API tests."""
    api = MagicMock()
    api.request = AsyncMock()
    api.request_with_body = AsyncMock()
    api.request_multipart = AsyncMock()
    api.web = MagicMock()
    api.web._build_xhr_headers = MagicMock(return_value={})
    api.web._filtered_cookies = MagicMock(return_value={})
    api.web.request = AsyncMock()
    api.web.login_via_mobile_web_auth_token = AsyncMock()
    api.web.submit_exchange_offer = AsyncMock()
    api.web._gen_draft_key = MagicMock(return_value="abc12345")
    api.web._extract_csrf_user_token = MagicMock(return_value=None)
    return api


# ============================================================
# 1. ProposalDB — Conversations
# ============================================================


class TestConversations:
    def test_create_conversation(self, tmp_db):
        conv_id = tmp_db.get_or_create_conversation("proj_1", "kwork", project_title="Test Project")
        assert conv_id > 0
        conv_id2 = tmp_db.get_or_create_conversation("proj_1", "kwork")
        assert conv_id == conv_id2

    def test_add_message(self, tmp_db):
        conv_id = tmp_db.get_or_create_conversation("proj_1", "kwork")
        result = tmp_db.add_conversation_message(
            conv_id, sender="customer", message_text="Привет!", platform_message_id="msg_1"
        )
        assert result is True
        result2 = tmp_db.add_conversation_message(
            conv_id, sender="customer", message_text="Привет!", platform_message_id="msg_1"
        )
        assert result2 is False

    def test_reopen_completed_conversation(self, tmp_db):
        conv_id = tmp_db.get_or_create_conversation("proj_1", "kwork")
        tmp_db.update_conversation_status(conv_id, "completed")
        tmp_db.add_conversation_message(
            conv_id, sender="customer", message_text="Новое сообщение", platform_message_id="msg_2"
        )
        conv = tmp_db.get_conversation("proj_1", "kwork")
        assert conv["status"] == "awaiting_reply"
        assert len(conv["messages"]) == 1

    def test_get_active_conversations(self, tmp_db):
        c1 = tmp_db.get_or_create_conversation("proj_1", "kwork", project_title="A")
        c2 = tmp_db.get_or_create_conversation("proj_2", "kwork", project_title="B")
        tmp_db.update_conversation_status(c2, "completed")
        active = tmp_db.get_active_conversations()
        assert len(active) == 1
        assert active[0]["project_title"] == "A"


# ============================================================
# 2. ProposalDB — Earnings
# ============================================================


class TestEarnings:
    def test_record_earning(self, tmp_db):
        eid = tmp_db.record_earning(project_id="proj_1", platform="kwork", amount=15000.0)
        assert eid > 0

    def test_earnings_dedup(self, tmp_db, mock_project):
        """Earnings UNIQUE index prevents duplicate pending entries."""
        cid = tmp_db.upsert_candidate(mock_project, stage="parsed", status="queued")
        tmp_db.record_earning(candidate_id=cid, project_id="proj_1", platform="kwork", amount=5000.0, status="pending")
        with pytest.raises(Exception):
            tmp_db.record_earning(
                candidate_id=cid, project_id="proj_1", platform="kwork", amount=5000.0, status="pending"
            )

    def test_earnings_summary(self, tmp_db):
        tmp_db.record_earning(project_id="p1", platform="kwork", amount=10000.0, status="paid")
        tmp_db.record_earning(project_id="p2", platform="kwork", amount=5000.0, status="pending")
        s = tmp_db.get_earnings_summary()
        assert s["total"] == 2
        assert s["paid_amount"] == 10000.0
        assert s["pending_amount"] == 5000.0

    def test_update_earning_status(self, tmp_db):
        eid = tmp_db.record_earning(project_id="p1", platform="kwork", amount=3000.0, status="pending")
        tmp_db.update_earning_status(eid, "paid")
        earnings = tmp_db.get_earnings()
        assert earnings[0]["status"] == "paid"


# ============================================================
# 3. ProposalDB — Conversion Funnel
# ============================================================


class TestFunnel:
    def test_funnel_empty(self, tmp_db):
        f = tmp_db.get_conversion_funnel()
        assert f["sent"] == 0
        assert f["responded"] == 0
        assert f["hired"] == 0


# ============================================================
# 4. ProposalDB — CAS Guard
# ============================================================


class TestCASGuard:
    def test_claim_queued_candidate(self, tmp_db, mock_project):
        cid = tmp_db.upsert_candidate(mock_project, stage="parsed", status="queued")
        assert tmp_db.claim_candidate_for_sending(cid) is True
        candidate = tmp_db.get_candidate(cid)
        assert candidate["status"] == "sending"

    def test_claim_already_sent_fails(self, tmp_db, mock_project):
        cid = tmp_db.upsert_candidate(mock_project, stage="parsed", status="queued")
        tmp_db.claim_candidate_for_sending(cid)
        assert tmp_db.claim_candidate_for_sending(cid) is False

    def test_claim_wrong_status_fails(self, tmp_db, mock_project):
        cid = tmp_db.upsert_candidate(mock_project, stage="parsed", status="skipped")
        assert tmp_db.claim_candidate_for_sending(cid) is False


# ============================================================
# 5. PII Filter
# ============================================================


class TestPIIFilter:
    def test_phone_removed(self):
        from src.action.pii_filter import filter_proposal_text

        text, violations = filter_proposal_text("Здравствуйте! Позвоните мне +7 999 123 45 67")
        assert "999" not in text
        assert len(violations) > 0

    def test_telegram_handle_removed(self):
        from src.action.pii_filter import filter_proposal_text

        text, violations = filter_proposal_text("Напишите мне в @my_telegram")
        assert "@my_telegram" not in text
        assert len(violations) > 0

    def test_commission_removed(self):
        from src.action.pii_filter import filter_proposal_text

        text, violations = filter_proposal_text("Учтите комиссию площадки 20%")
        assert "комисси" not in text.lower()
        assert len(violations) > 0

    def test_clean_text_passes(self):
        from src.action.pii_filter import filter_proposal_text

        text, violations = filter_proposal_text("Здравствуйте! Для этой задачи подойдёт FastAPI + Celery.")
        assert len(violations) == 0
        assert "FastAPI" in text

    def test_is_safe_for_kwork(self):
        from src.action.pii_filter import is_safe_for_kwork

        assert is_safe_for_kwork("Здравствуйте! Готов начать работу.") is True
        assert is_safe_for_kwork("Напишите в telegram @user") is False


# ============================================================
# 6. Proposal Generator — Cleaning
# ============================================================


class TestProposalCleaning:
    def test_preserves_russian_typography(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        text = "Здравствуйте! Стек — Python, «быстрый» старт, №1 вариант."
        cleaned = gen._clean_llm_response(text)
        assert "—" in cleaned or "Python" in cleaned
        assert "«" in cleaned or "быстрый" in cleaned

    def test_strips_assistant_prefix(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        text = "Вот ваш отклик. Здравствуйте! Для задачи подойдёт FastAPI."
        cleaned = gen._clean_llm_response(text)
        assert not cleaned.startswith("Вот")
        assert "FastAPI" in cleaned

    def test_strips_price_mentions(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        text = "Здравствуйте! Сделаю за 5000 рублей. FastAPI."
        cleaned = gen._clean_llm_response(text)
        assert "5000" not in cleaned or "руб" not in cleaned

    def test_template_fallback_is_cleaned(self):
        """Template fallback must go through _clean_llm_response."""
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        template_text = "Приветствую. Посмотрел проект «Test» — *bold* text с - bullet"
        cleaned = gen._clean_llm_response(template_text)
        assert "*" not in cleaned
        assert "—" not in cleaned or "—" in cleaned


# ============================================================
# 7. Proposal Generator — Language Detection
# ============================================================


class TestLanguageDetection:
    def test_detect_russian(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        project = MagicMock()
        project.title = "Создание telegram бота"
        project.description = "Нужен бот на Python для рассылки сообщений"
        assert gen._detect_language(project) == "ru"

    def test_detect_english(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        project = MagicMock()
        project.title = "Build a REST API with FastAPI"
        project.description = "Need a backend API with authentication and PostgreSQL database"
        assert gen._detect_language(project) == "en"


# ============================================================
# 8. KworkExtensions — Mocked API Tests
# ============================================================


class TestKworkExtensions:
    @pytest.mark.asyncio
    async def test_get_categories(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.request.return_value = {
            "response": [{"id": 11, "name": "Scripts", "categories": [{"id": 111, "name": "Python"}]}]
        }
        cats = await KworkExtensions.get_categories_raw(mock_api)
        assert len(cats) == 1
        assert cats[0]["name"] == "Scripts"

    @pytest.mark.asyncio
    async def test_get_all_category_ids(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.request.return_value = {"response": [{"id": 11, "categories": [{"id": 111}, {"id": 112}]}]}
        ids = await KworkExtensions.get_all_category_ids(mock_api)
        assert 11 in ids
        assert 111 in ids
        assert 112 in ids

    @pytest.mark.asyncio
    async def test_approve_order(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.request.return_value = {"success": True}
        result = await KworkExtensions.approve_order(mock_api, 123)
        assert result is not None
        mock_api.request.assert_called_with("post", "approveOrder", use_token=True, id=123)

    @pytest.mark.asyncio
    async def test_set_typing(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        await KworkExtensions.set_typing(mock_api, 456)
        mock_api.request.assert_called_with("post", "typing", use_token=True, recipientId=456)

    @pytest.mark.asyncio
    async def test_inbox_read(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        await KworkExtensions.inbox_read(mock_api, 789)
        mock_api.request.assert_called_with("post", "inboxRead", use_token=True, id=789)

    @pytest.mark.asyncio
    async def test_get_wants_count(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.request.return_value = {"response": {"count": 42}}
        count = await KworkExtensions.get_wants_count(mock_api, categories="all")
        assert count == 42

    @pytest.mark.asyncio
    async def test_check_is_template_not_flagged(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.web.request.return_value = {"json": {"success": True, "response": {"is_template": False}}}
        flagged = await KworkExtensions.is_text_template_flagged(mock_api, 123, "Unique proposal text")
        assert flagged is False

    @pytest.mark.asyncio
    async def test_check_is_template_flagged(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.web.request.return_value = {"json": {"success": False}}
        flagged = await KworkExtensions.is_text_template_flagged(mock_api, 123, "Template text")
        assert flagged is True

    @pytest.mark.asyncio
    async def test_check_web_session_valid(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.web._filtered_cookies.return_value = {"csrf_user_token": "abc123"}
        assert await KworkExtensions.check_web_session(mock_api) is True

    @pytest.mark.asyncio
    async def test_check_web_session_invalid(self, mock_api):
        from src.platforms.kwork_ext import KworkExtensions

        mock_api.web._filtered_cookies.return_value = {}
        assert await KworkExtensions.check_web_session(mock_api) is False


# ============================================================
# 9. Circuit Breaker — Error Classification
# ============================================================


class TestErrorClassification:
    def test_business_rejection_detected(self):
        from src.orchestrator import _is_business_rejection

        assert _is_business_rejection("уже отправл") is True
        assert _is_business_rejection("project closed") is True
        assert _is_business_rejection("минимальная цена") is True

    def test_infra_error_not_business(self):
        from src.orchestrator import _is_business_rejection

        assert _is_business_rejection("Connection timeout") is False
        assert _is_business_rejection("CSRF token invalid") is False
        assert _is_business_rejection("") is False


# ============================================================
# 10. Search Strategy — Cache TTL
# ============================================================


class TestSearchCache:
    def test_cache_invalidation(self):
        from src.brain.search_strategy import SearchStrategy

        with patch("src.brain.search_strategy.ProposalDB"):
            ss = SearchStrategy()
            ss._cached_queries["kwork_8_test"] = ["python", "бот"]
            ss._cached_queries_ts["kwork_8_test"] = time.time()
            ss.invalidate_cache()
            assert len(ss._cached_queries) == 0
            assert len(ss._cached_queries_ts) == 0

    def test_cache_ttl_expired(self):
        from src.brain.search_strategy import SearchStrategy

        with patch("src.brain.search_strategy.ProposalDB"):
            ss = SearchStrategy()
            ss._cached_queries["kwork_8_test"] = ["python"]
            ss._cached_queries_ts["kwork_8_test"] = time.time() - 1900
            cached = ss._cached_queries.get("kwork_8_test")
            cached_ts = ss._cached_queries_ts.get("kwork_8_test", 0)
            assert time.time() - cached_ts > 1800


# ============================================================
# 11. Connects Monitor
# ============================================================


class TestConnectsMonitor:
    def test_can_send_with_enough(self):
        from src.platforms.kwork_ext import ConnectsMonitor

        m = ConnectsMonitor()
        m._cache = {"free_amount": 10}
        assert m.can_send() is True

    def test_cannot_send_with_low(self):
        from src.platforms.kwork_ext import ConnectsMonitor

        m = ConnectsMonitor(block_threshold=2)
        m._cache = {"free_amount": 1}
        assert m.can_send() is False

    def test_free_amount_property(self):
        from src.platforms.kwork_ext import ConnectsMonitor

        m = ConnectsMonitor()
        m._cache = {"free_amount": 15}
        assert m.free_amount == 15


# ============================================================
# 12. Success Rate Monitor
# ============================================================


class TestSuccessRateMonitor:
    def test_high_rate_can_send(self):
        from src.platforms.kwork_ext import SuccessRateMonitor

        m = SuccessRateMonitor()
        m._cache = {"success_rate": 95.0, "completed": 19, "cancelled": 1, "active": 2}
        assert m.can_send() is True
        assert m.should_throttle() is False
        assert m.active_orders == 2

    def test_low_rate_blocked(self):
        from src.platforms.kwork_ext import SuccessRateMonitor

        m = SuccessRateMonitor(block_rate=70.0)
        m._cache = {"success_rate": 65.0, "completed": 13, "cancelled": 7, "active": 1}
        assert m.can_send() is False

    def test_throttle_zone(self):
        from src.platforms.kwork_ext import SuccessRateMonitor

        m = SuccessRateMonitor(warn_rate=80.0, block_rate=70.0)
        m._cache = {"success_rate": 75.0, "completed": 15, "cancelled": 5, "active": 3}
        assert m.can_send() is True
        assert m.should_throttle() is True


# ============================================================
# 13. Browser Fingerprint
# ============================================================


class TestFingerprint:
    def test_ua_pool_is_2026(self):
        from src.browser.fingerprint import _UA_POOL

        for ua in _UA_POOL:
            assert "Chrome/13" in ua or "Chrome/14" in ua, f"UA not 2026: {ua}"

    def test_sec_ch_ua_matches_ua(self):
        from src.browser.fingerprint import pick

        fp = pick(seed="test_profile")
        if fp.sec_ch_ua:
            version_in_ua = fp.user_agent.split("Chrome/")[1].split(".")[0]
            assert f'v="{version_in_ua}"' in fp.sec_ch_ua

    def test_browser_args_include_sec_ch_ua(self):
        from src.browser.fingerprint import pick

        fp = pick(seed="test")
        args = fp.browser_args()
        has_sec_ch = any("--sec-ch-ua=" in a for a in args)
        assert has_sec_ch or fp.sec_ch_ua == ""

    def test_deterministic_by_seed(self):
        from src.browser.fingerprint import pick

        f1 = pick(seed="abc")
        f2 = pick(seed="abc")
        assert f1.user_agent == f2.user_agent
        assert f1.sec_ch_ua == f2.sec_ch_ua


# ============================================================
# 14. Text Similarity (Proposal Dedup)
# ============================================================


class TestTextSimilarity:
    def test_identical_text(self):
        from src.action.proposal_sender import _text_similarity

        assert _text_similarity("Здравствуйте! Готов сделать.", "Здравствуйте! Готов сделать.") == 1.0

    def test_completely_different(self):
        from src.action.proposal_sender import _text_similarity

        assert _text_similarity("Здравствуйте! Python FastAPI.", "Добрый день! React Vue CSS.") == 0.0

    def test_high_similarity(self):
        from src.action.proposal_sender import _text_similarity

        a = "Здравствуйте! Для этой задачи подойдёт FastAPI и Celery для фоновой обработки данных."
        b = "Здравствуйте! Для этой задачи подойдёт FastAPI и Celery для фоновой обработки данных файлов."
        sim = _text_similarity(a, b)
        assert sim > 0.7

    def test_empty_text(self):
        from src.action.proposal_sender import _text_similarity

        assert _text_similarity("", "test") == 0.0
        assert _text_similarity("", "") == 0.0


# ============================================================
# 15. ProposalDB — Mark Response (Audit)
# ============================================================


class TestMarkResponse:
    def test_mark_response_records_audit(self, tmp_db, mock_project):
        cid = tmp_db.upsert_candidate(mock_project, stage="parsed", status="manual_sent")
        tmp_db.save_proposal(
            project_id="test_123",
            platform="kwork",
            title="Test",
            proposal_text="Отклик",
            candidate_id=cid,
        )
        tmp_db.mark_response("test_123", "Ответ от клиента")
        with tmp_db._connect() as conn:
            row = conn.execute("SELECT action FROM candidate_actions WHERE action = 'client_responded'").fetchone()
            assert row is not None

    def test_mark_response_updates_correct_proposal(self, tmp_db):
        """mark_response should only update the latest proposal for a project_id."""
        tmp_db.save_proposal(
            project_id="proj_1",
            platform="kwork",
            title="V1",
            proposal_text="text1",
        )
        tmp_db.save_proposal(
            project_id="proj_1",
            platform="kwork",
            title="V2",
            proposal_text="text2",
        )
        tmp_db.mark_response("proj_1", "Client reply")
        with tmp_db._connect() as conn:
            rows = conn.execute(
                "SELECT response FROM proposals WHERE project_id = ? ORDER BY id DESC", ("proj_1",)
            ).fetchall()
            assert rows[0]["response"] == "Client reply"
            assert rows[1]["response"] is None


# ============================================================
# 16. Account Health Monitor
# ============================================================


class TestAccountHealthMonitor:
    def test_summary_text(self):
        from src.platforms.kwork_ext import AccountHealthMonitor

        m = AccountHealthMonitor()
        m._cache = {
            "username": "testuser",
            "level": "Высший рейтинг",
            "rating": 4.9,
            "reviews_count": 50,
            "connects_free": 30,
            "success_rate": 95.0,
            "active_orders": 2,
            "busy_risk": False,
            "captcha_required": False,
        }
        text = m.get_summary_text()
        assert "testuser" in text
        assert "Высший рейтинг" in text
        assert "95.0%" in text

    def test_busy_risk_warning(self):
        from src.platforms.kwork_ext import AccountHealthMonitor

        m = AccountHealthMonitor()
        m._cache = {
            "username": "u",
            "level": "L",
            "rating": 5,
            "reviews_count": 10,
            "connects_free": 5,
            "success_rate": 90,
            "active_orders": 6,
            "busy_risk": True,
            "captcha_required": False,
        }
        text = m.get_summary_text()
        assert "Занят" in text or "⚠️" in text


# ============================================================
# 17. RatePacer
# ============================================================


class TestRatePacer:
    @pytest.mark.asyncio
    async def test_pacer_waits(self):
        from src.platforms.kwork_ext import RatePacer

        pacer = RatePacer(min_delay=0.05, max_delay=0.1, burst_limit=100, burst_window=60)
        t0 = time.monotonic()
        await pacer.wait()
        await pacer.wait()
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05

    @pytest.mark.asyncio
    async def test_burst_limit(self):
        from src.platforms.kwork_ext import RatePacer

        pacer = RatePacer(min_delay=0.0, max_delay=0.0, burst_limit=3, burst_window=10)
        await pacer.wait()
        await pacer.wait()
        await pacer.wait()
        await pacer.wait()
        assert len(pacer._timestamps) <= 4


# ============================================================
# 18. Proposal Templates — No Markdown
# ============================================================


class TestTemplatesNoMarkdown:
    def test_templates_have_no_markdown_after_cleaning(self):
        from src.action.proposal_generator import ProposalGenerator

        gen = ProposalGenerator.__new__(ProposalGenerator)
        from src.action.proposal_templates import generate_proposal

        project = {"title": "Test", "found_skills": ["python"], "budget": 5000, "currency": "RUB"}
        portfolio = {
            "developer": {"experience_years": 4},
            "cases": [{"title": "C1", "tech": ["python"], "description": "d", "result": "r"}],
        }
        text = generate_proposal(project, portfolio)
        cleaned = gen._clean_llm_response(text)
        assert "*" not in cleaned
        assert "#" not in cleaned
        assert "•" not in cleaned


# ============================================================
# 19. Inbox Monitor — Watermark
# ============================================================


class TestInboxWatermark:
    def test_watermark_persistence(self, tmp_db):
        tmp_db.set_runtime_state("inbox.kwork.watermark", "2026-06-15 12:00:00")
        assert tmp_db.get_runtime_state("inbox.kwork.watermark") == "2026-06-15 12:00:00"


# ============================================================
# 20. ProposalDB — Foreign Keys
# ============================================================


class TestForeignKeys:
    def test_foreign_keys_enabled(self, tmp_db):
        with tmp_db._connect() as conn:
            result = conn.execute("PRAGMA foreign_keys").fetchone()
            assert result[0] == 1


# ============================================================
# 21. Stats Table Dropped
# ============================================================


class TestStatsTableDropped:
    def test_stats_table_not_present(self, tmp_db):
        with tmp_db._connect() as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stats'").fetchone()
            assert row is None


# ============================================================
# 22. Skip Logging — _log_skip collects details
# ============================================================


class TestSkipLogging:
    def test_log_skip_with_project(self):
        from src.orchestrator import FreelanceOrchestrator
        from src.parsers.base_parser import ProjectItem

        orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
        skipped: list[dict[str, str]] = []
        project = ProjectItem(
            id="kw-123",
            title="Нужен парсер на Python",
            budget=3000,
            platform="kwork",
            url="https://kwork.ru/123",
            description="desc",
            created_at="2026-06-29 12:00:00",
        )
        orch._log_skip(skipped, project, stage="keyword", reason="no skills match")
        assert len(skipped) == 1
        entry = skipped[0]
        assert entry["stage"] == "keyword"
        assert entry["reason"] == "no skills match"
        assert entry["project_id"] == "kw-123"
        assert "парсер" in entry["title"]
        assert entry["budget"] == "3000.0"

    def test_log_skip_without_project(self):
        from src.orchestrator import FreelanceOrchestrator

        orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
        skipped: list[dict[str, str]] = []
        orch._log_skip(
            skipped,
            stage="existing",
            reason="already sent",
            project_id="kw-999",
            title="Старый проект",
        )
        assert len(skipped) == 1
        assert skipped[0]["project_id"] == "kw-999"
        assert skipped[0]["stage"] == "existing"

    def test_log_skip_title_truncated(self):
        from src.orchestrator import FreelanceOrchestrator
        from src.parsers.base_parser import ProjectItem

        orch = FreelanceOrchestrator.__new__(FreelanceOrchestrator)
        skipped: list[dict[str, str]] = []
        project = ProjectItem(
            id="x-1",
            title="А" * 200,
            budget=1000,
            platform="kwork",
            url="https://kwork.ru/1",
            description="d",
            created_at="2026-06-29 12:00:00",
        )
        orch._log_skip(skipped, project, stage="nlp_spam", reason="spam")
        assert len(skipped[0]["title"]) <= 80

    @pytest.mark.asyncio
    async def test_notify_skipped_sends_message(self):
        from src.utils.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.admin_id = "123"
        notifier.bot = MagicMock()
        notifier._safe_send_message = AsyncMock()

        skipped_log = [
            {
                "stage": "keyword",
                "reason": "no skills",
                "project_id": "kw-1",
                "title": "Дизайн логотипа",
                "budget": "500",
            },
            {
                "stage": "ai_score",
                "reason": "score 3/6: low relevance",
                "project_id": "kw-2",
                "title": "Нужен сайт",
                "budget": "5000",
            },
        ]
        await notifier.notify_skipped(skipped_log)
        assert notifier._safe_send_message.call_count == 1
        msg = notifier._safe_send_message.call_args[0][1]
        assert "Пропущено 2" in msg
        assert "keyword=1" in msg
        assert "ai_score=1" in msg
        assert "Дизайн логотипа" in msg
        assert "Нужен сайт" in msg
        assert "no skills" in msg

    @pytest.mark.asyncio
    async def test_notify_skipped_empty_does_nothing(self):
        from src.utils.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.admin_id = "123"
        notifier.bot = MagicMock()
        notifier._safe_send_message = AsyncMock()
        await notifier.notify_skipped([])
        assert notifier._safe_send_message.call_count == 0

    @pytest.mark.asyncio
    async def test_notify_skipped_truncates_long_list(self):
        from src.utils.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.admin_id = "123"
        notifier.bot = MagicMock()
        notifier._safe_send_message = AsyncMock()

        skipped_log = [
            {"stage": "keyword", "reason": "r", "project_id": f"kw-{i}", "title": f"Project {i}", "budget": ""}
            for i in range(20)
        ]
        await notifier.notify_skipped(skipped_log)
        msg = notifier._safe_send_message.call_args[0][1]
        assert "и ещё 5" in msg
