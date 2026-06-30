"""PII и stop-word фильтр для текстов откликов.

Kwork мониторит чаты и отклики. Упоминание контактов, мессенджеров,
комиссии — strike (4 strikes = бан аккаунта).

Этот модоль проверяет текст отклика перед отправкой и:
1. Удаляет PII (телефоны, @username, email, telegram-ссылки)
2. Удаляет stop-words (комиссия, напрямую, на прямую, перевод на карту)
3. Логирует найденные нарушения
4. Возвращает очищенный текст

Используется в ProposalSender.send_proposal() как последний барьер.
"""

from __future__ import annotations

import re

from loguru import logger


_PII_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("phone_ru", "Российский телефон", re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")),
    ("phone_intl", "Международный телефон", re.compile(r"\+\d{1,4}[\s\-]?\(?\d{1,4}\)?[\s\-]?[\d\s\-]{4,}")),
    ("email", "Email", re.compile(r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b", re.IGNORECASE)),
    ("telegram_link", "Telegram ссылка", re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[\w\d_]+", re.IGNORECASE)),
    ("telegram_handle", "Telegram @username", re.compile(r"@\w{3,32}")),
    ("whatsapp", "WhatsApp", re.compile(r"\b(?:whats\s*app|ватсап|вацап)\b", re.IGNORECASE)),
    ("vk_link", "VK ссылка", re.compile(r"(?:https?://)?vk\.com/[\w\d_.]+", re.IGNORECASE)),
    ("skype", "Skype", re.compile(r"\bskype\s*[:\-]?\s*\w+\b", re.IGNORECASE)),
]

_STOP_WORD_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("commission", "Комиссия", re.compile(r"\b(?:комисси[яюей]|процент\s*площадк|плата\s*площадк)\b", re.IGNORECASE)),
    (
        "direct_payment",
        "Оплата напрямую",
        re.compile(r"\b(?:на\s+прямую|напрямую\s+оплач|на\s+карту|перевод\s+на\s+карт|кинь\s+карт)\b", re.IGNORECASE),
    ),
    (
        "off_platform",
        "Вне платформы",
        re.compile(r"\b(?:вне\s+бирж[иу]|без\s+бирж[иу]|вне\s+платформ)\b", re.IGNORECASE),
    ),
    ("telegram_word", "Слово telegram", re.compile(r"\b(?:телеграм|telegram|тг|tg)\b", re.IGNORECASE)),
    (
        "personal_contact",
        "Личные контакты",
        re.compile(
            r"\b(?:напишите\s+в\s+лс|напиши\s+в\s+личк|в\s+личные\s+сообщения|свяжитесь\s+лично)\b", re.IGNORECASE
        ),
    ),
    ("free_work", "Бесплатное тестовое", re.compile(r"\b(?:бесплатно\s+тестов|бесплатное\s+тест)\b", re.IGNORECASE)),
]


def filter_proposal_text(text: str) -> tuple[str, list[str]]:
    """Проверить и очистить текст отклика от PII и stop-words.

    Returns:
        (cleaned_text, violations) — очищенный текст и список описаний нарушений.
    """
    if not text:
        return "", []

    violations: list[str] = []
    cleaned = text

    for _name, desc, pattern in _PII_PATTERNS:
        matches = pattern.findall(cleaned)
        if matches:
            violations.append(f"{desc}: {matches[:3]}")
            cleaned = pattern.sub("", cleaned)

    for _name, desc, pattern in _STOP_WORD_PATTERNS:
        matches = pattern.findall(cleaned)
        if matches:
            violations.append(f"{desc}: {matches[:3]}")
            cleaned = pattern.sub("", cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if violations:
        logger.warning(f"PIIFilter: найдены нарушения в тексте отклика: {'; '.join(violations)}")

    return cleaned, violations


def is_safe_for_kwork(text: str) -> bool:
    """Быстрая проверка — безопасен ли текст для отправки на Kwork."""
    _, violations = filter_proposal_text(text)
    return len(violations) == 0
