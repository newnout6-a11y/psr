"""Извлечение контактов из произвольного текста.

Берём описание проекта + био заказчика + любые поля — ищем:
  - email (стандартный)
  - телефон (RU-формат и международный)
  - telegram username/ссылку
  - @никнеймы (могут быть instagram/twitter/vk)
  - URL профилей на соц-платформах
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])"
)

# RU номера: +7/8 с разделителями и без. Международные: +XX....
PHONE_RE = re.compile(
    r"(?:\+7|8|\+\d{1,3})[\s\-().]*\d{2,4}[\s\-().]*\d{2,4}[\s\-().]*\d{2,4}(?:[\s\-().]*\d{2,4})?"
)

# Telegram: t.me/xxx, tg://resolve?domain=xxx, @xxx в контексте слова "телеграм/tg"
TG_URL_RE = re.compile(
    r"(?:(?:https?://)?t(?:elegram)?\.me/|tg://resolve\?domain=)(@?[A-Za-z0-9_]{3,})",
    re.IGNORECASE,
)
TG_HINT_RE = re.compile(
    r"(?:telegram|телеграм|тг|tg)[^@\n\r]{0,30}@([A-Za-z0-9_]{3,})",
    re.IGNORECASE,
)

USERNAME_AT_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_]{3,32})(?![\w])")

URL_RE = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", re.IGNORECASE
)


@dataclass
class Contacts:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    telegrams: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)   # @никнеймы без контекста ТГ
    urls: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([self.emails, self.phones, self.telegrams, self.usernames, self.urls])

    def as_dict(self) -> dict:
        return {
            "emails": self.emails,
            "phones": self.phones,
            "telegrams": self.telegrams,
            "usernames": self.usernames,
            "urls": self.urls,
        }


class ContactExtractor:
    """Извлекает контакты из любых текстовых данных."""

    @staticmethod
    def _dedup(items: Iterable[str]) -> list[str]:
        seen = set()
        out = []
        for x in items:
            key = x.lower().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(x.strip())
        return out

    @staticmethod
    def _normalize_phone(raw: str) -> str:
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return ""
        # Нормализуем: 8XXX... → +7XXX...
        if digits.startswith("8") and len(digits) == 11:
            digits = "7" + digits[1:]
        if len(digits) >= 10:
            return "+" + digits
        return ""

    def extract(self, *texts: str) -> Contacts:
        blob = "\n".join(t for t in texts if t)
        if not blob:
            return Contacts()

        emails = self._dedup(m.group(0) for m in EMAIL_RE.finditer(blob))

        phones_raw = [m.group(0) for m in PHONE_RE.finditer(blob)]
        phones = self._dedup(p for p in (self._normalize_phone(x) for x in phones_raw) if p)

        telegrams: list[str] = []
        for m in TG_URL_RE.finditer(blob):
            telegrams.append(m.group(1).lstrip("@"))
        for m in TG_HINT_RE.finditer(blob):
            telegrams.append(m.group(1))
        telegrams = self._dedup(telegrams)

        usernames: list[str] = []
        # Берём все @xxx, которые НЕ попали в ТГ
        tg_set = {t.lower() for t in telegrams}
        for m in USERNAME_AT_RE.finditer(blob):
            nick = m.group(1)
            if nick.lower() in tg_set:
                continue
            # Исключаем шум: email-хвосты уже не попадут, но есть @ в тексте "вес 10 @ 2"
            if nick.isdigit():
                continue
            usernames.append(nick)
        usernames = self._dedup(usernames)

        urls = self._dedup(m.group(0) for m in URL_RE.finditer(blob))

        return Contacts(
            emails=emails,
            phones=phones,
            telegrams=telegrams,
            usernames=usernames,
            urls=urls,
        )
