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
    # Win10, Chrome 138..140 (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    # Win11, Edge 140
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    # macOS, Chrome 139
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36",
]

# Client Hints sec-ch-ua values matching each UA entry.
# Format: list of (brand, version) tuples that Chrome sends.
_SEC_CH_UA_POOL = [
    '"Chromium";v="138", "Not(A:Brand";v="24", "Google Chrome";v="138"',
    '"Chromium";v="139", "Not(A:Brand";v="24", "Google Chrome";v="139"',
    '"Chromium";v="140", "Not(A:Brand";v="24", "Google Chrome";v="140"',
    '"Chromium";v="140", "Not(A:Brand";v="24", "Microsoft Edge";v="140"',
    '"Chromium";v="139", "Not(A:Brand";v="24", "Google Chrome";v="139"',
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
    sec_ch_ua: str = ""

    def browser_args(self) -> list[str]:
        """Аргументы для запуска Chromium через nodriver."""
        w, h = self.viewport
        args = [
            f"--window-size={w},{h}",
            f"--user-agent={self.user_agent}",
            f"--lang={self.accept_language.split(',')[0]}",
            "--disable-blink-features=AutomationControlled",
        ]
        return args


def pick(seed: str | None = None) -> Fingerprint:
    """Возвращает отпечаток. Если передан seed — детерминированный выбор."""
    if seed:
        h = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(h)
    else:
        rng = random.Random()
    ua_idx = rng.randrange(len(_UA_POOL))
    return Fingerprint(
        user_agent=_UA_POOL[ua_idx],
        viewport=rng.choice(_VIEWPORTS),
        accept_language=rng.choice(_LANG_POOL),
        timezone=rng.choice(_TIMEZONES),
        sec_ch_ua=_SEC_CH_UA_POOL[ua_idx],
    )
