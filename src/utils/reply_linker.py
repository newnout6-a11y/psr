"""Связка входящего сообщения с конкретным кандидатом.

InboxMonitor возвращает сырые dict'ы из API площадки. Phase 3 строит
обратную связь: «вот ответ заказчика → вот кандидат, на который мы
откликнулись», и сохраняет это в `candidates.replied_at/reply_text` для
аналитики конверсии.

Нарочно отдельный модуль (а не метод внутри InboxMonitor), потому что:

- логика «найти кандидата по username + project_id» нужна и в orchestrator-цикле,
  и в потенциальной ручной отвязке/переотвязке через дашборд / Telegram;
- классификация реплая через LLM — отдельная подзадача со своим IO,
  которую полезно тестировать без InboxMonitor;
- дать возможность звать линкер из тестов с фейковой `ProposalDB`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from loguru import logger

from src.action.proposal_db import ProposalDB


@dataclass
class ReplyLinkResult:
    candidate_id: Optional[int]
    project_id: Optional[str]
    project_title: Optional[str]
    linked: bool
    classification: Optional[str] = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "project_title": self.project_title,
            "linked": self.linked,
            "classification": self.classification,
            "reason": self.reason,
        }


def _normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "platform": (response.get("platform") or "").strip().lower(),
        "username": (response.get("username") or response.get("sender_username") or "").strip(),
        "project_id": (response.get("project_id") or "").strip() or None,
        "project_title": response.get("project_title") or "",
        "message": (response.get("message") or "").strip(),
        "timestamp": response.get("timestamp"),
    }


def link_inbox_response(
    db: ProposalDB,
    response: Mapping[str, Any],
    *,
    actor: str = "inbox_monitor",
) -> ReplyLinkResult:
    """Связать одно входящее сообщение с кандидатом, без классификации.

    Не делает сетевых запросов. Идемпотентен: повторный вызов с тем же
    реплаем по тому же кандидату ничего не запишет (см. `link_reply_to_candidate`).
    """
    norm = _normalize_response(response)
    if not norm["platform"] or not norm["username"] or not norm["message"]:
        return ReplyLinkResult(
            candidate_id=None,
            project_id=norm["project_id"],
            project_title=norm["project_title"],
            linked=False,
            reason="incomplete inbox payload",
        )

    candidate = db.find_candidate_for_reply(
        platform=norm["platform"],
        username=norm["username"],
        project_id=norm["project_id"],
    )
    if not candidate:
        logger.debug(
            "ReplyLinker: кандидат не найден для {platform}/{username} (project_id={pid})",
            platform=norm["platform"],
            username=norm["username"],
            pid=norm["project_id"],
        )
        return ReplyLinkResult(
            candidate_id=None,
            project_id=norm["project_id"],
            project_title=norm["project_title"],
            linked=False,
            reason="no candidate matched username/project",
        )

    candidate_id = int(candidate["candidate_id"])
    linked = db.link_reply_to_candidate(
        candidate_id,
        reply_text=norm["message"],
        replied_at=norm["timestamp"],
        actor=actor,
    )
    return ReplyLinkResult(
        candidate_id=candidate_id,
        project_id=candidate.get("project_id"),
        project_title=candidate.get("title"),
        linked=linked,
        reason="linked" if linked else "reply already recorded",
    )


async def classify_and_persist_reply(
    db: ProposalDB,
    candidate_id: int,
    reply_text: str,
    *,
    actor: str = "reply_classifier",
) -> Optional[str]:
    """LLM-классификация реплая + запись результата в БД.

    Изолирован от `link_inbox_response`, чтобы линковка не блокировалась
    сетью к LLM — и чтобы тесты могли проверять линковку независимо.
    Возвращает classification или None, если классификация не удалась.
    """
    if not reply_text or not reply_text.strip():
        return None
    # Локальный импорт: src.brain тащит за собой LLM SDK.
    from src.brain.reply_classifier import classify_reply

    classification = await classify_reply(reply_text)
    if not classification:
        return None
    try:
        db.set_reply_classification(candidate_id, classification, actor=actor)
    except Exception as exc:
        logger.warning(f"ReplyClassifier: запись классификации не удалась: {exc}")
        return classification
    return classification
