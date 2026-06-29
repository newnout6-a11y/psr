"""
Streamlit dashboard vNext.

Запуск:
    streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src.dashboard import queries as q  # noqa: E402

st.set_page_config(
    page_title="PSR vNext",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        delta = datetime.now() - dt
        if delta.days > 0:
            return f"{delta.days} d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours} h ago"
        minutes = (delta.seconds % 3600) // 60
        return f"{minutes} min ago"
    except Exception:
        return ts


def _empty(df: pd.DataFrame, text: str = "Нет данных") -> bool:
    if df is None or df.empty:
        st.info(text)
        return True
    return False


st.sidebar.title("PSR vNext")
page = st.sidebar.radio(
    "Page",
    ["Overview", "Decision Quality", "System Health"],
    label_visibility="collapsed",
)
days = st.sidebar.slider("Период, дней", 1, 60, 14)

runtime = q.runtime_state_snapshot()
activity = q.last_activity()
st.sidebar.markdown("---")
st.sidebar.caption(f"Mode: {runtime.get('execution_mode', 'semi_auto')}")
paused = runtime.get("paused_platforms", [])
st.sidebar.caption(f"Paused: {', '.join(paused) if paused else 'none'}")
st.sidebar.caption(f"Last parse: {_fmt_ts(activity.get('last_parse'))}")
st.sidebar.caption(f"Last send: {_fmt_ts(activity.get('last_send'))}")
st.sidebar.caption(f"Last candidate: {_fmt_ts(activity.get('last_candidate'))}")


if page == "Overview":
    st.title("Overview")

    metrics = q.overview_metrics(days)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Parsed", metrics["parsed"])
    c2.metric("Queued", metrics["queued"])
    c3.metric("Manual sent", metrics["manual_sent"])
    c4.metric("Auto sent", metrics["auto_sent"])
    c5.metric("Drafts", metrics["draft"])
    c6.metric("Responses", metrics["responses"])

    st.markdown("---")
    left, right = st.columns([1.2, 1])

    with left:
        st.subheader("Queue Snapshot")
        queue = q.queue_snapshot(limit=15)
        if not _empty(queue, "Очередь пуста"):
            st.dataframe(
                queue.rename(
                    columns={
                        "candidate_id": "ID",
                        "platform": "Platform",
                        "title": "Title",
                        "ai_score": "AI",
                        "ai_score_source": "AI Source",
                        "vet_score": "Vet",
                        "risk_level": "Risk",
                        "priority": "Priority",
                        "offers_count": "Offers",
                        "search_query": "Query",
                        "updated_at": "Updated",
                        "status": "Status",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

    with right:
        st.subheader("Decision Timeline")
        timeline = q.decisions_timeline(days)
        if not _empty(timeline, "Пока нет действий по кандидатам"):
            pivot = timeline.pivot(index="day", columns="action", values="total").fillna(0)
            st.area_chart(pivot)


elif page == "Decision Quality":
    st.title("Decision Quality")

    top_left, top_right = st.columns(2)
    with top_left:
        st.subheader("Scoring Quality")
        scoring = q.scoring_quality(days)
        if not _empty(scoring, "Нет scoring-данных"):
            st.dataframe(
                scoring.rename(
                    columns={
                        "ai_score_source": "Source",
                        "total": "Total",
                        "avg_ai_score": "Avg AI",
                        "avg_vet_score": "Avg Vet",
                        "shipped": "Shipped",
                        "skipped": "Skipped",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

    with top_right:
        st.subheader("Candidate Statuses")
        statuses = q.candidate_status_breakdown(days)
        if not _empty(statuses, "Нет статусов за период"):
            st.dataframe(
                statuses.rename(columns={"status": "Status", "total": "Total"}),
                hide_index=True,
                width="stretch",
            )
            st.bar_chart(statuses.set_index("status")["total"])

    st.markdown("---")
    lower_left, lower_right = st.columns(2)

    with lower_left:
        st.subheader("Proposal Outcomes")
        outcomes = q.proposal_outcomes(days)
        if not _empty(outcomes, "Нет отправленных откликов"):
            st.dataframe(
                outcomes.rename(
                    columns={
                        "decision_source": "Decision",
                        "total": "Total",
                        "responses": "Responses",
                        "response_rate": "Response Rate %",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        st.subheader("Best Hours")
        by_hour = q.proposals_by_hour()
        if not _empty(by_hour, "Пока мало данных по часу отправки"):
            by_hour["response_rate"] = (
                (by_hour["responded"] / by_hour["sent"].replace({0: pd.NA}) * 100).fillna(0).round(1)
            )
            st.line_chart(by_hour.set_index("hour")["response_rate"])

    with lower_right:
        st.subheader("Query Memory")
        memory = q.query_memory_top(limit=20)
        if not _empty(memory, "Query memory пока пустая"):
            st.dataframe(
                memory.rename(
                    columns={
                        "platform": "Platform",
                        "query_text": "Query",
                        "runs": "Runs",
                        "shortlisted_count": "Shortlisted",
                        "auto_ready_count": "Auto Ready",
                        "sent_count": "Sent",
                        "responded_count": "Responded",
                        "skipped_count": "Skipped",
                        "user_preferred": "Preferred",
                    }
                ),
                hide_index=True,
                width="stretch",
            )


elif page == "System Health":
    st.title("System Health")

    c1, c2, c3 = st.columns(3)
    c1.metric("Mode", runtime.get("execution_mode", "semi_auto"))
    c2.metric("Paused platforms", len(runtime.get("paused_platforms", [])))
    c3.metric("Last error", _fmt_ts(activity.get("last_error")))

    st.markdown("---")
    left, right = st.columns(2)

    with left:
        st.subheader("Circuit Breaker")
        breaker = q.breaker_snapshot()
        if not _empty(breaker, "Persisted breaker state пока пуст"):
            st.dataframe(
                breaker.rename(
                    columns={
                        "key": "Key",
                        "state": "State",
                        "consecutive_failures": "Failures",
                        "consecutive_opens": "Opens",
                        "paused_seconds_left": "Pause Left",
                        "last_error": "Last Error",
                        "updated_at": "Updated",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        st.subheader("Provider Generation")
        generation = q.provider_generation_stats(days)
        if not _empty(generation, "Нет generation-логов"):
            st.dataframe(
                generation.rename(
                    columns={
                        "provider": "Provider",
                        "calls": "Calls",
                        "ok": "OK",
                        "err": "ERR",
                        "avg_ms": "Avg ms",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

    with right:
        st.subheader("Parse Stats")
        parse = q.parse_stats(days)
        if not _empty(parse, "Нет parse-данных"):
            st.dataframe(
                parse.rename(
                    columns={
                        "platform": "Platform",
                        "runs": "Runs",
                        "ok": "OK",
                        "err": "ERR",
                        "projects": "Projects",
                        "avg_ms": "Avg ms",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

        st.subheader("Send Stats")
        send = q.send_stats(days)
        if not _empty(send, "Нет send-логов"):
            st.dataframe(
                send.rename(
                    columns={
                        "platform": "Platform",
                        "status": "Status",
                        "total": "Total",
                        "avg_ms": "Avg ms",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

    st.markdown("---")
    st.subheader("Recent Errors")
    errors = q.errors_recent(limit=30)
    if not _empty(errors, "Ошибок нет"):
        st.dataframe(
            errors.rename(
                columns={
                    "timestamp": "When",
                    "module": "Module",
                    "error_type": "Type",
                    "message": "Message",
                }
            ),
            hide_index=True,
            width="stretch",
        )
