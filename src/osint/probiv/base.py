"""Базовый интерфейс пробив-провайдера."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbivFinding:
    """Находка пробив-провайдера.

    `kind` — стандартизированная классификация:
      - "breach"      — email/phone в утечке
      - "suspicious"  — подозрительный аккаунт (disposable/spam-pattern)
      - "account"     — обнаружен аккаунт на стороннем сервисе
      - "profile"     — публичный профиль (vk/ok/fb/etc)
      - "leak"        — запись в даркнете / файловой утечке
      - "risk"        — обобщённый красный флаг
    """
    source: str                      # "hibp", "leakcheck", "emailrep", ...
    kind: str
    title: str
    snippet: str = ""
    url: str = ""
    severity: str = "info"           # info | low | medium | high | critical
    confidence: float = 0.7
    meta: dict[str, Any] = field(default_factory=dict)


class ProbivProvider(ABC):
    """Асинхронный пробив-провайдер.

    Каждый провайдер работает с одной или несколькими 'сущностями':
    email / phone / telegram / username. Контакты передаются через kwargs.
    """

    name: str = "base"
    requires_key: bool = False       # если True — регистрируется только при наличии ключа
    timeout_sec: float = 12.0

    # Какие поля контакта провайдер умеет принимать.
    accepts: tuple[str, ...] = ()    # например ("email",) или ("email", "phone", "username")

    @abstractmethod
    async def lookup(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        telegram: str | None = None,
        username: str | None = None,
        **ctx: Any,
    ) -> list[ProbivFinding]:
        """Вернуть список находок. Пустой список если ничего не найдено."""
        ...
