"""
Утилиты для конвертации времени в московский часовой пояс (UTC+3).
"""

import re
from datetime import datetime, timedelta, timezone


MSK = timezone(timedelta(hours=3))


def now_msk_str() -> str:
    """Текущее время в МСК."""
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def freelance_ru_date_to_msk(text: str) -> str:
    """
    Конвертация даты Freelance.ru в МСК.
    Примеры: 'Опубликовано2026-04-05 в 14:28', 'Обновлено2026-04-03 в 19:06'
    """
    if not text:
        return ""

    match = re.search(r"(\d{4}-\d{2}-\d{2})\s*[вв]+\s*(\d{2}:\d{2})", text)
    if not match:
        return text.strip()

    date_str = match.group(1)
    time_str = match.group(2)
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    except ValueError:
        return text.strip()


def parse_created_at(text: str, platform: str) -> str:
    """Нормализация даты проекта для активных платформ."""
    if not text:
        return ""

    if platform == "freelance_ru":
        return freelance_ru_date_to_msk(text)

    return text.strip()


def parse_created_at_datetime(text: str, platform: str) -> datetime | None:
    """Преобразовать дату проекта в datetime, если формат распознан."""
    normalized = parse_created_at(text, platform)
    if not normalized:
        return None

    candidate = normalized.replace(" МСК", "").strip()
    if not candidate:
        return None

    if candidate.isdigit():
        timestamp = int(candidate)
        if timestamp > 10**12:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    iso_candidate = candidate.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass

    formats = (
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue

    return None
