"""Агрегатор OSINT-провайдеров.

Запускает всех параллельно с таймаутом, кэширует, возвращает единый результат.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from .base import OSINTFinding, OSINTProvider
from .cache import OSINTCache
from .probiv import (
    ContactExtractor,
    Contacts,
    EmailRepProvider,
    HIBPProvider,
    IntelXProvider,
    LeakCheckProvider,
    ProbivFinding,
    ProbivProvider,
    WhatsMyNameProvider,
)
from .providers import (
    DuckDuckGoProvider,
    GitHubProvider,
    HabrProvider,
    KworkProfileProvider,
)


@dataclass
class OSINTResult:
    """Результат агрегации по одному заказчику."""
    username: str
    findings: list[OSINTFinding] = field(default_factory=list)
    probiv_findings: list[ProbivFinding] = field(default_factory=list)
    contacts: dict[str, list[str]] = field(default_factory=dict)
    summary: str = ""                      # короткое резюме для LLM
    reputation_score: int = 50             # 0-100, для веттинга
    red_flags: list[str] = field(default_factory=list)
    positive_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "findings": [asdict(f) for f in self.findings],
            "probiv_findings": [asdict(f) for f in self.probiv_findings],
            "contacts": self.contacts,
            "summary": self.summary,
            "reputation_score": self.reputation_score,
            "red_flags": self.red_flags,
            "positive_signals": self.positive_signals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OSINTResult":
        findings = [OSINTFinding(**f) for f in data.get("findings", [])]
        probiv = [ProbivFinding(**f) for f in data.get("probiv_findings", [])]
        return cls(
            username=data.get("username", ""),
            findings=findings,
            probiv_findings=probiv,
            contacts=data.get("contacts", {}),
            summary=data.get("summary", ""),
            reputation_score=int(data.get("reputation_score", 50)),
            red_flags=data.get("red_flags", []),
            positive_signals=data.get("positive_signals", []),
        )


def _has_key_for(name: str) -> bool:
    """Проверка наличия API-ключа для платных probiv-провайдеров."""
    env_map = {
        "hibp": "HIBP_API_KEY",
        "leakcheck": "LEAKCHECK_API_KEY",
        "intelx": "INTELX_API_KEY",
    }
    env_var = env_map.get(name)
    return bool(env_var and os.getenv(env_var, "").strip())


class OSINTAggregator:
    """Оркестратор OSINT-провайдеров с кэшем и устойчивостью к ошибкам."""

    def __init__(
        self,
        cache: OSINTCache | None = None,
        providers: list[OSINTProvider] | None = None,
        probiv_providers: list[ProbivProvider] | None = None,
    ):
        self.cache = cache or OSINTCache()
        if providers is None:
            providers = self._default_providers()
        if probiv_providers is None:
            probiv_providers = self._default_probiv_providers()
        self.providers = providers
        self.probiv_providers = probiv_providers
        self.contacts_extractor = ContactExtractor()

    @staticmethod
    def _default_providers() -> list[OSINTProvider]:
        """Включаем только то что разрешено флагами (все по умолчанию ON)."""
        enabled = os.getenv("OSINT_PROVIDERS", "github,habr,duckduckgo,kwork_profile")
        active = {name.strip().lower() for name in enabled.split(",") if name.strip()}
        out: list[OSINTProvider] = []
        mapping = {
            "github": GitHubProvider,
            "habr": HabrProvider,
            "duckduckgo": DuckDuckGoProvider,
            "kwork_profile": KworkProfileProvider,
        }
        for name, cls in mapping.items():
            if name in active:
                try:
                    out.append(cls())
                except Exception as e:
                    logger.warning(f"OSINT: не удалось инициализировать {name}: {e}")
        return out

    @staticmethod
    def _default_probiv_providers() -> list[ProbivProvider]:
        """Пробив-провайдеры. По умолчанию ВКЛЮЧЕНЫ бесплатные emailrep+whatsmyname:
        они работают без ключа и дают полезные сигналы (утечки, аккаунты на других
        сервисах). Платные (hibp/leakcheck/intelx) добавляйте явным списком в
        OSINT_PROBIV_PROVIDERS — без API-ключа они тихо отвалятся всё равно.
        Пустая строка `OSINT_PROBIV_PROVIDERS=` полностью выключает пробив.
        """
        enabled = os.getenv(
            "OSINT_PROBIV_PROVIDERS",
            "emailrep,whatsmyname",
        )
        active = {name.strip().lower() for name in enabled.split(",") if name.strip()}
        mapping = {
            "emailrep": EmailRepProvider,
            "whatsmyname": WhatsMyNameProvider,
            "hibp": HIBPProvider,
            "leakcheck": LeakCheckProvider,
            "intelx": IntelXProvider,
        }
        out: list[ProbivProvider] = []
        for name, cls in mapping.items():
            if name not in active:
                continue
            try:
                inst = cls()
                # Платные — регистрируем только если API-key задан
                if inst.requires_key and not _has_key_for(name):
                    logger.debug(f"Probiv[{name}] пропущен: нет API-ключа")
                    continue
                out.append(inst)
            except Exception as e:
                logger.warning(f"Probiv: не удалось инициализировать {name}: {e}")
        return out

    async def gather(
        self,
        username: str,
        *,
        email: str | None = None,
        phone: str | None = None,
        telegram: str | None = None,
        description: str = "",
        **ctx: Any,
    ) -> OSINTResult:
        """Основной вход: собираем findings по всем провайдерам + probiv.

        Args:
            username:     ник заказчика (с kwork/fl.ru/...).
            email/phone/telegram: известные контакты (если есть).
            description:  произвольный текст, из которого попытаемся
                          извлечь ещё контакты (описание проекта, bio).
            **ctx:        контекст для OSINT-провайдеров (например, context=название проекта).
        """
        username = (username or "").strip()
        if not username:
            return OSINTResult(username="")

        # 1. Кэш (ключ включает все входные контакты, чтобы не мешать результаты)
        cache_key = self._cache_key(username, email, phone, telegram)
        cached = self.cache.get(cache_key)
        if cached:
            logger.debug(f"OSINT cache hit: {username}")
            return OSINTResult.from_dict(cached)

        # 2. Извлечение контактов из описания
        contacts = self.contacts_extractor.extract(description, ctx.get("context", ""))
        # Добавляем явно переданные
        if email and email not in contacts.emails:
            contacts.emails.insert(0, email)
        if phone and phone not in contacts.phones:
            contacts.phones.insert(0, phone)
        if telegram and telegram not in contacts.telegrams:
            contacts.telegrams.insert(0, telegram)

        # 3. OSINT-провайдеры (github, habr, kwork_profile, duckduckgo) по username
        osint_tasks = [self._run_provider(p, username, ctx) for p in self.providers]
        # 4. Probiv-провайдеры по всем собранным контактам
        probiv_tasks = list(self._make_probiv_tasks(username, contacts))

        osint_results, probiv_results = await asyncio.gather(
            asyncio.gather(*osint_tasks, return_exceptions=False),
            asyncio.gather(*probiv_tasks, return_exceptions=False),
        )

        all_findings: list[OSINTFinding] = []
        for r in osint_results:
            all_findings.extend(r)
        all_probiv: list[ProbivFinding] = []
        for r in probiv_results:
            all_probiv.extend(r)

        # 5. Агрегируем сигналы
        result = OSINTResult(
            username=username,
            findings=all_findings,
            probiv_findings=all_probiv,
            contacts=contacts.as_dict(),
        )
        self._compute_signals(result)

        # 6. Сохраняем в кэш
        self.cache.set(cache_key, result.to_dict())
        return result

    @staticmethod
    def _cache_key(username: str, email: str | None, phone: str | None, telegram: str | None) -> str:
        parts = [f"user:{username.lower()}"]
        if email:
            parts.append(f"e:{email.lower()}")
        if phone:
            parts.append(f"p:{phone}")
        if telegram:
            parts.append(f"t:{telegram.lower()}")
        return "|".join(parts)

    def _make_probiv_tasks(self, username: str, contacts: Contacts):
        """Собирает корутины пробива: каждому провайдеру — каждый релевантный контакт."""
        for provider in self.probiv_providers:
            accepted = provider.accepts
            # email
            if "email" in accepted:
                for em in contacts.emails[:3]:   # лимит чтобы не сжечь платный API
                    yield self._run_probiv(provider, email=em)
            # phone
            if "phone" in accepted:
                for ph in contacts.phones[:2]:
                    yield self._run_probiv(provider, phone=ph)
            # telegram
            if "telegram" in accepted:
                for tg in contacts.telegrams[:3]:
                    yield self._run_probiv(provider, telegram=tg)
            # username
            if "username" in accepted:
                # всегда свой username + найденные в описании
                unames = [username] + [u for u in contacts.usernames[:2] if u.lower() != username.lower()]
                for un in unames:
                    yield self._run_probiv(provider, username=un)

    async def _run_probiv(self, provider: ProbivProvider, **kwargs) -> list[ProbivFinding]:
        try:
            return await asyncio.wait_for(
                provider.lookup(**kwargs), timeout=provider.timeout_sec
            )
        except asyncio.TimeoutError:
            logger.debug(f"Probiv[{provider.name}] timeout для {kwargs}")
            return []
        except Exception as e:
            logger.debug(f"Probiv[{provider.name}] ошибка: {e}")
            return []

    async def _run_provider(
        self, provider: OSINTProvider, username: str, ctx: dict[str, Any]
    ) -> list[OSINTFinding]:
        """Запуск провайдера с таймаутом и подавлением исключений."""
        try:
            return await asyncio.wait_for(
                provider.search(username, **ctx), timeout=provider.timeout_sec
            )
        except asyncio.TimeoutError:
            logger.debug(f"OSINT[{provider.name}] timeout для {username}")
            return []
        except Exception as e:
            logger.debug(f"OSINT[{provider.name}] ошибка: {e}")
            return []

    # ---------------- сигналы и резюме ----------------

    def _compute_signals(self, result: OSINTResult) -> None:
        """Простая эвристика: больше подтверждений → выше репутация."""
        score = 50
        red: list[str] = []
        pos: list[str] = []

        by_source: dict[str, list[OSINTFinding]] = {}
        for f in result.findings:
            by_source.setdefault(f.source, []).append(f)

        # GitHub: если нашли профиль с >10 репо и >5 фолловерами — плюс
        for f in by_source.get("github", []):
            if f.kind == "profile" and f.confidence >= 0.7:
                pos.append(f"GitHub: @{f.meta.get('login')} ({f.meta.get('public_repos', 0)} репо)")
                repos = f.meta.get("public_repos", 0)
                followers = f.meta.get("followers", 0)
                if repos >= 10:
                    score += 8
                if followers >= 10:
                    score += 5

        # Habr: присутствие — плюс
        for f in by_source.get("habr", []):
            pos.append(f"Habr: {f.title}")
            score += 5

        # Kwork-профиль: много отзывов → плюс; негативные → минус
        reviews = [f for f in by_source.get("kwork_profile", []) if f.kind == "review"]
        if reviews:
            total = len(reviews)
            negative = sum(1 for f in reviews if not f.meta.get("positive"))
            pos.append(f"Kwork-отзывов: {total}")
            if negative > total * 0.3:
                red.append(f"Много негативных отзывов на Kwork ({negative}/{total})")
                score -= 15
            elif total >= 5 and negative <= 1:
                score += 10

        # DuckDuckGo: наличие упоминаний на значимых доменах
        mentions = [f for f in by_source.get("duckduckgo", []) if f.kind == "mention"]
        strong_mentions = [f for f in mentions if f.confidence >= 0.6]
        if strong_mentions:
            pos.append(f"Открытые упоминания: {len(strong_mentions)}")
            score += min(10, len(strong_mentions) * 2)

        # --- Пробив-сигналы ---
        probiv_by_source: dict[str, list[ProbivFinding]] = {}
        for f in result.probiv_findings:
            probiv_by_source.setdefault(f.source, []).append(f)

        # EmailRep: утечки и suspicious — риск
        for f in probiv_by_source.get("emailrep", []):
            if f.kind != "risk":
                continue
            m = f.meta or {}
            if m.get("credentials_leaked"):
                red.append(f"email в утечках: {m.get('data_breach') or 'есть'}")
                score -= 5
            if m.get("malicious_activity"):
                red.append("EmailRep: malicious activity")
                score -= 8
            if m.get("suspicious"):
                red.append("EmailRep: suspicious")
                score -= 5
            if m.get("disposable"):
                red.append("одноразовый email (disposable)")
                score -= 15
            refs = int(m.get("references") or 0)
            if refs >= 50:
                pos.append(f"публичные упоминания email ({refs})")
                score += 3
        # Найденные платформы по email (GitHub/Twitter/...)
        email_accounts = [f for f in probiv_by_source.get("emailrep", []) if f.kind == "account"]
        if email_accounts:
            platforms = ", ".join(a.meta.get("platform", "?") for a in email_accounts[:6])
            pos.append(f"email-аккаунты: {platforms}")

        # HIBP: серьёзные утечки
        hibp_risks = [f for f in probiv_by_source.get("hibp", []) if f.kind == "risk"]
        for f in hibp_risks:
            m = f.meta or {}
            total = int(m.get("total_breaches") or 0)
            recent = int(m.get("recent_breaches") or 0)
            red.append(f"HIBP: {total} утечек (свежих ≤2г: {recent})")
            # Это НЕ вина заказчика, но сигнал что учётка старая/засвеченная
            score -= min(8, total)

        # LeakCheck
        lc_risks = [f for f in probiv_by_source.get("leakcheck", []) if f.kind == "risk"]
        for f in lc_risks:
            cnt = int((f.meta or {}).get("count") or 0)
            if cnt > 0:
                red.append(f"LeakCheck: {cnt} записей в утечках")
                score -= min(6, cnt // 2)

        # IntelX — серьёзнее всего (darknet / paste)
        ix_risks = [f for f in probiv_by_source.get("intelx", []) if f.kind == "risk"]
        for f in ix_risks:
            cnt = int((f.meta or {}).get("count") or 0)
            if cnt > 0:
                red.append(f"IntelX: {cnt} записей в даркнете/утечках")
                score -= min(10, cnt)

        # WhatsMyName — подтверждённые аккаунты на сторонних сервисах
        wmn_accounts = probiv_by_source.get("whatsmyname", [])
        if wmn_accounts:
            sites = ", ".join(a.meta.get("site", "?") for a in wmn_accounts[:8])
            pos.append(f"WhatsMyName ({len(wmn_accounts)}): {sites}")
            # чем больше цифровых следов тем выше легитимность
            score += min(10, len(wmn_accounts))

        # --- Контакты найдены в описании (полезно для лид-квалификации) ---
        if result.contacts:
            found_bits = []
            if result.contacts.get("emails"):
                found_bits.append(f"email: {result.contacts['emails'][0]}")
            if result.contacts.get("phones"):
                found_bits.append(f"phone: {result.contacts['phones'][0]}")
            if result.contacts.get("telegrams"):
                found_bits.append(f"tg: @{result.contacts['telegrams'][0]}")
            if found_bits:
                pos.append("контакты: " + ", ".join(found_bits))

        # Призрак: если все источники молчат — подозрительно
        if not result.findings and not result.probiv_findings:
            red.append("Нет публичных следов — возможно 'призрак'")
            score -= 10

        result.reputation_score = max(0, min(100, score))
        result.red_flags = red
        result.positive_signals = pos
        result.summary = self._build_summary(result)

    @staticmethod
    def _build_summary(result: OSINTResult) -> str:
        """Текстовая выжимка для LLM-промпта (3-5 строк)."""
        lines: list[str] = []
        if result.positive_signals:
            lines.append("Положительные: " + ", ".join(result.positive_signals[:4]))
        if result.red_flags:
            lines.append("Риски: " + "; ".join(result.red_flags[:3]))
        if not lines:
            lines.append("Публичных сигналов не найдено.")
        lines.append(f"Репутация OSINT: {result.reputation_score}/100")
        return "\n".join(lines)
