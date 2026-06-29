"""Базовые типы и интерфейсы для OSINT-провайдеров."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OSINTFinding:
    """Единица данных, найденная провайдером."""

    source: str  # "github", "habr", "duckduckgo", "kwork_profile", ...
    kind: str  # "profile", "repo", "article", "comment", "mention"
    title: str  # короткое описание находки
    url: str = ""
    snippet: str = ""  # описание/превью
    confidence: float = 0.5  # 0.0 — слабая, 1.0 — точное совпадение
    meta: dict[str, Any] = field(default_factory=dict)


class OSINTProvider(ABC):
    """Базовый асинхронный провайдер OSINT.

    Каждый провайдер независим: падение одного не ломает остальных.
    """

    name: str = "base"

    # Таймаут на провайдера, чтобы агрегатор не висел
    timeout_sec: float = 10.0

    @abstractmethod
    async def search(self, query: str, **ctx: Any) -> list[OSINTFinding]:
        """Поиск по запросу (обычно username или "имя + контекст")."""
        ...
