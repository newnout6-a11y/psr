"""Классификация реплая клиента в одну из 5 категорий.

Вход: текст ответа клиента (русский, иногда английский).
Выход: одна из меток
    - `interested`     — заинтересован, готов обсуждать;
    - `negotiating`    — торгуется, обсуждает условия/сроки;
    - `rejected`       — отказ;
    - `asks_price`     — запрос цены/сметы;
    - `asks_portfolio` — запрос примеров/портфолио/опыта.

Стратегия:
1. Сначала бьём по дешёвым keyword-эвристикам — они закрывают
   ~60-70% типовых реплаев и не требуют сети.
2. Если эвристика не уверена, идём в LLM с жёстким system prompt'ом
   (constrained output: одно слово из словаря).
3. Любая ошибка LLM/таймаут → возвращаем `interested` как наименее
   вредный default (мы уведомим оператора в любом случае; неверная
   метка лишь чуть смажет аналитику).
"""

from __future__ import annotations

import re
from typing import Iterable

from loguru import logger

ALLOWED_LABELS: tuple[str, ...] = (
    "interested",
    "negotiating",
    "rejected",
    "asks_price",
    "asks_portfolio",
)

# Набор лёгких регулярок. Порядок важен: специальные намерения
# (asks_price, asks_portfolio, rejected) ловим раньше общего intent'а.
_HEURISTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "asks_price",
        re.compile(
            r"\b(сколько\s+(?:будет|стои[тл]|обойдётся)|какая\s+цена|за\s+сколько|"
            r"стоимость|расцен(?:ка|ки)|прайс|смет(?:а|у)|ценник|"
            r"how\s+much|what.{1,15}price|price\s+please|quote)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "asks_portfolio",
        re.compile(
            r"\b(портфолио|пример(?:ы)?\s+работ|кейс[ыо]в?|ваш(?:и|у)?\s+работ[ыа]|"
            r"что\s+(?:вы\s+)?делали|покажите\s+(?:пример|работы|портфолио)|"
            r"опыт\s+работы|portfolio|examples?|samples?\s+of\s+work|cases?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "rejected",
        re.compile(
            r"(?:^|\W)(не\s+подходит|не\s+интересно|не\s+нужно|спасибо,?\s*нет|"
            r"уже\s+(?:нашёл|выбрал|закрыл)|неактуально|не\s+актуально|"
            r"откажусь|отказ|пропуст(?:им|ите)|не\s+ваш\s+проект|"
            r"not\s+interested|no\s+thank|already\s+(?:found|hired|closed))(?:\W|$)",
            re.IGNORECASE,
        ),
    ),
    (
        "negotiating",
        re.compile(
            r"\b(а\s+если|можно\s+(?:дешевле|быстрее|за|со\s+скидкой)|"
            r"скидк(?:а|у|и)|(?:слишком\s+)?дорого|дороговато|"
            r"бюджет\s+(?:до|меньше|ограничен)|урезать|сократить|"
            r"торг(?:уем(?:ся)?|оваться)?|"
            r"(?:когда|какие)\s+сроки|по\s+срокам|сроки\s+какие|"
            r"can\s+you\s+do.{1,20}(?:cheaper|faster|less)|negotiate|discount)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "interested",
        re.compile(
            r"\b(давайте|обсуди[мт]|интересно|подходит|готов(?:а|ы)?\s+(?:работать|обсудить)|"
            r"начнём|погнали|свяжитесь|пишите|оста[ёе]мся\s+на\s+связи|"
            r"sounds\s+good|let'?s\s+(?:start|talk|discuss)|interested|works\s+for\s+me)\b",
            re.IGNORECASE,
        ),
    ),
)

_SYSTEM_PROMPT = (
    "Ты классификатор ответов заказчика на фриланс-биржах. "
    "Твоя задача — отнести сообщение ровно к одной категории из списка:\n"
    "- interested: заинтересован, готов работать/обсуждать без условий;\n"
    "- negotiating: торгуется, спорит про цену/сроки/объём;\n"
    "- rejected: отказ, проект уже закрыт, не подходит;\n"
    "- asks_price: спрашивает стоимость/смету/прайс;\n"
    "- asks_portfolio: просит показать примеры работ, кейсы, портфолио, опыт.\n"
    "Верни строго одно слово на латинице из списка выше. "
    "Никаких пояснений, кавычек, точек, переводов строки."
)


def _normalize_label(raw: str) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lower().split()[0] if raw.strip() else ""
    cleaned = re.sub(r"[^a-z_]", "", cleaned)
    return cleaned if cleaned in ALLOWED_LABELS else None


def _heuristic_label(text: str) -> str | None:
    if not text or not text.strip():
        return None
    snippet = text.strip()
    for label, pattern in _HEURISTIC_PATTERNS:
        if pattern.search(snippet):
            return label
    return None


def _truncate_for_llm(text: str, *, limit: int = 1500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def classify_reply_heuristic(text: str) -> str | None:
    """Чисто синхронная эвристика без сети — для тестов и быстрого fallback."""
    return _heuristic_label(text)


async def classify_reply(
    text: str,
    *,
    allowed: Iterable[str] = ALLOWED_LABELS,
) -> str | None:
    """Классифицировать ответ. Сначала эвристика, потом LLM, потом None.

    Возвращает None, только если ни эвристика, ни LLM не дали валидной
    метки — вызывающий должен зафиксировать «классификации нет» и
    оператор разберётся вручную.
    """
    allowed_set = {label for label in allowed if label in ALLOWED_LABELS}
    if not allowed_set:
        allowed_set = set(ALLOWED_LABELS)

    # 1. Дешёвый keyword-проход. Если попали — не ходим в LLM зря.
    label = _heuristic_label(text)
    if label and label in allowed_set:
        return label

    # 2. LLM-классификация с constrained output.
    try:
        from src.brain.llm_router import get_llm_router

        router = get_llm_router()
        prompt = f"Сообщение от заказчика:\n---\n{_truncate_for_llm(text)}\n---\nКатегория (одно слово):"
        result = await router.generate(
            prompt=prompt,
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=8,
            task="reply_classification",
        )
        normalized = _normalize_label(result or "")
        if normalized and normalized in allowed_set:
            return normalized
        logger.debug(f"ReplyClassifier: LLM вернул не из словаря: {result!r}")
    except Exception as exc:
        # Сеть упала / нет ключей / провайдеры все мертвы — это НЕ
        # причина падать в orchestrator-цикле. Возвращаем эвристический
        # лейбл, если он был, иначе None.
        logger.debug(f"ReplyClassifier: LLM недоступен ({exc}); fallback на heuristic")

    return label  # может быть None — это валидно, оператор увидит
