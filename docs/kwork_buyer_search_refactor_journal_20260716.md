# Kwork Buyer Search Refactor: research journal

Date: 2026-07-16
Status: research and implementation plan complete
Scope: implementation planning only; no changes to the VPNTEN codebase

## Goal

Prepare an evidence-backed implementation plan for a separate buyer-search workspace that can coordinate up to 30 Kwork account workers, assign unique AI-generated searches, collect buyer projects into durable storage, expose the whole process in the desktop UI, export full project datasets, generate attachment-aware proposals, send selected proposals, and continue the resulting conversations with AI assistance.

## Hard constraints

- One worker is one Kwork account and must retain account identity through the whole operation.
- A verified/signup IP is a preference, not a hard account lock. The allocator must try the retained route, then the free verified/signup IP, then the preferred slot, then any other healthy free route with a unique egress IP. An IP already leased by another worker must never make the account wait while another unique route is available.
- Target fan-out is 30 simultaneous search requests through 30 distinct network routes/IP addresses when VPNTEN reports that capacity as healthy.
- Search workers collect and persist data only. Sending proposals is a separate, explicitly confirmed action.
- VPNTEN internals are out of scope. The final plan must contain a precise external contract and a separate VPNTEN change request list.
- All query batches assigned to workers must be globally deduplicated inside a run and auditable after the run.
- Category-first discovery must work without a user-supplied keyword.
- The UI must expose real worker, transport, query, request, result, error, and retry state rather than simulated progress.
- Existing user changes in the dirty worktree must not be reverted or rewritten.

## Evidence streams

- [x] Audit all buyer/search/demand/category/API evidence under `docs/`.
- [x] Map the current durable market job, shard, worker, transport, repository, export, and event-stream implementation.
- [x] Map the current proposal generation, attachment ingestion, proposal sending, conversation sync, and AI reply paths.
- [x] Map the current desktop navigation, Kwork market workspace, chat UI, API client, and reusable components.
- [x] Translate the two supplied design references into an operational desktop design language.
- [x] Identify facts that are live-verified, snapshot-only, inferred, stale, or still unverified.
- [x] Produce a phased implementation plan with schemas, APIs, UI states, tests, rollout gates, migration, and rollback.
- [x] Produce a concrete VPNTEN requirements list without changing VPNTEN code.

## Initial findings

- The repository already contains a substantial durable market control plane under `src/platforms/kwork_supply/`, REST/event routes in `src/api/routes/kwork_market_jobs.py`, and a desktop Kwork market feature under `desktop/src/features/kwork-market/`.
- The existing market control plane was designed mainly around supply/catalog collection. The buyer-project workflow must be modeled as a separate product surface while reusing durable jobs, leases, workers, transports, events, exports, and operational views.
- Existing buyer research is extensive and includes mobile API probes, authenticated web `/projects` state extraction, filter matrices, captcha diagnostics, query suggestion probes, category taxonomy, and history snapshots.
- Existing proposal generation already extracts bounded text from some project attachments, but the target requires a broader multimodal attachment pipeline with file provenance, parsing status, OCR/vision, safety limits, and cached artifacts.
- Existing conversation routes already sync Kwork dialogs, persist messages, generate AI drafts, and send messages. The buyer-search plan should extend this path instead of creating a parallel chat database.
- The reference visual language combines dense editorial grids, black/off-white surfaces, chartreuse primary accents, coral/cyan semantic accents, thin borders, large numeric emphasis, and monospace operational labels. The buyer-search tab should apply this language to a dense control surface rather than a marketing hero.

## Research log

### 2026-07-16 - kickoff

- Confirmed the goal-backed task and preserved the existing active goal.
- Verified that `C:\psr` is indexed by codebase-memory-mcp, detected a stale index, and rebuilt the full repository index with a persisted graph artifact.
- Enumerated the Kwork API, buyer-analysis, market-control, worker, snapshot, and journal documents under `docs/`.
- Inspected both supplied design references at original resolution.
- Started three parallel audits: Kwork API evidence, backend architecture, and frontend/UX.
- Confirmed a dirty worktree containing unrelated user changes; this task will add documentation only.
- Ran a current Tavily check against public Kwork surfaces. Public search results confirm the exchange/project concept and project filtering, but they do not replace the repository's authenticated probe evidence.

## Resolved research questions

- Canonical fast collector: mobile `POST /projects`. Authenticated web `/projects` and mobile `/want` are selective enrichment sources.
- Confirmed remote filters: category, query, page, budget range, buyer hiring range, and offer-count range. Classifier/attributes remain planner/local filters until a live server contract is proven.
- Views are canonical from `/want.views` for the first implementation; web `views_dirty` remains a separately stored field until a same-project comparison gate is complete.
- Proposal count is available as mobile `offers` and web `kwork_count`, with source and freshness retained.
- A VPNTEN slot may be rebound after an explicit lease release/drain. Account affinity is a preference; exclusivity of current account and egress IP is the invariant.
- Generic jobs, workers, operations, attempts, commands, events, identity and transports are reused. Buyer projects, queries, observations, attachments, scores, exports, outreach and conversations receive buyer-specific tables.
- The strongest proposal/chat model is selected by task-specific runtime configuration and recorded as the resolved provider/model. The intended alias is `gpt-5.6-sol`.
- Attachment download and outgoing upload contracts still require controlled live gates. The implementation plan includes a capability matrix and feature flags rather than assuming unsupported mutations.

## Decision log

- Decision: keep buyer discovery and proposal sending as separate state machines.
  Reason: collection can be highly parallel and retryable, while sending has account quota, duplicate-send, pricing, confirmation, and reputational consequences.
- Decision: plan for reuse of the durable market control plane through explicit source/workflow types rather than copy-pasting another worker system.
  Reason: leases, commands, attempts, event streaming, checkpoints, exports, and transport health already exist and are tested.
- Decision: preserve raw evidence and normalized records.
  Reason: hidden endpoints and response shapes can drift; reparsing must not require repeating every remote request.
- Decision: treat account signup/verified IP as routing priority only.
  Reason: the required invariant is exclusive account and exclusive current egress IP, not permanent account-to-IP pinning. The current `MarketIdentityPool._route_candidates()` ordering already follows retained -> preferred IP -> preferred slot -> fallback and excludes occupied transports/IPs; the refactor must preserve and test this behavior.

## Consolidated findings

### Buyer API

- `POST /projects` returns project cards plus paging totals and should be the first-wave request. Calling `/getWantsCount` first would add avoidable latency.
- `/want` adds views, orders, views history and lifecycle dates.
- Authenticated web `/projects` state provides full descriptions, `files[]`, `views_dirty`, offer count and web lifecycle fields.
- Discovery and enrichment must be separate. Historical measurements show a fast scout at roughly 13 seconds and an enriched run at roughly 44 seconds for much smaller workloads.
- The read-only scanner must explicitly deny offer, message, hide, review, restart and add-view mutations.
- Successful create-offer, nonempty incoming project files, outgoing upload, inbox mutation and remote offer reconciliation still need controlled live capture.

### Runtime

- The current durable market runtime is a strong reusable kernel, but its operation kinds, mapper and analyzer are seller-domain specific.
- `MarketIdentityPool._route_candidates()` already implements retained -> signup IP -> preferred slot -> fallback and excludes occupied IPs/transports.
- `commit_accepted_batch` needs lease owner, attempt and fence validation to prevent stale commits after reassignment.
- Long handlers renew operation leases but can outlive the identity lease. Operation and identity heartbeat must be one lifecycle.
- The 250 ms idle leasing loop can create approximately 600 SQLite write-lock transactions per second with 30 idle workers. It must become event-driven with throttled state persistence and batched writes.
- Synchronous VPNTEN refresh must not block the asyncio event loop.

### Current capacity

- The local DB contains 40 activated market accounts.
- It contains 121 transport rows, but only 12 healthy rows with a nonempty verified egress and only 10 unique verified egress IPs.
- Therefore the current real simultaneous capacity is 10, not 30. The plan requires 30 fresh healthy unique egress IPs, not merely 30 slot records.

### Proposal and chat

- Current proposal attachment extraction handles only a bounded subset of PDF/text files and lacks image vision/OCR and Office formats.
- Current sender discards attachments and uses a global session.
- A crash after platform acceptance but before local commit can lead to a duplicate blind retry.
- Conversations are not keyed by sender account, and the inbox watermark is global.
- `reply_linker` references ProposalDB methods that do not exist.
- The target uses versioned drafts, account-scoped preflight/send, outbox/idempotency, unknown-outcome reconciliation, per-account conversation cursors and a contextual AI sidecar.

### Frontend

- The current buyer scout is embedded in a very large `KworkMarket.tsx` and is not a durable workspace.
- The new bounded context uses `/buyer-search`, run tabs, a server-side project grid, a 30-row execution view, a project inspector and a proposal workspace.
- The backend is the dataset source of truth; localStorage stores only harmless UI preferences.
- The visual language is an operational editorial grid with dark neutral surfaces, chartreuse commands, coral failures and cyan information, thin borders, small radii, Inter UI text and JetBrains Mono telemetry.

## Verification log

### Tests run or confirmed during the audit

- `python -m pytest tests/unit/test_market_identity_pool.py -q`: 8 passed.
- Dashboard conversation tests: 10 passed.
- Proposal attachment tests: 2 passed.
- Market resume integration tests: 2 passed.
- `python -m pytest tests/smoke/test_phase3_feedback_loop.py -q`: 6 failed, 1 passed.

The first meaningful phase-3 smoke failure is the missing `candidates.replied_at` schema field. Later Windows temporary-database lock failures cascade from the first failure. Repairing ProposalDB migrations, reply-linker methods and test cleanup is an explicit Phase 0 task.

## Final artifacts

- Main implementation plan: `docs/kwork_buyer_search_refactor_implementation_plan_20260716.md`.
- Research and decision journal: `docs/kwork_buyer_search_refactor_journal_20260716.md`.

The plan contains the endpoint inventory, trust levels, target architecture, 30-worker algorithm, query planner, database schema, REST/WS contracts, UI design, attachment pipeline, export formats, proposal/chat state machines, VPNTEN change request, phased PR sequence, tests, live verification gates, observability, migration, rollback and Definition of Done.

## Development log

### 2026-07-16 - Phase 0 feedback-loop baseline repair

- Reproduced the planned Phase 0 baseline failure with `python -m pytest tests/smoke/test_phase3_feedback_loop.py -q`: six failures, rooted in missing `candidates.replied_at`; subsequent failures were Windows SQLite file-lock cascades.
- Added idempotent `ProposalDB` migrations for reply timestamp/text/classification, classification timestamp, outcome, and revenue fields.
- Added durable first-reply linking, candidate lookup with project-first and username fallback, reply classification persistence, outcome persistence, and audit actions. Existing send states remain unchanged so conversion metrics retain their sent counts.
- Converted `won` to a boolean in ProposalDB records.
- Closed the smoke test's explicit SQLite connection before temporary database cleanup and released the second ProposalDB instance.
- Verification: `python -m pytest tests/smoke/test_phase3_feedback_loop.py -q` passed, 7 tests; `python -m compileall -q src/action/proposal_db.py src/utils/reply_linker.py` passed.

### 2026-07-16 - Phase 0 runtime fencing and Phase 1 foundation

- Added additive `market_operations.lease_fence` and `leased_attempt_id` migrations. Leasing now increments the fence atomically, and worker-originated accepted batch commits validate worker, attempt, fence, ownership, and non-expired deadline before writing any listing or observation.
- Added a stale-commit regression: after worker A's lease expires and worker B receives the next fence, A cannot write a batch and B retains the lease.
- Unified long-handler lease health: the worker heartbeat renews both the operation fence and the account identity lease; loss of either cancels the handler path. Idle and leasing state writes are coalesced to a ten-second recovery heartbeat, while fallback polling is two seconds rather than 250 ms.
- Moved synchronous transport refresh into `asyncio.to_thread`, preserving the existing VPNTEN adapter contract without modifying VPNTEN itself.
- Added additive `job_kind` and `config_json` fields to the durable market-job schema plus a workflow registry. Existing seller jobs default to `supply`; a buyer-search workflow can use an independent namespace without inheriting seller semantics.
- Added isolated `src/platforms/kwork_buyer` domain foundations: run/query/operation enums, stable exact query identity, deterministic planner, collision report, round-robin assignment, and an account-scoped read-only capability boundary.
- Verification: `python -m pytest tests/unit/test_market_job_repository.py tests/unit/test_market_worker_runtime.py tests/unit/test_market_worker_supervisor.py tests/unit/test_market_workflow_registry.py -q` passed (31 tests across the focused runs); buyer normalizer, planner, and read-capability tests passed (17 tests at the latest combined agent run).

### 2026-07-16 - Buyer discovery boundary and work execution

- Added a deterministic, side-effect-free candidate generator for brief, category, hybrid, and manual modes. It preserves manual operator queries exactly, makes fallback expansion explicit in rationale, and leaves final exact/semantic deduplication to the central planner.
- Added account-scoped mobile and web page adapters. They accept only `BuyerReadCapabilities`, validate that the leased worker identity matches the capability's account, transport, and egress provenance, and strip web-only filter fields before a mobile request.
- Added a single-flight `BuyerDiscoveryWorker`: it leases a fenced query task, performs exactly one read request, maps source cards to canonical plus immutable observation payloads, atomically commits them, and records durable lifecycle events. HTTP 403/429 and timeout paths become fenced retries rather than mutation or blind replay paths.
- The worker has no generic Kwork-client reference and no mutation method access. Its only remote seam is `fetch_page(task, identity)` on a read-only source adapter.
- Verification: `python -m pytest tests/unit/test_kwork_buyer_query_generation.py tests/unit/test_kwork_buyer_read_pages.py tests/unit/test_kwork_buyer_worker.py -q` passed (13 tests); `python -m ruff check src/platforms/kwork_buyer/query_generation.py src/platforms/kwork_buyer/sources/read_pages.py tests/unit/test_kwork_buyer_read_pages.py` passed.

### 2026-07-16 - Proposal and conversation domain foundations

- Added an attachment-aware, versioned proposal composer. Its only model seam is an async task-specific gateway invoked with `task="proposal_writing"`; resolved provider/model, immutable context manifest, context hash, terms and explicit sender account are retained in the generated draft.
- Added pure preflight diagnostics for project freshness, duplicate offer evidence, feature flags, and outgoing attachment policy. The composer creates drafts only: no request path can send a proposal or touch a transport.
- Added strict account-bound conversation keys `(platform, account_registration_id, remote_dialog_id)`, per-account cursors, immutable normalized message/attachment records, project/proposal context, and a draft-only AI editor type. Equal remote dialog IDs on different sender accounts remain separate conversations.
- Registered a distinct Buyer Search operation namespace in the generic workflow registry while keeping all existing supply jobs defaulted to `supply`.
- Verification: the current Buyer unit selection passed with `73 passed`; proposal-composer focused tests plus outreach/attachment tests passed `20`; workflow registry focused tests passed `3`.

### 2026-07-16 - Durable Buyer execution, control plane, and workbench

- Added the dedicated Buyer Search SQLite repository with idempotent run/query/task/project/observation/match/raw-artifact/event storage, stable cursor paging, server-side filters, facets, and materialized run-project rows.
- Added durable attachments, parser derivatives, versioned scores, shortlist tags/notes, export jobs, query edits, fenced retry/fail transitions, run diagnostics, and task/counter projections.
- Corrected the live task contract: a lease now includes the durable query text/category/filters, disabled queries cannot be leased, and a confirmed next page is inserted in the same transaction as the completed page. A post-commit telemetry failure cannot trigger a stale retry.
- Added server composition for a separate deterministic Buyer backing Market job, account/VPNTE identity leases, account-bound Kwork read clients, capacity preflight, heartbeats, recovery, and a VPNTE-only no-direct-fallback policy. Live discovery remains gated by `BUYER_SEARCH_LIVE_DISCOVERY`.
- Added actual REST controls for query generation/edit/distribution, projects/facets, shortlist bulk actions, deterministic scoring, attachment/derivative evidence, durable exports, event replay, and a root WebSocket route at `/ws/kwork-buyer-search/runs/{run_id}`.
- Added the desktop Buyer Search workbench route with URL-synchronized server filters/cursors, cancellation-safe loading, detail inspector, event reconciliation, connection status, accessible empty/loading/error states, and compact responsive navigation. Corrected the mobile outlet so the workbench scrolls.
- Verification to this point: targeted repository/worker tests passed `19`; supervisor/runtime adapter/composition tests passed `19`; service/routes/enrichment persistence tests passed `9`; outreach domain/persistence/controller tests passed `9`.

### 2026-07-16 - Outreach and conversation persistence

- Added an additive SQLite outreach store for idempotent project promotion, immutable proposal versions, preflight evidence, confirmation-gated outbox records, reconciliation evidence, and audit events. The proposal API creates no remote send request.
- Added an application bridge that promotes the latest Buyer project projection, carries bounded attachment manifest/excerpts into context, records task-routed model output, and evaluates preflight against current durable project evidence.
- Added an additive account-scoped conversation store for cursors, dialogs, messages, attachments, and draft replies. Its uniqueness key includes platform, account registration ID, and remote dialog ID, preventing cross-account dialog mixing.

### 2026-07-16 - Runtime audit and completion pass

- Ran an implementation audit against the phase plan and used it to turn several latent edge cases into concrete regressions rather than treating the initial vertical slice as complete.
- Fixed the query-edit API contract in the desktop client: `PATCH` returns `{query, distribution}`, and the workbench now consumes the nested durable query rather than briefly replacing a row with `undefined` fields.
- Added an observable current-task snapshot to discovery workers and fleet snapshots so the execution grid can show the actual leased query/task instead of a placeholder.
- Fixed account-bound attachment cleanup: a capability is now closed even when its provenance account does not match the requested account, releasing the factory's local worker guard.
- Hardened authenticated attachment redirects: every redirect hop is validated as a Kwork HTTPS host before a follow-up request is made through the leased VPNTE route; redirects are no longer followed implicitly.
- Added a content-addressed attachment preview foundation. Object references resolve only through checksum verification, and a dedicated Buyer attachment preview route serves a durable original only after run/project/attachment ownership is checked.
- Added a versioned task-routed final AI scoring primitive with bounded project context, JSON schema validation, provider/model/prompt/context hashes, and a deterministic persistence payload shape.
- Verification during this pass: worker/supervisor tests `15 passed`; Buyer unit selection `160 passed`; generic Phase 0/1 focused suite `46 passed`; enrichment controller/runtime attachment regression suite `8 passed`; AI final scoring suite `2 passed`; desktop production build passed.

### 2026-07-16 - Full Buyer Search implementation closeout

- Added selective project enrichment with a single account/VPNTE capability per project, durable per-source evidence/raw artifacts, attachment manifests, policy decisions, and explicit partial-failure records. Detail, want, buyer-history, and web-state reads remain separate from first-wave discovery.
- Added account-bound taxonomy capture through the fixed read-only catalog endpoint set. The refresh route persists immutable snapshots with account/run/route provenance, and the query planner resolves a bounded taxonomy vocabulary from the selected snapshot through task-routed AI generation. Manual query mode remains deterministic and never calls the taxonomy or LLM provider.
- Added resilient discovery pressure handling: durable account/transport/endpoint quarantines for 403/429, fenced cross-run lease exclusion, task endpoint provenance, adaptive 429 worker caps/backoff, and recovery events/metrics in the fleet snapshot.
- Completed the attachment path with checksum-verified local-object preview, bounded XML/image/archive parsing, and an optional `BUYER_ATTACHMENT_VISION` task-routed image OCR/vision pass. When vision is disabled or unavailable the manifest records the omission rather than implying extracted text exists.
- Added versioned final AI scoring, scoped export evidence, query collision/report/regeneration controls, durable worker query bundles, and an explicit restart path. Restart requeues retained query tasks with incremented lease fences while preserving prior projects, observations, artifacts, and audit history.
- Added a durable shadow rollout gate that blocks live discovery until an accepted canary record exists for the requested worker count. Startup recovery applies the configured worker cap and the same durable gate.
- Completed explicit mutation state machines without broadening discovery capabilities: proposal delivery requires a second confirmation, account-bound VPNTE gateway, CSRF and idempotency key, receipt persistence, and unknown-outcome reconciliation; conversation draft delivery uses the pinned sender account, immutable draft body, durable send intent/audit, and inboxCreate only through a short-lived account-bound capability. Neither route auto-retries an ambiguous remote mutation.
- Marked the legacy buyer scout route deprecated while retaining compatibility behavior.

### 2026-07-16 - Final verification

- `python -m pytest tests/unit -k kwork_buyer -q`: `233 passed, 487 deselected`.
- `python -m pytest tests/smoke/test_phase3_feedback_loop.py tests/unit/test_market_job_repository.py tests/unit/test_market_worker_runtime.py tests/unit/test_market_worker_supervisor.py tests/unit/test_market_workflow_registry.py tests/unit/test_market_identity_pool.py -q`: `46 passed`.
- `python -m ruff check src/platforms/kwork_buyer src/api/server.py src/api/routes/kwork_buyer_attachments.py src/api/routes/kwork_buyer_conversations.py src/api/routes/kwork_buyer_outreach.py src/api/routes/kwork_buyer_search.py src/api/routes/kwork_buyer_shadow.py src/api/routes/kwork_buyer_taxonomy.py tests/unit/test_kwork_buyer*.py`: passed.
- `python -m compileall -q src/api/server.py src/api/routes/kwork_buyer_search.py src/api/routes/kwork_buyer_outreach.py src/api/routes/kwork_buyer_conversations.py src/api/routes/kwork_buyer_shadow.py src/api/routes/kwork_buyer_taxonomy.py src/api/routes/kwork_buyer_attachments.py src/platforms/kwork_buyer`: passed.
- `npm run build` in `desktop/`: passed, including Vite production build and Electron packaging.
- Restarted the local API at `http://127.0.0.1:7788`; `/api/health` returned `status: ok`. OpenAPI exposes the Buyer Search restart, taxonomy refresh, preview, final score, proposal delivery, conversation send, collision, and query-bundle routes.
- Browser smoke at `http://127.0.0.1:5173/#/buyer-search`: no application console errors. Final desktop and mobile captures are in `output/playwright/buyer-search-final-desktop.png` and `output/playwright/buyer-search-final-mobile.png`.

### 2026-07-16 - Category picker and Russian UI correction

- Replaced the project-filter category ID input with a dropdown populated from the durable project-category facets of the selected run.
- Added automatic taxonomy bootstrap to the category planner. When no durable Buyer taxonomy snapshot exists, it reads the Kwork category tree through the configured strict VPNTE market route, stores a local immutable snapshot, and immediately opens the rubric picker without requiring an account ID or an active Buyer run.
- Enabled strict VPNTE routing for the market category read (`KWORK_MARKET_USE_PROXY=true`, `KWORK_MARKET_PROXY_STRICT=true`) and retained the existing account-bound refresh path for future enriched taxonomy captures.
- Localized the Buyer Search workbench, taxonomy planner, outreach workspace, conversation workspace, statuses, tooltips, filters, empty states, and known state labels. Historical Buyer Search test-run names are rendered in Russian without mutating durable history.
- Clarified search modes: `Бриф` is now `Описание`, `Гибрид` is now `Описание + рубрика`; the description input is hidden for a pure rubric run and category/hybrid runs require an explicit selected rubric.
- Verification: `npm run build` in `desktop/` passed and rebuilt the NSIS installer; Playwright desktop/mobile checks at `http://127.0.0.1:5173/#/buyer-search` created the automatic snapshot, rendered 61 Kwork categories, selected a rubric successfully, and reported no browser-console errors.

### 2026-07-16 - Multiple rubric selection and Kwork totals

- Replaced the single-choice rubric action with independent checkboxes, selected-rubric chips, removal controls, and a compact multi-rubric summary in the Buyer Search form.
- The picker now requests the same read-only Market metrics endpoint used by the Kwork Market workspace only for selected rubrics. Each selected rubric displays its current Kwork total and the form displays the sum across all selected rubrics.
- Extended the durable Buyer run scope with `category_scopes` and `category_ids`. Query generation now runs per selected scope, preserving every generated query's own category ID and path instead of silently falling back to the first selected rubric.
- Added a service regression proving that a two-rubric category run persists scopes `[5, 7]` and creates planned queries for both categories.
- Live verification through strict VPNTE returned `189 912` Kwork for `Тексты и переводы` and `34 301` for `Аудио, видео, съемка`; the UI rendered the summed total `224 213` with two checked rubrics and no browser-console errors.
- Verification: `python -m pytest tests/unit/test_kwork_buyer_service.py tests/unit/test_kwork_buyer_search_routes.py -q` passed (`15 passed`); `python -m ruff check src/platforms/kwork_buyer/service.py tests/unit/test_kwork_buyer_service.py` passed; `npm run build` in `desktop/` passed and rebuilt `desktop/dist-electron/PSR Desktop Setup 1.0.0.exe`.

### 2026-07-16 - Catalog duplicate cleanup

- Fixed automatic snapshot startup under React development remounts: an aborted initial snapshot request no longer looks like an empty catalog and therefore cannot trigger a duplicate catalog capture.
- The rubric picker now keeps only the latest automatic Kwork catalog visible, hides the technical snapshot ID, and shows one plain status line with the catalog update time. Historical immutable records remain available in storage for audit but no longer confuse the operator.
