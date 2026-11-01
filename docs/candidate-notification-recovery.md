# Candidate notification delivery and recovery

This applies to **the cross-platform orchestrator** (`ProposalDB.candidates` for
Kwork, Freelance.ru and HH.ru). Kwork Buyer Search has a distinct repository,
run-event stream and WebSocket protocol; this outbox does not merge them.

## Commit and delivery boundaries

When the candidate pipeline changes a project to `queued` or `auto_ready`, the
same SQLite transaction changes the candidate, records its status/action and
inserts one `candidate_notification_outbox` event. The unique
`(candidate_id, event_type)` key is retained as a tombstone after cleanup, so
repeat pages, cycles and concurrent inserts cannot schedule the ordinary alert
again. `parsed`, `filtered`, `scored` and `vetted` are *not* treated as qualified
alerts; they may still appear in the separate status event journal.

The dispatcher claims at most a bounded batch with an SQLite `BEGIN IMMEDIATE`
lease, unique owner and fencing token. Each SQLite connection is short-lived;
network calls are **outside** all database transactions. SQLite calls are
offloaded with `asyncio.to_thread` so a busy connection doesn't stall the
FastAPI event loop. Pending and expired leases can be recovered on restart;
failures use bounded exponential backoff and a terminal `dead` state after the
maximum attempts. An in-process async lock prevents competing API/orchestrator
consumers from sending concurrently, and database fencing handles competing
processes.

The candidate worker attempts early delivery as each page is processed. An
independent bounded API recovery task also wakes periodically (default 10s),
including when no scanning cycle is running. If Telegram is not configured,
events remain pending instead of being falsely acknowledged. A successful
`TelegramNotifier.notify_candidate()` return means **Telegram accepted the
primary message request**, not that the operator received or read it.

Two important crash windows have different guarantees:

1. **After candidate commit, before notification:** the pending outbox entry
   survives restart and is retried.
2. **After the Telegram API accepts the message, before outbox ACK:** a lost
   ACK leads to re-delivery after lease expiry. Telegram has no applicable
   idempotency key in this path; external exactly-once delivery is not claimed.

For a hard crash *before the first candidate transaction commits*, pages still
in an in-memory bounded discovery queue are not durable and must be fetched
again in a later scan. That is not solved by the notification outbox.

## Operator state and replay

`candidate_event_journal` has a monotonic sequence number and a durable
per-database `stream_id`. It includes candidate status transitions and outbox
changes (`pending`, `leased`, `retry`, `provider_accepted`, `failed`, `cancelled`).
`GET /api/dashboard/candidate-events` provides a bounded read-only REST replay
with a cursor and explicit reset when the database generation changes or old
journal history has expired. Dashboard polling does not infer qualification
from an initial `parsed` insert or confuse provider acceptance with device
delivery. It also does not claim to receive Buyer Search run events.

The API recovery task cleans terminal notification metadata and journal rows
older than 30 days; it retains minimal idempotency tombstones. Clients with an
outdated cursor must resynchronize their snapshot instead of silently skipping
events. Persisted status and notification state remain queryable across
WebSocket or Electron process restarts.

## Verification and constraints

Offline tests use temporary real SQLite databases, mock Telegram and mock
platform pages to cover atomic rollback, cross-source ID collisions, duplicate
pages, leases and fencing, crash-recovery windows, retries/dead letters,
provider rejection, cancellation, event replay/reset, and bounded page
discovery. Do not run production Electron/uvicorn as a smoke test without an
isolated profile: its normal startup may revive live Buyer Search workers and
perform real platform traffic.

```powershell
python -m pytest tests/unit -q --tb=line
python -m ruff check src main.py tests/unit
cd desktop
cmd /c node_modules\.bin\tsc.cmd --noEmit --incremental false -p tsconfig.json
cmd /c node_modules\.bin\vite.cmd build
```

`first_persisted_ms` and `first_notifier_call_completed_ms` are local pipeline
timings, **not** real Telegram receipt or Electron paint latency. An isolated
benchmark with a deliberately slow mock Kwork page measures pipeline order,
not real-site latency or rate-limit behavior.
