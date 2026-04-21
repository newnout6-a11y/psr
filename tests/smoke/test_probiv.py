"""Тесты пробив-модуля.

Запуск: python tests/smoke/test_probiv.py
        python -m pytest tests/smoke/test_probiv.py -v
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ---------- ContactExtractor ----------

def test_extract_email():
    from src.osint.probiv import ContactExtractor
    ex = ContactExtractor()
    c = ex.extract("Свяжитесь: ivan.petrov@example.com или admin@mysite.co.uk")
    assert "ivan.petrov@example.com" in c.emails
    assert "admin@mysite.co.uk" in c.emails


def test_extract_phone_ru():
    from src.osint.probiv import ContactExtractor
    ex = ContactExtractor()
    c = ex.extract("Звоните +7 (999) 123-45-67 или 8(495)1234567")
    assert any(p.startswith("+7999") for p in c.phones)
    assert any(p.startswith("+7495") for p in c.phones)


def test_extract_telegram():
    from src.osint.probiv import ContactExtractor
    ex = ContactExtractor()
    c = ex.extract("Мой Telegram: @ivan_dev или t.me/coolguy123")
    assert "ivan_dev" in c.telegrams
    assert "coolguy123" in c.telegrams


def test_extract_urls():
    from src.osint.probiv import ContactExtractor
    ex = ContactExtractor()
    c = ex.extract("Портфолио https://github.com/ivan и https://habr.com/ru/users/ivan/")
    assert any("github.com/ivan" in u for u in c.urls)


def test_empty():
    from src.osint.probiv import ContactExtractor
    c = ContactExtractor().extract("Просто описание проекта, никаких контактов.")
    assert c.is_empty()


# ---------- Provider imports / lifecycle ----------

def test_probiv_providers_import():
    from src.osint.probiv import (
        EmailRepProvider,
        WhatsMyNameProvider,
        HIBPProvider,
        LeakCheckProvider,
        IntelXProvider,
    )
    # Бесплатные должны инстанцироваться без ключей
    _ = EmailRepProvider()
    _ = WhatsMyNameProvider()
    # Платные — тоже, но accepts пустой не должен быть
    assert EmailRepProvider().accepts == ("email",)
    assert "username" in WhatsMyNameProvider().accepts
    assert HIBPProvider().accepts == ("email",)
    assert "email" in LeakCheckProvider().accepts
    assert "email" in IntelXProvider().accepts


def test_paid_providers_need_key():
    import os
    # Гарантируем что ключей нет
    for k in ("HIBP_API_KEY", "LEAKCHECK_API_KEY", "INTELX_API_KEY"):
        os.environ.pop(k, None)

    from src.osint.probiv import HIBPProvider, LeakCheckProvider, IntelXProvider

    async def run():
        assert await HIBPProvider().lookup(email="test@test.com") == []
        assert await LeakCheckProvider().lookup(email="test@test.com") == []
        assert await IntelXProvider().lookup(email="test@test.com") == []

    asyncio.run(run())


# ---------- Aggregator integration ----------

def test_aggregator_with_probiv_disabled_gracefully():
    import os
    # Отключаем внешние сетевые провайдеры, оставляем только пустые
    os.environ["OSINT_PROVIDERS"] = ""
    os.environ["OSINT_PROBIV_PROVIDERS"] = ""

    from src.osint import OSINTAggregator

    async def run():
        agg = OSINTAggregator()
        r = await agg.gather(
            "nonexistent_user_xyz123",
            description="Пишите на test@example.com или в телеграм @somenick",
        )
        # Контакты должны быть извлечены
        assert "test@example.com" in r.contacts.get("emails", [])
        assert "somenick" in r.contacts.get("telegrams", [])
        # Findings = 0 (провайдеры отключены)
        assert r.findings == []
        assert r.probiv_findings == []

    asyncio.run(run())


if __name__ == "__main__":
    test_extract_email()
    print("[ok] extract email")
    test_extract_phone_ru()
    print("[ok] extract phone RU")
    test_extract_telegram()
    print("[ok] extract telegram")
    test_extract_urls()
    print("[ok] extract urls")
    test_empty()
    print("[ok] extract empty")
    test_probiv_providers_import()
    print("[ok] provider imports")
    test_paid_providers_need_key()
    print("[ok] paid providers fail-safe without key")
    test_aggregator_with_probiv_disabled_gracefully()
    print("[ok] aggregator integration w/ extracted contacts")
    print("\nВсе тесты probiv прошли.")
