"""Rotation of browser fingerprints.

Не пытаемся быть полноценным anti-detect — только снижаем 'скучность' сессии.
Меняем User-Agent, viewport, Accept-Language и timezone между запусками
браузера. Поддерживаем детерминированный seed по profile_dir, чтобы
одна и та же сессия сохраняла согласованный отпечаток.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

# Актуальные сочетания UA / версий Chromium — обновляем раз в квартал.
_UA_POOL = [
    # Win10, Chrome 120..125
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    # Win11, Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
]

_VIEWPORTS = [
    (1920, 1080),
    (1536, 864),
    (1680, 1050),
    (1440, 900),
    (1366, 768),
]

_LANG_POOL = [
    "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "ru,en;q=0.9",
    "ru-RU,ru;q=0.9",
]

_TIMEZONES = [
    "Europe/Moscow",
    "Europe/Warsaw",
    "Europe/Kaliningrad",
    "Asia/Yekaterinburg",
]


@dataclass
class Fingerprint:
    user_agent: str
    viewport: tuple[int, int]
    accept_language: str
    timezone: str

    def browser_args(self) -> list[str]:
        """Аргументы для запуска Chromium через nodriver."""
        w, h = self.viewport
        return [
            f"--window-size={w},{h}",
            f"--user-agent={self.user_agent}",
            f"--lang={self.accept_language.split(',')[0]}",
            "--disable-blink-features=AutomationControlled",
        ]


def pick(seed: str | None = None) -> Fingerprint:
    """Возвращает отпечаток. Если передан seed — детерминированный выбор."""
    if seed:
        h = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(h)
    else:
        rng = random.Random()
    return Fingerprint(
        user_agent=rng.choice(_UA_POOL),
        viewport=rng.choice(_VIEWPORTS),
        accept_language=rng.choice(_LANG_POOL),
        timezone=rng.choice(_TIMEZONES),
    )
