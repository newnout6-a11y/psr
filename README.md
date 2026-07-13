# PSR

PSR is a Windows-focused control center for finding freelance work, qualifying
opportunities, preparing proposals, and operating a Kwork account. It combines
a Python workflow engine, a FastAPI backend, and a React/Electron desktop app.

The project is designed around a human-reviewed workflow. By default the CLI
runs in dry-run mode, and the desktop app exposes the queue, account health,
logs, Kwork tools, and operational settings.

## What PSR Does

### Opportunity workflow

- collects projects from Kwork, Freelance.ru, and HH.ru;
- generates search queries from a natural-language brief;
- filters projects by keywords, budget, age, client signals, and AI score;
- enriches suitable candidates with portfolio/RAG context and optional OSINT;
- drafts proposals and prices, then stores candidates and actions in SQLite;
- sends candidates to Telegram for review and supports approval, skip, snooze,
  editing, and lifecycle tracking;
- can submit Kwork offers through the API/web flow when live sending is enabled.

### Kwork Operations

- Kwork web session support through Session Hub, saved manual cookies, or
  configured fallback credentials;
- account, inbox, conversations, orders, reviews, connects, and account-health
  helpers;
- rate pacing, browser fallback, and optional VPN Tunnel Enforcer (VPNTE)
  proxy integration;
- Kwork Market: category supply, competitor cards, price ranges, buyer-project
  signals, seller summaries, buyer history, and rubric-scoped recommendations;
- Kwork service-draft and publishing helpers, including form manifests,
  generated cover assets, dry-run payloads, and explicit publishing actions.

### Desktop App

The desktop app provides Dashboard, Queue, Skipped, Conversations, Orders,
Kwork Market, Earnings, Health, Chat, Settings, Logs, and OSINT views. It starts
and checks the local backend, and detects a stale PSR backend before opening the
interface.

## Supported Platforms

| Platform | Current role |
| --- | --- |
| Kwork | Search, qualification, proposal workflow, account operations, market analysis, and supported offer/service flows. |
| Freelance.ru | HTML discovery and browser-assisted reply workflow. |
| HH.ru | Public API discovery and monitoring; it is not an automatic response channel. |

## Architecture

```text
main.py                         CLI cycle runner
src/orchestrator.py             Discovery -> filtering -> scoring -> proposal -> action workflow
src/api/server.py               FastAPI backend on 127.0.0.1:7788
src/api/routes/                 Backend routes used by the desktop UI
src/platforms/                  Kwork, market analysis, listing, and platform integrations
src/action/                     Proposal generation, sending, SQLite persistence
src/brain/                      LLM router, query strategy, RAG, scoring
src/browser/                    Browser management, fingerprinting, session handling
src/utils/                      Telegram, logs, VPNTE, scheduling, safety helpers
desktop/                        React, Vite, Electron desktop client
config/                         Runtime filters and platform descriptions
data/reference/                 Portfolio, cases, and other durable reference data
data/runtime/                   SQLite DBs, browser profiles, logs, assets, runtime state
data/debug/                     Debug HTML, screenshots, and diagnostics
docs/                           Design notes, implementation journals, and research snapshots
tests/                          Unit, smoke, integration, and manual checks
```

The API backend owns the runtime state. The desktop frontend calls the local API
and receives log events through a WebSocket sink.

## Requirements

- Windows 10 or later;
- Python 3.11+; the local development environment currently uses Python 3.14;
- Node.js and npm for the desktop app;
- a Kwork account/session only for Kwork features that require authentication;
- optionally, a local Session Hub and VPN Tunnel Enforcer installation.

## Setup

1. Create local configuration from the template:

```powershell
Copy-Item .env.example .env
```

2. Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Install a browser engine if browser-based Kwork or Freelance.ru flows are
needed:

```powershell
python -m playwright install chromium
```

4. Install the desktop dependencies:

```powershell
Set-Location desktop
npm ci
Set-Location ..
```

5. Configure the minimum required values in `.env`:

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=
TELEGRAM_TOKEN=
ADMIN_CHAT_ID=
PLATFORMS=kwork,freelance_ru,hh_ru
SEARCH_BRIEF=
```

`GROQ_API_KEY` and DeepSeek/OpenAI-compatible provider settings are supported as
alternatives. See `.env.example` for the complete provider, scoring, browser,
Telegram, OSINT, proxy, and Kwork configuration.

## Running PSR

### CLI workflow

Run one safe cycle. `main.py` defaults to dry-run mode:

```powershell
python main.py --dry-run --limit 3
```

Useful options:

```powershell
# Use a one-off search brief.
python main.py --dry-run --search "Telegram bots and small automation tasks" --top 5

# Keep running on the configured cycle interval.
python main.py --continuous --dry-run

# Allow live offer sending. Use only after sessions and settings are verified.
python main.py --live --limit 1
```

### Backend API

Start the API separately when working with the web/desktop control plane:

```powershell
python -m src.api.server
```

It listens on `http://127.0.0.1:7788`. Confirm that it is running:

```powershell
Invoke-RestMethod http://127.0.0.1:7788/api/health
```

### Desktop development

From the `desktop` directory:

```powershell
npm run dev
```

Electron starts the local backend and opens the desktop app. In development the
app prefers the live repository, so it uses the repository `.env` and runtime
data. To create a Windows installer:

```powershell
npm run build
```

The packaged output is written to `desktop/dist-electron/`.

## Kwork Session Hub

Session Hub is the preferred source of Kwork browser cookies. PSR expects a
local compatible endpoint:

```text
GET http://127.0.0.1:8669/cookies?domain=kwork.ru
```

Set the endpoint with `SESSION_HUB_URL`. Cookie data is kept local. When
`SESSION_HUB_REQUIRED=true`, PSR refuses to use `.env` cookie fallbacks if the
hub is unavailable.

The repository includes two optional helper scripts under
`scripts/session_hub/`:

- `session_hub_manual.py` stores manually supplied cookies locally;
- `session_hub_example.py` demonstrates a browser-cookie reader.

They are not part of `requirements.txt`. The manual helper needs Flask; the
example also needs `browser-cookie3`:

```powershell
python -m pip install flask browser-cookie3
python scripts\session_hub\session_hub_manual.py
```

For the normal desktop workflow, use the installed Session Hub service rather
than copying cookie values into project files.

## VPN Tunnel Enforcer (VPNTE)

PSR can send Kwork traffic through VPNTE's stable local proxy while VPNTE rotates
the underlying profile. The current local endpoints are:

```text
Control API: http://127.0.0.1:17873
Application proxy: http://127.0.0.1:17990
```

Typical configuration:

```env
VPNTE_PROXY_ENABLED=true
VPNTE_PROXY_STRICT=true
VPNTE_CONTROL_URL=http://127.0.0.1:17873
VPNTE_PROXY_SLOT=
VPNTE_PROXY_PORT=17990
VPNTE_PROXY_CACHE_TTL=60
```

With `VPNTE_PROXY_STRICT=true`, Kwork traffic stops if the VPNTE control API is
unavailable instead of falling back to a direct connection. This is the expected
setting when Kwork requests must use VPNTE. The Kwork Market screen is a
read-only analytics path and uses a direct connection by default; opt into the
market proxy separately with `KWORK_MARKET_USE_PROXY=true`.

## Runtime Data and Configuration

- `config/filters.yaml` defines the default skills, exclusions, budget range,
  project age, proposal cap, and client-score threshold.
- `config/platforms.yaml` records supported platform endpoints and integration
  styles.
- `data/reference/` holds durable portfolio and case data used for matching and
  proposal context.
- `data/runtime/` is generated at runtime and contains SQLite databases, logs,
  browser profiles, parsing results, generated proposal assets, and saved
  manual Kwork cookies.
- `data/debug/` contains disposable diagnostics such as screenshots and HTML
  dumps.

Use `PSR_ROOT`, `PSR_DATA_DIR`, and `PSR_REFERENCE_DIR` only when the default
repository-relative layout is unsuitable.

## Kwork Market and Service Publishing

Kwork Market is not a generic market crawler. It works with selected Kwork
rubrics and uses bounded, cached calls to build a practical decision view:

- supply and competitor samples for a category/classifier;
- price ranges and optional category price rules;
- buyer project signals, offer counts, budgets, and buyer history;
- seller concentration, portfolio/review summaries, and optional account
  context;
- Russian recommendations derived from the selected rubric's own data.

Heavy options such as buyer details, seller details, price rules, and account
context are explicit UI controls because they increase upstream latency.

Service publishing is intentionally separate from the market scan. The desktop
app can build a form manifest, create drafts, validate payloads, and run a
dry-run before a real publishing action. A valid authenticated Kwork web session
is required for live publishing.

## Tests and Checks

Run the offline unit suite:

```powershell
python -m pytest tests\unit -q
```

Tests are deliberately separated by execution requirements:

| Suite | Purpose |
| --- | --- |
| `tests/unit/` | Fast mocked checks of core logic, Kwork modules, routes, market analysis, and VPNTE helpers. |
| `tests/smoke/` | Broader feature checks that may need local runtime state. |
| `tests/integration/` | Integration coverage for external-facing components. |
| `tests/manual/` | Manual/live checks that can require credentials, browser state, or network access. |

Run lint before shipping Python changes:

```powershell
python -m ruff check src main.py
```

Do not treat a plain `pytest` run as a quick local check: it also collects
manual and integration tests that can depend on external services.

## Documentation

`docs/` contains both durable technical notes and time-stamped research output.
Start with these documents:

- `docs/kwork_market_api_integration_backlog.md` for implemented market API
  coverage and remaining work;
- `docs/kwork_market_runtime_fix_journal.md` for desktop/backend, performance,
  and proxy behavior;
- `docs/kwork_autopublish_progress.md` for service-publishing progress;
- `docs/kwork_hidden_api_facts.md` for confirmed Kwork integration facts;
- `docs/follow_up_automation_design.md` for the planned follow-up system.

The many JSON files in `docs/` and `docs/kwork_market_snapshots/` are evidence
snapshots from probes and market runs. They are useful for reproducing findings,
but they are not required to install or run PSR.

## Operational Notes

- Keep `.env`, Session Hub cookies, VPNTE control tokens, and runtime databases
  local. Do not commit them.
- Verify the Kwork session and VPNTE status before enabling live Kwork actions.
- Use dry-run for changes to filters, LLM prompts, proxy settings, or Kwork
  workflows before enabling `--live`.
- Restart the backend after backend or configuration changes; the Electron app
  also checks for a stale PSR backend on port `7788`.
