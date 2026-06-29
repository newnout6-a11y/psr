"""Централизованные SQL-запросы для dashboard vNext."""

from __future__ import annotations

import sqlite3
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
        return {"total": 0, "paid_count": 0, "pending_count": 0, "paid_amount": 0.0, "pending_amount": 0.0, "total_amount": 0.0}
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
            c.project_id,
            c.platform,
            c.project_title,
            c.status,
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
        SELECT cm.sender, cm.message_text, cm.created_at
        FROM conversation_messages cm
        JOIN conversations c ON c.conversation_id = cm.conversation_id
        WHERE c.project_id = ? AND c.platform = ?
        ORDER BY cm.created_at ASC
        """,
        (project_id, platform),
    )
    return df.to_dict(orient="records") if not df.empty else []
