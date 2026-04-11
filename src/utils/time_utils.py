"""
Утилиты для конвертации времени в московский часовой пояс (UTC+3).
"""

from datetime import datetime, timezone, timedelta
from typing import Optional
import re

MSK = timezone(timedelta(hours=3))

# Смещения часовых поясов Freelancer.com
TZ_OFFSETS = {
    "EDT": -4, "EST": -5, "CDT": -5, "CST": -6,
    "MDT": -6, "MST": -7, "PDT": -7, "PST": -8,
    "UTC": 0, "GMT": 0, "AEST": 10, "AWST": 8,
    "CET": 1, "CEST": 2, "IST": 5.5,
}


def now_msk_str() -> str:
    """Текущее время в МСК."""
    return datetime.now(MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def relative_to_msk(text: str) -> str:
    """
    Конвертация относительного времени (FL.ru) в МСК.
    Примеры: '3 часа 33 минуты назад', '45 минут назад', '2 дня назад'
    """
    if not text:
        return ""
    
    # Если уже похоже на МСК дату — возвращаем как есть
    if "МСК" in text or "Обновлено" in text or "Опубликовано" in text:
        return text.strip()
    
    now_msk = datetime.now(MSK)
    
    # Парсим "X часов/час/часа Y минут назад"
    hours_match = re.search(r'(\d+)\s*час', text)
    minutes_match = re.search(r'(\d+)\s*минут', text)
    days_match = re.search(r'(\d+)\s*дн', text)
    weeks_match = re.search(r'(\d+)\s*недел', text)
    
    total_minutes = 0
    
    if days_match:
        total_minutes += int(days_match.group(1)) * 24 * 60
    if weeks_match:
        total_minutes += int(weeks_match.group(1)) * 7 * 24 * 60
    if hours_match:
        total_minutes += int(hours_match.group(1)) * 60
    if minutes_match:
        total_minutes += int(minutes_match.group(1))
    
    if total_minutes == 0:
        return text.strip()
    
    from datetime import timedelta
    created_at = now_msk - timedelta(minutes=total_minutes)
    return created_at.strftime("%d.%m.%Y %H:%M") + " МСК"


def freelancer_date_to_msk(text: str) -> str:
    """
    Конвертация даты Freelancer.com в МСК.
    Примеры: '05/04/2026 07:29 EDT', '15 days ago', 'in 3 hours'
    """
    if not text:
        return ""
    
    now_msk = datetime.now(MSK)
    
    # "DD/MM/YYYY HH:MM TZ"
    match = re.search(r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\s*([A-Z]{2,4})?', text)
    if match:
        date_str = match.group(1)
        tz_str = match.group(2) or "UTC"
        offset = TZ_OFFSETS.get(tz_str, 0)
        
        try:
            dt = datetime.strptime(date_str, "%d/%m/%Y %H:%M")
            # Переводим в UTC, потом в МСК
            from datetime import timedelta
            dt_utc = dt - timedelta(hours=offset)
            dt_msk = dt_utc.replace(tzinfo=timezone.utc).astimezone(MSK)
            return dt_msk.strftime("%d.%m.%Y %H:%M") + " МСК"
        except ValueError:
            pass
    
    # "X days/hours ago"
    days_match = re.search(r'(\d+)\s*days?\s+ago', text)
    hours_match = re.search(r'(\d+)\s*hours?\s+ago', text)
    
    if days_match:
        from datetime import timedelta
        dt = now_msk - timedelta(days=int(days_match.group(1)))
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    if hours_match:
        from datetime import timedelta
        dt = now_msk - timedelta(hours=int(hours_match.group(1)))
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    return text.strip()


def freelance_ru_date_to_msk(text: str) -> str:
    """
    Конвертация даты Freelance.ru в МСК.
    Примеры: 'Опубликовано2026-04-05 в 14:28', 'Обновлено2026-04-03 в 19:06'
    """
    if not text:
        return ""
    
    # Ищем дату в формате YYYY-MM-DD в HH:MM
    match = re.search(r'(\d{4}-\d{2}-\d{2})\s*[вв]+\s*(\d{2}:\d{2})', text)
    if match:
        date_str = match.group(1)
        time_str = match.group(2)
        
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            # Freelance.ru — московское время, так что просто форматируем
            return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
        except ValueError:
            pass
    
    return text.strip()


def pph_date_to_msk(text: str) -> str:
    """
    Конвертация даты PeoplePerHour в МСК.
    Примеры: 'an hour ago', '2 hours ago', '10 minutes ago', '1 day ago'
    """
    if not text:
        return ""
    
    now_msk = datetime.now(MSK)
    text_lower = text.lower().strip()
    
    from datetime import timedelta
    
    # "X hours ago" / "X hour ago"
    hours_match = re.search(r'(\d+)\s*hours?\s+ago', text_lower)
    if hours_match:
        dt = now_msk - timedelta(hours=int(hours_match.group(1)))
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    # "X minutes ago" / "X minute ago"
    minutes_match = re.search(r'(\d+)\s*minutes?\s+ago', text_lower)
    if minutes_match:
        dt = now_msk - timedelta(minutes=int(minutes_match.group(1)))
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    # "X days ago"
    days_match = re.search(r'(\d+)\s*days?\s+ago', text_lower)
    if days_match:
        dt = now_msk - timedelta(days=int(days_match.group(1)))
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    # "an hour ago"
    if "an hour ago" in text_lower:
        dt = now_msk - timedelta(hours=1)
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    # "a day ago"
    if "a day ago" in text_lower:
        dt = now_msk - timedelta(days=1)
        return dt.strftime("%d.%m.%Y %H:%M") + " МСК"
    
    return text.strip()


def parse_created_at(text: str, platform: str) -> str:
    """
    Универсальный парсер даты в МСК.
    
    Args:
        text: Сырой текст даты
        platform: fl_ru, freelancer_com, freelance_ru
    """
    if not text:
        return ""
    
    if platform == "fl_ru":
        return relative_to_msk(text)
    elif platform == "freelancer_com":
        return freelancer_date_to_msk(text)
    elif platform == "freelance_ru":
        return freelance_ru_date_to_msk(text)
    elif platform == "peopleperhour":
        return pph_date_to_msk(text)
    else:
        return text.strip()
