"""Пробив-площадки и контакт-интеллидженс.

Модуль оперирует контактами (email / phone / telegram / username) а НЕ username
фриланс-профиля. Контакты собираются отдельно (см. `extract.py`) и скармливаются
провайдерам, которые могут быть:

- **Бесплатные** (без ключа): EmailRep, WhatsMyName — работают из коробки.
- **Платные / с free tier** (через API-ключ в .env): HIBP, LeakCheck, IntelX.
  Регистрируются только если задан соответствующий ключ.

Провайдеры независимы: падение одного не влияет на остальные.
"""

from .base import ProbivProvider, ProbivFinding
from .extract import ContactExtractor, Contacts
from .emailrep import EmailRepProvider
from .whatsmyname import WhatsMyNameProvider
from .hibp import HIBPProvider
from .leakcheck import LeakCheckProvider
from .intelx import IntelXProvider

__all__ = [
    "ProbivProvider",
    "ProbivFinding",
    "ContactExtractor",
    "Contacts",
    "EmailRepProvider",
    "WhatsMyNameProvider",
    "HIBPProvider",
    "LeakCheckProvider",
    "IntelXProvider",
]
