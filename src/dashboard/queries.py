"""Централизованные SQL-запросы для dashboard vNext."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from src.paths import LOGS_DB_FILE, PROPOSALS_DB_FILE

LOGS_DB = str(LOGS_DB_FILE)
PROPOSALS_DB = str(PROPOSALS_DB_FILE)


def _ensure_proposals_schema() -> None:
    try:
        from src.action.proposal_db import ProposalDB

        ProposalDB()
    except Exception:
        pass


def _read_sql(db_path: str, query: str, params: tuple = ()) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    try:
        with sqlite3.connect(db_path) as conn:
            return pd.read_sql_query(query, conn, params=params)
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return pd.DataFrame()


def candidate_discovery_snapshot(*, after_id: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Read the orchestrator's persisted candidate insertions without sending notifications.

    The monotonically increasing candidate_id tracks *new rows*, not page
    discoveries, suitability decisions, or state changes to existing rows.
    The separate Buyer Search repository is intentionally not included.
    """
    missing = {
        "status": "unavailable",
        "reason": "База кандидатов ещё не создана",
        "latest_id": 0,
        "next_after_id": after_id if after_id is not None else 0,
        "has_more": False,
        "total": 0,
        "by_platform": [],
        "recent": [],
        "items": [],
    }
    db_path = Path(PROPOSALS_DB)
    if not db_path.is_file():
        return missing
    try:
        # Opening read-only prevents a monitoring request from creating or
        # migrating the live proposal database.
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(candidates)")}
            required = {"candidate_id", "project_id", "platform", "title", "status", "updated_at"}
            if not required.issubset(columns):
                return {**missing, "reason": "Таблица кандидатов или необходимые поля пока недоступны"}
            summary = connection.execute(
                "SELECT COALESCE(MAX(candidate_id), 0) AS latest_id, COUNT(*) AS total FROM candidates"
            ).fetchone()
            latest_id = int(summary["latest_id"])
            total = int(summary["total"])
            by_platform = [
                {"platform": str(row["platform"] or "unknown"), "total": int(row["total"])}
                for row in connection.execute(
                    "SELECT platform, COUNT(*) AS total FROM candidates GROUP BY platform ORDER BY total DESC, platform"
                )
            ]
            fields = "candidate_id, project_id, platform, title, status, updated_at"
            recent = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {fields} FROM candidates ORDER BY candidate_id DESC LIMIT 10"
                )
            ]
            # The first request is a baseline. Historical rows appear as
            # 'recent' but are never reported as newly detected notifications.
            next_after_id = latest_id if after_id is None else after_id
            items: list[dict[str, Any]] = []
            if after_id is not None:
                items = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT {fields} FROM candidates WHERE candidate_id > ? ORDER BY candidate_id LIMIT ?",
                        (after_id, limit),
                    )
                ]
                if items:
                    next_after_id = int(items[-1]["candidate_id"])
            return {
                "status": "ready",
                "reason": None,
                "latest_id": latest_id,
                "next_after_id": next_after_id,
                "has_more": latest_id > next_after_id,
                "total": total,
                "by_platform": by_platform,
                "recent": recent,
                "items": items,
            }
    except sqlite3.Error:
        return {**missing, "reason": "Не удалось прочитать базу кандидатов; проверьте доступность SQLite"}


def overview_metrics(days: int = 7) -> dict[str, Any]:
    metrics = {
        "parsed": 0,
        "queued": 0,
        "manual_sent": 0,
        "auto_sent": 0,
        "draft": 0,
        "responses": 0,
    }

    parsed = _read_sql(
        LOGS_DB,
        """
        SELECT COALESCE(SUM(projects_count), 0) AS total
        FROM parse_logs
        WHERE status = 'success' AND timestamp >= datetime('now', ?)
        """,
        (f"-{days} days",),
    )
    if not parsed.empty:
        metrics["parsed"] = int(parsed.iloc[0]["total"] or 0)

    queued = _read_sql(
        PROPOSALS_DB,
        """
        SELECT COALESCE(COUNT(*), 0) AS total
        FROM candidates
        WHERE status IN ('queued', 'snoozed')
        """,
    )
    if not queued.empty:
        metrics["queued"] = int(queued.iloc[0]["total"] or 0)

    proposals = _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            SUM(CASE WHEN status = 'manual_sent' THEN 1 ELSE 0 END) AS manual_sent,
            SUM(CASE WHEN status = 'auto_sent'   THEN 1 ELSE 0 END) AS auto_sent,
            SUM(CASE WHEN status = 'draft'       THEN 1 ELSE 0 END) AS draft,
            SUM(CASE WHEN response IS NOT NULL AND response != '' THEN 1 ELSE 0 END) AS responses
        FROM proposals
        WHERE sent_at >= datetime('now', ?)
        """,
        (f"-{days} days",),
    )
    if not proposals.empty:
        row = proposals.iloc[0]
        metrics["manual_sent"] = int(row.get("manual_sent", 0) or 0)
        metrics["auto_sent"] = int(row.get("auto_sent", 0) or 0)
        metrics["draft"] = int(row.get("draft", 0) or 0)
        metrics["responses"] = int(row.get("responses", 0) or 0)

    return metrics


def runtime_state_snapshot() -> dict[str, Any]:
    data: dict[str, Any] = {"execution_mode": "semi_auto", "paused_platforms": []}
    df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT state_key, state_value, updated_at
        FROM runtime_state
        ORDER BY state_key
        """,
    )
    if df.empty:
        return data

    for _, row in df.iterrows():
        key = row["state_key"]
        value = row["state_value"]
        if key == "execution_mode":
            data["execution_mode"] = value
        elif key.startswith("platform.pause.") and value == "1":
            data.setdefault("paused_platforms", []).append(key.split(".", 2)[-1])
        elif key.startswith("telegram.digest"):
            data[key] = value
    return data


def queue_snapshot(limit: int = 20) -> pd.DataFrame:
    _ensure_proposals_schema()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            candidate_id,
            platform,
            title,
            ai_score,
            ai_score_source,
            vet_score,
            risk_level,
            priority,
            offers_count,
            client_hired_percent,
            proposal_text,
            search_query,
            updated_at,
            status
        FROM candidates
        WHERE status IN ('queued', 'snoozed', 'auto_ready')
        ORDER BY
            CASE status WHEN 'auto_ready' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
            priority DESC,
            updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )


def decisions_timeline(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            DATE(created_at) AS day,
            action,
            COUNT(*) AS total
        FROM candidate_actions
        WHERE created_at >= datetime('now', ?)
        GROUP BY day, action
        ORDER BY day, action
        """,
        (f"-{days} days",),
    )


def candidate_status_breakdown(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT status, COUNT(*) AS total
        FROM candidates
        WHERE updated_at >= datetime('now', ?)
        GROUP BY status
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    )


def scoring_quality(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(ai_score_source, 'unknown') AS ai_score_source,
            COUNT(*) AS total,
            ROUND(AVG(COALESCE(ai_score, 0)), 2) AS avg_ai_score,
            ROUND(AVG(COALESCE(vet_score, 0)), 2) AS avg_vet_score,
            SUM(CASE WHEN status IN ('auto_sent', 'manual_sent') THEN 1 ELSE 0 END) AS shipped,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
        FROM candidates
        WHERE updated_at >= datetime('now', ?)
        GROUP BY COALESCE(ai_score_source, 'unknown')
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    )


def proposal_outcomes(days: int = 30) -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(decision_source, 'unknown') AS decision_source,
            COUNT(*) AS total,
            SUM(CASE WHEN response IS NOT NULL AND response != '' THEN 1 ELSE 0 END) AS responses,
            ROUND(
                100.0 * SUM(CASE WHEN response IS NOT NULL AND response != '' THEN 1 ELSE 0 END) / COUNT(*),
                1
            ) AS response_rate
        FROM proposals
        WHERE sent_at >= datetime('now', ?)
        GROUP BY COALESCE(decision_source, 'unknown')
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    )


def query_memory_top(limit: int = 20) -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            platform,
            query_text,
            runs,
            shortlisted_count,
            auto_ready_count,
            sent_count,
            responded_count,
            skipped_count,
            user_preferred
        FROM query_memory
        ORDER BY
            user_preferred DESC,
            responded_count DESC,
            sent_count DESC,
            shortlisted_count DESC,
            runs DESC
        LIMIT ?
        """,
        (limit,),
    )


def proposals_by_hour() -> pd.DataFrame:
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            CAST(strftime('%H', sent_at) AS INTEGER) AS hour,
            COUNT(*) AS sent,
            SUM(CASE WHEN response IS NOT NULL AND response != '' THEN 1 ELSE 0 END) AS responded
        FROM proposals
        GROUP BY hour
        ORDER BY hour
        """,
    )


def breaker_snapshot() -> pd.DataFrame:
    return _read_sql(
        LOGS_DB,
        """
        SELECT
            key,
            state,
            consecutive_failures,
            consecutive_opens,
            paused_seconds_left,
            last_error,
            updated_at
        FROM breaker_state
        WHERE key NOT LIKE 'test%'
        ORDER BY key
        """,
    )


def provider_generation_stats(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        LOGS_DB,
        """
        SELECT
            provider,
            COUNT(*) AS calls,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS err,
            ROUND(AVG(duration_ms), 0) AS avg_ms
        FROM generation_logs
        WHERE timestamp >= datetime('now', ?)
        GROUP BY provider
        ORDER BY calls DESC
        """,
        (f"-{days} days",),
    )


def parse_stats(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        LOGS_DB,
        """
        SELECT
            platform,
            COUNT(*) AS runs,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS err,
            SUM(projects_count) AS projects,
            ROUND(AVG(duration_ms), 0) AS avg_ms
        FROM parse_logs
        WHERE timestamp >= datetime('now', ?)
        GROUP BY platform
        ORDER BY projects DESC
        """,
        (f"-{days} days",),
    )


def send_stats(days: int = 14) -> pd.DataFrame:
    return _read_sql(
        LOGS_DB,
        """
        SELECT
            platform,
            status,
            COUNT(*) AS total,
            ROUND(AVG(duration_ms), 0) AS avg_ms
        FROM send_logs
        WHERE timestamp >= datetime('now', ?)
        GROUP BY platform, status
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    )


# ---------------------------------------------------------------------------
# Phase 3 — feedback loop / conversion analytics
# ---------------------------------------------------------------------------


def _conversion_columns_available() -> bool:
    """Старые БД могут не иметь новых колонок Phase 3.

    `_ensure_column` мигрирует их при первом подключении ProposalDB,
    но dashboard читает ту же БД sqlite напрямую. Если запустить
    дашборд раньше orchestrator на чистой машине — колонок может
    не быть. Этот гард не даёт SQL-ошибкам положить страницу.
    """
    if not Path(PROPOSALS_DB).exists():
        return False
    try:
        with sqlite3.connect(PROPOSALS_DB) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    except sqlite3.Error:
        return False
    return {
        "sent_at", "status", "replied_at", "reply_classification",
        "won", "revenue", "prompt_variant", "provider", "search_query",
        "offers_count", "currency",
    }.issubset(cols)


# One sent-date cohort for all phase-3 breakdowns. A candidate remains in the
# cohort after hire/completion; editing or classifying it does not move it into
# another period. Drafts have sent_at in some legacy data, so status matters.
_CONVERSION_COHORT = """
    c.sent_at IS NOT NULL AND trim(c.sent_at) != ''
    AND c.sent_at >= datetime('now', ?)
    AND c.status IN ('auto_sent', 'manual_sent', 'hired', 'declined', 'completed')
"""


def conversion_summary(days: int = 30) -> dict[str, float]:
    """Сводка по Phase 3: сколько отправлено / получено реплаев / выиграно.

    `revenue` — float (рубли с дробью), остальные счётчики хранятся как
    `int`, но возвращаются в общем dict[str, float], чтобы mypy был
    happy без union-типов на словаре.
    """
    out: dict[str, float] = {"sent": 0, "replied": 0, "won": 0, "revenue": 0.0}
    if not _conversion_columns_available():
        return out
    df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COUNT(*) AS sent,
            SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            COALESCE(SUM(CASE WHEN c.won = 1 AND upper(c.currency) = 'RUB' THEN c.revenue ELSE 0 END), 0) AS revenue
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT,
        (f"-{days} days",),
    )
    if df.empty:
        return out
    row = df.iloc[0]
    out["sent"] = int(row.get("sent", 0) or 0)
    out["replied"] = int(row.get("replied", 0) or 0)
    out["won"] = int(row.get("won", 0) or 0)
    out["revenue"] = float(row.get("revenue", 0) or 0)
    return out


def conversion_by_provider(days: int = 30) -> pd.DataFrame:
    """Конверсия по LLM-провайдеру отклика."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(NULLIF(trim(c.provider), ''), 'unknown') AS provider,
            COUNT(*) AS sent,
            SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            COALESCE(SUM(CASE WHEN c.won = 1 AND upper(c.currency) = 'RUB' THEN c.revenue ELSE 0 END), 0) AS revenue,
            ROUND(
                100.0 * SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS reply_rate,
            ROUND(
                100.0 * SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT + """
        GROUP BY COALESCE(NULLIF(trim(c.provider), ''), 'unknown')
        ORDER BY sent DESC
        """,
        (f"-{days} days",),
    )


def conversion_by_niche(days: int = 30, limit: int = 15) -> pd.DataFrame:
    """Конверсия по «нише» — берём search_query как прокси для ниши."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(NULLIF(trim(c.search_query), ''), 'unknown') AS niche,
            COUNT(*) AS sent,
            SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            ROUND(
                100.0 * SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS reply_rate
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT + """
        GROUP BY COALESCE(NULLIF(trim(c.search_query), ''), 'unknown')
        ORDER BY sent DESC
        LIMIT ?
        """,
        (f"-{days} days", limit),
    )


def conversion_by_queue_position(days: int = 30) -> pd.DataFrame:
    """Конверсия по сохранённому offers_count; значение не снимок на момент отправки."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            CASE
                WHEN c.offers_count IS NULL THEN 'неизвестно'
                WHEN c.offers_count = 0 THEN '0'
                WHEN c.offers_count BETWEEN 1 AND 3 THEN '1-3'
                WHEN c.offers_count BETWEEN 4 AND 9 THEN '4-9'
                WHEN c.offers_count BETWEEN 10 AND 19 THEN '10-19'
                ELSE '20+'
            END AS queue_bucket,
            COUNT(*) AS sent,
            SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            ROUND(
                100.0 * SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS reply_rate
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT + """
        GROUP BY queue_bucket
        ORDER BY
            CASE queue_bucket
                WHEN '0' THEN 0
                WHEN '1-3' THEN 1
                WHEN '4-9' THEN 2
                WHEN '10-19' THEN 3
                WHEN '20+' THEN 4
                ELSE 5
            END
        """,
        (f"-{days} days",),
    )


def conversion_by_response_time(days: int = 30) -> pd.DataFrame:
    """Время отклика клиента: от sent_at до replied_at, корзинами."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            CASE
                WHEN diff_minutes < 5 THEN '<5 мин'
                WHEN diff_minutes < 30 THEN '5-30 мин'
                WHEN diff_minutes < 120 THEN '30 мин - 2 ч'
                WHEN diff_minutes < 720 THEN '2-12 ч'
                WHEN diff_minutes < 1440 THEN '12-24 ч'
                ELSE '> 1 дня'
            END AS response_bucket,
            COUNT(*) AS replies,
            SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) AS won,
            ROUND(
                100.0 * SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM (
            SELECT
                c.won,
                CAST(
                    (julianday(c.replied_at) - julianday(c.sent_at)) * 24 * 60 AS INTEGER
                ) AS diff_minutes
            FROM candidates c
            WHERE """ + _CONVERSION_COHORT + """
              AND c.replied_at IS NOT NULL AND trim(c.replied_at) != ''
              AND julianday(c.sent_at) IS NOT NULL AND julianday(c.replied_at) IS NOT NULL
              AND julianday(c.replied_at) >= julianday(c.sent_at)
        )
        GROUP BY response_bucket
        ORDER BY
            CASE response_bucket
                WHEN '<5 мин' THEN 0
                WHEN '5-30 мин' THEN 1
                WHEN '30 мин - 2 ч' THEN 2
                WHEN '2-12 ч' THEN 3
                WHEN '12-24 ч' THEN 4
                ELSE 5
            END
        """,
        (f"-{days} days",),
    )


def reply_classification_breakdown(days: int = 30) -> pd.DataFrame:
    """Распределение реплаев по классификации LLM."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(NULLIF(trim(c.reply_classification), ''), 'unclassified') AS classification,
            COUNT(*) AS replies,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            ROUND(
                100.0 * SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT + """
          AND c.replied_at IS NOT NULL AND trim(c.replied_at) != ''
        GROUP BY COALESCE(NULLIF(trim(c.reply_classification), ''), 'unclassified')
        ORDER BY replies DESC
        """,
        (f"-{days} days",),
    )


def conversion_by_prompt_variant(days: int = 30) -> pd.DataFrame:
    """Конверсия по A/B-варианту промпта."""
    if not _conversion_columns_available():
        return pd.DataFrame()
    return _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COALESCE(NULLIF(trim(c.prompt_variant), ''), 'untracked') AS prompt_variant,
            COUNT(*) AS sent,
            SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END) AS replied,
            SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END) AS won,
            ROUND(
                100.0 * SUM(CASE WHEN c.replied_at IS NOT NULL AND trim(c.replied_at) != '' THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS reply_rate,
            ROUND(
                100.0 * SUM(CASE WHEN c.won = 1 THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0),
                1
            ) AS win_rate
        FROM candidates c
        WHERE """ + _CONVERSION_COHORT + """
        GROUP BY COALESCE(NULLIF(trim(c.prompt_variant), ''), 'untracked')
        ORDER BY sent DESC
        """,
        (f"-{days} days",),
    )


def conversion_analytics(days: int = 30) -> dict[str, Any]:
    """One persisted sent-date cohort across summary and all drilldowns."""
    _ensure_proposals_schema()
    available = _conversion_columns_available()
    summary = conversion_summary(days)
    sent = int(summary["sent"])
    replied = int(summary["replied"])
    won = int(summary["won"])
    return {
        "days": days,
        "status": "ready" if available else "unavailable",
        "summary": {
            **summary,
            "reply_rate": round(100 * replied / sent, 1) if sent else 0.0,
            "win_rate": round(100 * won / sent, 1) if sent else 0.0,
        },
        "by_provider": conversion_by_provider(days).to_dict(orient="records"),
        "by_niche": conversion_by_niche(days).to_dict(orient="records"),
        "by_queue_position": conversion_by_queue_position(days).to_dict(orient="records"),
        "by_response_time": conversion_by_response_time(days).to_dict(orient="records"),
        "classification_breakdown": reply_classification_breakdown(days).to_dict(orient="records"),
        "by_prompt_variant": conversion_by_prompt_variant(days).to_dict(orient="records"),
    }


def errors_recent(limit: int = 50) -> pd.DataFrame:
    return _read_sql(
        LOGS_DB,
        """
        SELECT timestamp, module, error_type, message
        FROM errors
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (limit,),
    )


def last_activity() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, db_path, query in [
        ("last_parse", LOGS_DB, "SELECT MAX(timestamp) FROM parse_logs"),
        ("last_generation", LOGS_DB, "SELECT MAX(timestamp) FROM generation_logs"),
        ("last_send", LOGS_DB, "SELECT MAX(timestamp) FROM send_logs"),
        ("last_proposal", PROPOSALS_DB, "SELECT MAX(sent_at) FROM proposals"),
        ("last_candidate", PROPOSALS_DB, "SELECT MAX(updated_at) FROM candidates"),
        ("last_error", LOGS_DB, "SELECT MAX(timestamp) FROM errors"),
        ("last_conversation", PROPOSALS_DB, "SELECT MAX(updated_at) FROM conversations"),
        ("last_earning", PROPOSALS_DB, "SELECT MAX(updated_at) FROM earnings"),
    ]:
        df = _read_sql(db_path, query)
        out[label] = df.iloc[0, 0] if not df.empty and df.iloc[0, 0] else None
    return out


def conversion_funnel(days: int = 30) -> dict[str, Any]:
    _ensure_proposals_schema()
    funnel: dict[str, Any] = {
        "sent": 0,
        "responded": 0,
        "hired": 0,
        "completed": 0,
        "paid": 0,
        "response_rate": 0.0,
        "hire_rate": 0.0,
        "completion_rate": 0.0,
        "payment_rate": 0.0,
    }

    sent_df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT COUNT(*) AS n FROM candidates
        WHERE status IN ('auto_sent', 'manual_sent', 'hired', 'declined', 'completed')
        AND sent_at IS NOT NULL AND sent_at >= datetime('now', ?)
        """,
        (f"-{days} days",),
    )
    if not sent_df.empty:
        funnel["sent"] = int(sent_df.iloc[0]["n"] or 0)

    responded_df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT COUNT(DISTINCT c.candidate_id) AS n
        FROM candidates c
        JOIN conversations conv ON conv.candidate_id = c.candidate_id
        WHERE c.sent_at IS NOT NULL AND c.sent_at >= datetime('now', ?)
          AND conv.status NOT IN ('new')
        """,
        (f"-{days} days",),
    )
    if not responded_df.empty:
        funnel["responded"] = int(responded_df.iloc[0]["n"] or 0)

    hired_df = _read_sql(
        PROPOSALS_DB,
        "SELECT COUNT(*) AS n FROM candidates WHERE status IN ('hired', 'completed') AND sent_at >= datetime('now', ?)",
        (f"-{days} days",),
    )
    if not hired_df.empty:
        funnel["hired"] = int(hired_df.iloc[0]["n"] or 0)

    completed_df = _read_sql(
        PROPOSALS_DB,
        "SELECT COUNT(*) AS n FROM candidates WHERE status = 'completed' AND sent_at >= datetime('now', ?)",
        (f"-{days} days",),
    )
    if not completed_df.empty:
        funnel["completed"] = int(completed_df.iloc[0]["n"] or 0)

    paid_df = _read_sql(
        PROPOSALS_DB,
        "SELECT COUNT(*) AS n FROM earnings WHERE status = 'paid' AND created_at >= datetime('now', ?)",
        (f"-{days} days",),
    )
    if not paid_df.empty:
        funnel["paid"] = int(paid_df.iloc[0]["n"] or 0)

    if funnel["sent"] > 0:
        funnel["response_rate"] = round(100.0 * funnel["responded"] / funnel["sent"], 1)
    if funnel["responded"] > 0:
        funnel["hire_rate"] = round(100.0 * funnel["hired"] / funnel["responded"], 1)
    if funnel["hired"] > 0:
        funnel["completion_rate"] = round(100.0 * funnel["completed"] / funnel["hired"], 1)
    if funnel["completed"] > 0:
        funnel["payment_rate"] = round(100.0 * funnel["paid"] / funnel["completed"], 1)

    return funnel


def earnings_summary() -> dict[str, Any]:
    _ensure_proposals_schema()
    df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
            COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) AS paid_amount,
            COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0) AS pending_amount,
            COALESCE(SUM(amount), 0) AS total_amount
        FROM earnings
        """,
    )
    if df.empty:
        return {
            "total": 0,
            "paid_count": 0,
            "pending_count": 0,
            "paid_amount": 0.0,
            "pending_amount": 0.0,
            "total_amount": 0.0,
        }
    row = df.iloc[0]
    return {
        "total": int(row["total"] or 0),
        "paid_count": int(row["paid_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
        "paid_amount": float(row["paid_amount"] or 0),
        "pending_amount": float(row["pending_amount"] or 0),
        "total_amount": float(row["total_amount"] or 0),
    }


def active_conversations(limit: int = 20) -> list[dict[str, Any]]:
    _ensure_proposals_schema()
    df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT
            c.conversation_id,
            c.candidate_id,
            c.project_id,
            c.platform,
            c.project_title,
            c.status,
            c.created_at,
            c.last_message_at,
            c.updated_at,
            (SELECT COUNT(*) FROM conversation_messages cm WHERE cm.conversation_id = c.conversation_id) AS message_count
        FROM conversations c
        WHERE c.status NOT IN ('completed', 'declined')
        ORDER BY c.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return df.to_dict(orient="records") if not df.empty else []


def conversation_history(project_id: str, platform: str) -> list[dict[str, Any]]:
    _ensure_proposals_schema()
    df = _read_sql(
        PROPOSALS_DB,
        """
        SELECT cm.message_id, cm.sender, cm.message_text, cm.created_at
        FROM conversation_messages cm
        JOIN conversations c ON c.conversation_id = cm.conversation_id
        WHERE c.project_id = ? AND c.platform = ?
        ORDER BY cm.created_at ASC
        """,
        (project_id, platform),
    )
    return df.to_dict(orient="records") if not df.empty else []
