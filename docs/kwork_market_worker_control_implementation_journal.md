# Kwork Market Worker Control: implementation journal

## Purpose

This journal records implementation decisions, changed files, verification
commands, and remaining work for the architecture plan in
`docs/kwork_market_worker_control_architecture_plan.md`.

## Plan checklist

- [ ] Phase 0: truth and source-contract gates.
- [ ] Phase 1: source adapters and validated web continuation.
- [ ] Phase 2: durable jobs and worker actors.
- [ ] Phase 3: VPNTE multi-slot transport management.
- [ ] Phase 4: desktop control-plane UI.
- [ ] Phase 5: deep scans and 10k-scale safeguards.
- [ ] Phase 6: cutover and legacy compatibility.

## 2026-07-11 - Discovery and implementation start

- Read the full architecture plan and indexed project `C-psr` with
  Codebase Memory MCP (`8020` nodes, `19433` edges).
- Confirmed the implementation order: validate the card-source contract
  before introducing durable jobs and worker actors.
- Confirmed from the plan that the current mobile `page` continuation must
  be treated as invalid until the response cursor is proven to advance.
- Confirmed that the target durable implementation uses SQLite WAL and
  in-process actors behind broker/runtime/transport interfaces.
- Ran Tavily Search only (not Tavily Research) against SQLite WAL and Celery
  documentation to cross-check the local-desktop storage/runtime direction.
- Current work: map existing supply scanner, API, transport, and UI seams;
  then implement Phase 0 as the first vertical slice.

## Evidence and decisions

- The legacy scanner remains in place until the new source and job paths have
  parity; it must not be deleted during the early phases.
- The worktree is treated as the source of truth. Every phase completion must
  be recorded here with tests or runtime evidence.

## 2026-07-11 - Phase 0 vertical slice

### Implemented

- Added `src/platforms/kwork_supply/contracts.py` with transport-independent
  cursor, batch, capability, novelty, fingerprint, state, verdict, and source
  protocol types.
- Added fixture-driven mobile contract tests. A repeated page-two response with
  `reported_page=1` is a `contract_violation`; protection is never accepted as
  an empty page.
- `KworkMarketClient.get_kworks()` now keeps top-level `paging`, `pagination`,
  and `meta` response data so downstream code can compare request and response
  cursors.
- Legacy `KworkSupplyScanner` now limits the mobile source to a verified first
  page, records requested/reported cursors and sorted-ID fingerprints, emits
  novelty/duplicate counters, preserves first-seen listing provenance, and
  exposes a clear continuation policy in its snapshot.
- Narrow assistant refresh follows the same first-page rule and no longer
  silently requests page two or three.
- Phase-0 desktop work added a structured renderer for AI objects, bounded
  legacy request diagnostics, and truthful labels for aggregate volume,
  transport slots, working runs, and seller repetition.

### Verification

- `python -m pytest tests/unit/test_kwork_market_supply.py tests/unit/test_kwork_source_contracts.py tests/unit/test_kwork_market.py -q`
  -> `54 passed`.
- `python -m ruff check src/platforms/kwork_market.py src/platforms/kwork_market_supply.py src/platforms/kwork_supply tests/unit/test_kwork_market.py tests/unit/test_kwork_market_supply.py tests/unit/test_kwork_source_contracts.py`
  -> passed.
- `npm exec vite build` in `desktop/` -> passed.

### Remaining for Phase 1

- Persist validated aliases in the durable registry and wire the adapters into
  the job coordinator; the legacy scanner remains compatibility-only until
  that path is exercised through the durable job flow.

## 2026-07-11 - Phase 1 source foundation

### Implemented

- Added first-page-only `KworkMobileKworksAdapter`; it retains the reported
  mobile cursor and rejects continuation before issuing a network request.
- Added `WebCatalogSource` and `KworkWebCatalogAdapter`. They parse
  `viewData.kworks.posts.data`, use the accepted `excludeIds + onePage=1`
  cursor, validate `activeCategoryId`, separate `total` from `total_found`,
  and classify 403/429 as protection rather than exhaustion.
- Added safe canonical nested path validation in both the source adapter and
  the existing web client. URL-like input, queries, fragments, and traversal
  are rejected.
- Added `KworkMarketClient.get_catalog_filters()` for aggregate mapping and
  fixed the web helper to honor a client-specific proxy URL.
- Added `LocalArtifactStore` for atomic gzip raw-response storage at
  `data/kwork_market_jobs/{job_id}/raw/{operation_id}-{attempt}.json.gz`.
- Added an opt-in live continuation gate at
  `tests/integration/test_kwork_web_continuation_live.py`; it is skipped unless
  `KWORK_LIVE_WEB_CONTINUATION=1` is explicitly set.

### Verification

- Source, adapter, artifact, transport, and proxy tests currently pass:
  `35 passed` in the focused unit suite.
- The web adapter has a synthetic three-batch test that accepts 72 unique
  cards and checks the accumulated `excludeIds` cursor.
- The live gate is intentionally skipped by default and was verified to skip
  cleanly without environment credentials.

## 2026-07-11 - Phase 2 durable repository foundation

### Implemented

- Added `src/platforms/kwork_supply/repository.py` with a separate SQLite DB
  path, WAL, `foreign_keys`, a busy timeout, short transactions, and async
  entry points backed by `asyncio.to_thread`.
- Added durable tables and indexes for jobs, category aliases, shards,
  operations, attempts, workers, transports, listings, observations, events,
  checkpoints, worker commands, and accepted-batch idempotency records.
- Jobs expose serializable scope, counters, state, phase, and optimistic
  revision updates. Operations have durable leases, expiry recovery, and
  attempt history. Events are atomically sequenced per job and replayable.
- Accepted batches atomically persist listings, append provenance observations,
  update counters and shard cursor, complete the operation, enqueue an optional
  successor, append events, save a checkpoint, and retain an idempotent result.
  Replaying a committed key cannot increment unique-card or observation totals.
- Added a minimal durable canonical-alias registry for Phase 1 validation
  results and a focused repository test suite.

### Verification

- `python -m pytest tests/unit/test_market_job_repository.py tests/unit/test_kwork_source_contracts.py tests/unit/test_kwork_web_catalog_source.py -q`
  -> `17 passed`.
- `python -m ruff check src/platforms/kwork_supply/repository.py tests/unit/test_market_job_repository.py`
  -> passed.

## 2026-07-11 - Phase 5 deterministic rate control

### Implemented

- Added pure `src/platforms/kwork_supply/rate_control.py`. It receives explicit
  timestamps and immutable state, has no network, repository, worker runtime,
  system-clock, or transport-manager dependency.
- Added atomic global plus source-class token-bucket acquisition. Unknown
  source labels share a bounded default bucket rather than growing state for
  arbitrary strings.
- Added bounded numeric and HTTP-date `Retry-After` support, explicit source
  or global cooldown scope, fallback delays, and 403/429 feedback. A 403 can
  recommend quarantine, but every result explicitly leaves
  `transport_rotation_requested=False`; rotation remains a separate drained
  supervisor action.
- Added bounded adaptive worker-concurrency recommendations from success,
  403/429, timeout, novelty, and healthy-transport signals. Recommendations
  are clamped to the policy and healthy transport capacity.

### Verification

- `python -m pytest tests/unit/test_market_rate_control.py -q`
  -> `5 passed`.
- `python -m ruff check src/platforms/kwork_supply/rate_control.py tests/unit/test_market_rate_control.py`
  -> passed.

## 2026-07-11 - Phase 4 market jobs REST and event stream

### Implemented

- Added the isolated `src/api/routes/kwork_market_jobs.py` control-plane
  router. It reads the initialized `MarketScanCoordinator` from
  `app.state.market_jobs` and does not construct scanners or workers per HTTP
  request.
- Added the planned create/list/snapshot/update, pause/resume/stop,
  worker-pool, operational-view, worker-command, operation-retry, and results
  endpoints. Create returns `202 Accepted` with stable status and event URLs.
- Added strict request schemas and centralized mappings for durable missing
  records (`404`), optimistic/lease conflicts (`409`), and invalid commands
  (`422`).
- Added `/ws/kwork-market/jobs/{job_id}?after_seq=N`. It subscribes before
  replay and de-duplicates live notifications by durable sequence, preventing
  a gap or duplicate at the replay-to-live boundary.
- Added a FastAPI test app backed by a temporary coordinator/SQLite database;
  tests cover durable create/replay, all worker commands, operational views,
  mutable controls, error mappings, and an uninitialized runtime.

### Verification

- `python -m pytest tests/unit/test_market_job_routes.py -q` -> `4 passed`.
- `python -m ruff check src/api/routes/kwork_market_jobs.py tests/unit/test_market_job_routes.py`
  -> passed.

## 2026-07-11 - Phase 2 lease state gate

### Implemented

- `lease_operation` now joins `market_jobs` inside its atomic lease
  transaction and excludes `pausing`, `paused`, `stopping`, `stopped`,
  `blocked`, `failed`, and `completed` jobs. A pause or stop therefore cannot
  race into a newly claimed queued operation after its state transition commits.

### Verification

- Repository tests parameterize all seven non-leasable job states and verify
  that a paused job can lease its queued operation only after a running resume.
- `python -m pytest tests/unit/test_market_job_repository.py -q`
  -> `14 passed`.
- `python -m ruff check src/platforms/kwork_supply/repository.py tests/unit/test_market_job_repository.py`
  -> passed.

## 2026-07-11 - Phase 3 VPNTE transport adapter

### Implemented

- Added `src/platforms/kwork_supply/transports/` with a minimal supervisor
  contract: refresh, acquire, release, rotate, get, health, and close.
- Added `VpnteTransportManager`, which polls the slot-aware VPNTE
  `/instances` contract and exposes immutable `TransportSnapshot` records
  using stable IDs such as `vpnte-slot-3`.
- A stopped discovered slot is started explicitly. It is usable only after
  VPNTE reports both `running=true` and a non-empty `proxyUrl`; no guessed port
  or direct fallback is created.
- Leases are exclusive in process, release/rotate require the owning worker,
  and rotation targets only the leased slot. A healthy rotation advances its
  local generation and preserves visible profile, country, and PID metadata.
- Added fake-provider unit tests; they do not use the real VPNTE control
  endpoint.

### Verification

- `python -m pytest tests/unit/test_market_transport_manager.py tests/unit/test_vpnte_proxy.py -q`
  -> `10 passed`.
- `python -m ruff check src/platforms/kwork_supply/transports src/platforms/kwork_supply/__init__.py tests/unit/test_market_transport_manager.py`
  -> passed.

## 2026-07-11 - Phase 2 repository operational views

### Implemented

- Added cursor/limit operational reads for workers, transports, operations,
  listings, checkpoints, and worker commands; records remain JSON-serializable
  for upcoming REST routes.
- Added optimistic mutable job controls for desired worker count, card target,
  profile, and request/time budgets.
- Added durable worker commands with queued, acknowledged, completed, and
  failed state transitions and timestamps.
- Added lease-owned `complete_operation` and `fail_operation`; failure can be
  terminal or move an operation into `retry_wait` at a supplied deadline.
- Added `market_workers.job_id` with an additive migration. Worker-to-job
  binding persists after an operation is released, so job-scoped worker and
  transport views retain idle actors instead of inferring ownership solely from
  `current_operation_id`.

### Verification

- `python -m pytest tests/unit/test_market_job_repository.py -q`
  -> `7 passed`.
- `python -m ruff check src/platforms/kwork_supply/repository.py tests/unit/test_market_job_repository.py`
  -> passed.

## 2026-07-11 - Durable coordinator, workers, and runtime

### Implemented

- Added `MarketScanCoordinator` as the sole control-plane writer above the
  repository. Job creation persists an idempotent `map_scope` operation and
  replayable event envelopes; pause/resume/stop, worker-pool changes, retries,
  commands, snapshots, and event replay all use durable state.
- Added `MarketWorker` and `MarketWorkerSupervisor`. Workers have stable IDs,
  persisted generations and heartbeats, process durable worker commands,
  drain without issuing a new lease, keep an exclusive managed transport, and
  recover queued work after restart. The repository lease query now rejects
  paused, stopping, blocked, and terminal jobs atomically.
- Added `ScopeMapper` and deterministic `ShardPlanner`. Alias validation,
  aggregate scope total, reported stream total, and observed first-batch count
  remain separate. Initial shard IDs and request budgets are deterministic and
  use observed batch size rather than aggregate volume.
- Added `MarketOperationExecutor` for `map_scope` and `fetch_batch`. It uses
  the validated web adapter, writes private raw payloads to the artifact store,
  persists alias evidence and shards, commits accepted batches atomically, and
  schedules only explicit `excludeIds + onePage` continuations.
- Connected the runtime in FastAPI lifespan: `data/kwork_market_jobs.db` is
  initialized before requests, the supervisor is started once, the coordinator
  is assigned to `app.state.market_jobs`, and workers close before shutdown.
- Included the dedicated REST/WebSocket market-jobs router in the server.

### Verification

- `python -m pytest tests/unit/test_market_job_coordinator.py
  tests/unit/test_market_worker_supervisor.py tests/unit/test_market_operation_executor.py -q`
  -> passed.
- `python -m pytest tests/unit/test_market_job_routes.py
  tests/unit/test_market_job_repository.py tests/unit/test_market_scope_mapper.py
  tests/unit/test_market_shard_planner.py -q` -> passed.
- `python -m ruff check src/platforms/kwork_supply src/api/server.py
  src/api/routes/kwork_market_jobs.py` -> passed for the changed modules.
- FastAPI lifespan smoke with a fresh temporary `PSR_DATA_DIR` returned
  `/api/health` successfully and exposed an initialized market coordinator.

## 2026-07-11 - Phase 5 deterministic marketplace metrics

### Implemented

- Added pure `src/platforms/kwork_supply/metrics.py`, independent of the
  repository, runtime, network, clocks, and LLMs.
- Added exact linear-interpolation P10/P25/P50/P75/P90 using `Decimal` and
  the explicit rank formula `p * (n - 1)`. Shares use a documented fixed
  six-decimal `ROUND_HALF_UP` contract, avoiding binary float drift.
- Added separately labelled aggregate scope volume and observed-card records;
  aggregate count is explicitly not described as card coverage.
- Added valid/missing/invalid-price counts, quantiles, min/max outlier
  context, configurable cheap threshold/share, deterministic shard price
  distributions, and observed seller repetition/HHI diagnostics.
- Seller metrics explicitly say they describe observed-card repetition, not a
  market-concentration estimate. The module and public helpers are exported
  from `kwork_supply`.

### Verification

- `python -m pytest tests/unit/test_market_metrics.py -q` -> `4 passed`.
- `python -m pytest tests/unit/test_market_metrics.py
  tests/unit/test_market_scope_mapper.py tests/unit/test_market_shard_planner.py
  tests/unit/test_market_job_coordinator.py tests/unit/test_market_job_repository.py -q`
  -> `29 passed`.
- `python -m ruff check src/platforms/kwork_supply/metrics.py
  src/platforms/kwork_supply/__init__.py tests/unit/test_market_metrics.py`
  -> passed.

## 2026-07-11 - Phase 5 deterministic snapshot exports

### Implemented

- Added pure/service `src/platforms/kwork_supply/exporter.py` with
  `MarketSnapshotExporter` / `SnapshotExporter` and
  `export_market_snapshot(...)`. It accepts repository-shaped mappings and
  does not depend on workers, network, coordinator, or routes.
- Exports use the LocalArtifactStore-compatible layout
  `{job_id}/exports/summary.json`, `listings.jsonl.gz`, and
  `observations.jsonl.gz`. Every target is written to a same-directory temp
  file, fsynced, then atomically replaced.
- JSON and JSONL are canonicalized; listing and observation rows receive
  stable identifier ordering; GZIP uses `mtime=0`. Re-running the same logical
  snapshot in a different mapping/record order produces identical bytes.
- Summary records artifact hashes, sizes, schema versions, relative paths,
  job data, metrics, and counts. Event input contributes only type counts;
  event payloads, including raw response content, are never exported.
- Export metadata and all public exporter APIs are re-exported from
  `kwork_supply`.

### Verification

- `python -m pytest tests/unit/test_market_exporter.py -q` -> `3 passed`.
- `python -m pytest tests/unit/test_market_exporter.py
  tests/unit/test_market_artifacts.py tests/unit/test_market_metrics.py
  tests/unit/test_market_job_repository.py -q` -> `28 passed`.
- `python -m ruff check src/platforms/kwork_supply/exporter.py
  src/platforms/kwork_supply/__init__.py tests/unit/test_market_exporter.py`
  -> passed.

## 2026-07-11 - Phase 5 bounded selective enrichment

### Implemented

- Added pure `src/platforms/kwork_supply/enrichment.py` with no network,
  database, worker, runtime, or clock dependency.
- Added a deterministic policy-bounded selector over normalized listings.
  The hard ceiling is 500 candidates, so a 10k collection cannot schedule a
  10k-detail enrichment pass.
- Selection covers shard, rank-derived price band, and seller-frequency strata
  before balancing already represented strata. Every selected listing returns
  stable references plus explicit selection reasons and compact evidence.
- Exported the selector and policy from `src.platforms.kwork_supply`.

### Verification

- `python -m pytest tests/unit/test_market_enrichment.py -q`
  -> `3 passed`.
- `python -m ruff check src/platforms/kwork_supply/enrichment.py
  src/platforms/kwork_supply/__init__.py tests/unit/test_market_enrichment.py`
  -> passed.

## 2026-07-11 - Phase 5 bounded AI evidence packet

### Implemented

- Added pure `src/platforms/kwork_supply/ai_evidence.py` with no LLM, network,
  repository, worker, runtime, or clock dependency.
- Added a JSON-safe deterministic packet that keeps aggregate observed-data
  metrics separate from a small selected evidence sample. It marks the packet
  and hypothesis inputs as sample-based and explicitly says that the sample is
  not market coverage.
- Evidence rows retain only stable listing references plus title, price, seller,
  shard, selection reasons, and stratification evidence. Raw/payload/body/HTML
  and credential-like fields are filtered recursively before serialization.
- Added bounded record, metric, string, and nesting policies; a 10k observed
  input with 20 enrichment candidates emits only five evidence records in the
  focused test. Exported the public API from `kwork_supply`.

### Verification

- `python -m pytest tests/unit/test_market_ai_evidence.py -q`
  -> `3 passed`.
- `python -m ruff check src/platforms/kwork_supply/ai_evidence.py
  src/platforms/kwork_supply/__init__.py tests/unit/test_market_ai_evidence.py`
  -> passed.

## 2026-07-11 - Phase 4 operation evidence and transport route UI

### Implemented

- Added a per-operation evidence action in `OperationTable` and a compact
  `AttemptEvidencePanel` for durable attempts. It shows requested/reported
  cursors, page fingerprint, received/new/duplicate counts, HTTP evidence,
  raw response reference, retry state, and error details without rendering raw
  response content.
- `KworkMarketJob` loads attempts only after the action through
  `marketJobsApi.getOperationAttempts(jobId, operationId)`. An incrementing
  request token prevents a late response from a previously selected operation
  from overwriting the current evidence panel.
- `WorkerTable` now accepts the job transport list, resolves each worker's
  transport by ID, and shows transport ID, sanitized proxy endpoint, profile,
  and country. A shared proxy formatter strips URL userinfo, query strings,
  and fragments; the existing transport-routes panel uses it too.

### Verification

- `npm exec vite build` in `desktop/` -> passed.
- `npm exec tsc --noEmit` still reports pre-existing unrelated errors in
  Conversations, Conversion, Dashboard, Health, KworkMarket, Orders, and
  Skipped. No errors were reported for the modified market-job components.

## 2026-07-11 - Phase 4 revision-aware job controls

### Implemented

- Added compact `JobConfigControls` to the existing job screen for target
  unique cards, desired worker count, profile, request budget, and time budget.
  Blank budget controls explicitly clear their existing optional budget.
- `KworkMarketJob` validates values locally, sends only changed configuration
  fields to `marketJobsApi.updateJob`, and applies worker-count changes through
  `marketJobsApi.setWorkerPool`.
- Both updates send the currently rendered optimistic revision. When a submit
  needs both endpoints, they run serially with the revision advanced between
  calls. An optimistic job value is shown while saving; conflict/error handling
  clears it, refreshes durable state, and keeps a prominent error in the
  control bar.
- Updated the frontend API client's worker-pool method to pass the already
  supported `expected_revision` field. The page now prefers a freshly fetched
  snapshot when its revision is at least the stream snapshot revision, avoiding
  stale stream state after configuration changes.

### Verification

- `npm exec vite build` in `desktop/` -> passed.

## 2026-07-11 - Phase 2 bounded mobile first-page runtime

### Implemented

- Added a dedicated `mobile_first_page_only` executor branch that never enters
  the web-catalog adapter. It plans one `mobile_kworks` shard and one
  `fetch_batch` operation with an explicit page-1 cursor and request budget of
  one.
- The mobile fetch validates page 1 before the client call, persists the
  requested/reported cursors, observations, and raw artifact, marks the shard
  exhausted, and schedules no continuation before the existing analyze/export
  pipeline.
- Mobile adapter results now carry the private raw payload used by the durable
  artifact store. Focused unit and supervisor integration tests prove that a
  fake client receives only `get_kworks(..., page=1)` and never a web request.

### Verification

- `python -m pytest tests/unit/test_kwork_mobile_source.py
  tests/unit/test_market_operation_executor.py
  tests/integration/test_market_mobile_first_page.py -q` -> `8 passed`.
- `python -m ruff check src/platforms/kwork_supply/executor.py
  src/platforms/kwork_supply/sources/mobile_kworks.py
  tests/unit/test_kwork_mobile_source.py
  tests/unit/test_market_operation_executor.py
  tests/integration/test_market_mobile_first_page.py` -> passed.

## 2026-07-11 - Phase 5 runtime completion, protection, and results projection

### Implemented

- Collection now advances through durable `analyze_snapshot` and
  `export_snapshot` operations instead of marking a job complete after its
  final fetch. Analysis checkpoints deterministic metrics, a bounded
  enrichment selection, and a sample-based AI evidence packet before export.
- Export loads one repository snapshot, writes the compressed artifacts, keeps
  analysis data in the final export checkpoint, emits `result.ready`, and only
  then changes the job to `completed`.
- Added a repository export projection containing normalized listings,
  observations, and event metadata. It is internal to export and does not
  change paginated UI reads.
- Added executor-wide global/source token buckets, bounded `Retry-After`
  handling for 429, and explicit 403 route quarantine. A 429 keeps the job
  runnable with a durable retry time; a 403 quarantines the worker route and
  blocks collection without silently rotating or falling back.
- Added a durable operation-attempt endpoint and UI evidence panel. Requested
  and reported cursors, fingerprint, counts, raw artifact reference, retry,
  and error details are loaded only when the operator opens an operation.
- Added job configuration controls and source-policy selection to the default
  workspace. Mobile page-one jobs can be created without a web alias; validated
  web jobs still require one. Results now expose only compact enrichment and
  AI evidence counts, never raw source content.

### Verification

- `python -m pytest tests/unit/test_market_operation_executor.py
  tests/unit/test_market_results_analyzer.py tests/integration/test_market_job_resume.py
  tests/unit/test_market_job_routes.py tests/unit/test_market_rate_control.py
  tests/smoke/test_market_job_10k_synthetic.py -q` -> `17 passed`.
- `python -m ruff check` passed for the changed market runtime, repository,
  API route, and focused tests.
- `npm exec vite build` in `desktop/` -> passed.
- FastAPI lifespan smoke with a fresh `PSR_DATA_DIR` returned `/api/health`
  and initialized `app.state.market_jobs`.
- Playwright checked the new workspace at desktop and 390px mobile widths.
  The only console errors were expected connection failures because no API
  server was running on `127.0.0.1:7788` during frontend-only QA.

## 2026-07-11 - Runtime budgets, adaptive caps, and cutover marking

### Implemented

- Added bounded recent-attempt concurrency signals and an advisory effective
  worker cap in the supervisor. Protection signals and the healthy VPNTE route
  count can temporarily reduce active workers, while durable
  `desired_workers` remains the operator's configured value. Coalesced
  `worker.concurrency_recommended` events make the cap observable.
- Added time-budget finalization. On expiration, workers stop taking new
  collection leases, pending collection operations are cancelled with durable
  reason evidence, and committed partial data proceeds to analysis/export.
  A late already-leased fetch cannot create another continuation once the job
  enters `completing`.
- Exposed source policy, time budget, and AI evidence choice in the default
  job-create UI. The old synchronous `/market/supply-scan` endpoint remains
  available for the parity window but is marked deprecated in OpenAPI and its
  response identifies `/api/kwork/market/jobs` as the replacement.

### Verification

- `python -m pytest tests/unit/test_market_job_repository.py
  tests/unit/test_market_job_coordinator.py tests/unit/test_market_worker_supervisor.py
  tests/unit/test_market_operation_executor.py tests/integration/test_market_job_resume.py -q`
  -> `29 passed` during the budget/adaptive implementation.
- `python -m pytest tests/unit/test_kwork_routes.py
  tests/unit/test_market_job_routes.py -q` -> `21 passed` after legacy marking.
- Targeted `ruff` checks and `npm exec vite build` -> passed.

## 2026-07-11 - Partition filter execution

### Implemented

- Web source requests now carry validated shard filters on both the initial
  request and every `excludeIds + onePage` continuation. The cursor-owned keys
  are reserved so a partition cannot override continuation behavior.
- `MarketOperationExecutor` passes scope filters during mapping and durable
  shard filters during fetch. Planner output therefore affects the actual web
  request rather than remaining metadata-only.

### Verification

- `python -m pytest tests/unit/test_kwork_web_catalog_source.py
  tests/unit/test_kwork_web_catalog_adapter.py tests/unit/test_kwork_web_transport.py
  tests/unit/test_market_operation_executor.py tests/integration/test_market_job_resume.py -q`
  -> `22 passed`.
- `ruff` passed for the source adapter, executor, and focused tests.

## 2026-07-11 - Explicit direct-route transport policy

### Implemented

- `KworkMarketClient` now distinguishes an explicit direct route from legacy
  environment-proxy behavior. Durable market workers always pass
  `use_environment_proxy=False`; a VPNTE lease supplies the only proxy URL and
  a direct fallback remains direct.
- Existing callers retain their previous environment-proxy default, so the
  compatibility scanner behavior is unchanged while the worker control plane
  has no silent transport fallback.

### Verification

- `python -m pytest tests/unit/test_kwork_web_transport.py
  tests/unit/test_kwork_market.py tests/unit/test_market_operation_executor.py -q`
  -> `47 passed`.
- `ruff` passed for the client, executor, and transport test.

## 2026-07-11 - Deep resume after completed export

### Implemented

- A completed job whose target is increased now re-enters `collect` using its
  durable validated web cursor, instead of pretending that resume succeeded
  while leaving no work queued.
- Historical succeeded analysis and export operations no longer suppress the
  next analysis/export cycle. Export operation IDs are checkpoint-specific, so
  a fresh export cannot collide with a completed historical operation.

### Verification

- `python -m pytest tests/unit/test_market_job_coordinator.py
  tests/unit/test_market_operation_executor.py -q` -> `12 passed`.
- `python -m pytest tests/integration/test_market_job_resume.py -q` ->
  `2 passed`, including `4 -> 6` cards in the same job, two analysis cycles,
  and two export cycles.

## 2026-07-11 - Mapping evidence and replay gaps

### Implemented

- Mapping now reads supported `catalogFilters` and `categoryAttributes` data,
  stores compact evidence in a durable mapping checkpoint, and archives the
  raw mapping payload separately from web-card batches.
- Large scopes may propose classification-value partitions, but a shard is
  created only after a web-catalog probe has a valid contract and a different
  fingerprint from the root stream. Unproven price and seller dimensions stay
  excluded.
- WebSocket replay now emits `resync_required` for a retention gap or a
  backlog larger than the durable replay window. The existing UI hook fetches
  a fresh snapshot for that frame, avoiding a silent sequence gap.

### Verification

- `python -m pytest tests/unit/test_market_scope_mapper.py
  tests/unit/test_market_operation_executor.py -q` -> `10 passed`.
- `python -m pytest tests/unit/test_market_job_routes.py
  tests/unit/test_market_job_repository.py -q` -> `22 passed`.
- Full focused market suite -> `157 passed`.
- `python -m ruff check` passed for all modified runtime, API, and tests.

## 2026-07-11 - Resync completion and worker reconnect control

### Implemented

- Job snapshots now expose the current durable `last_event_sequence`, allowing
  the browser to reconnect after `resync_required` from the exact snapshot
  sequence instead of replaying the same oversized gap.
- Added the missing reconnect action to the worker UI; it uses the existing
  durable worker-command API and remains an icon-only, tooltip-labelled control.

### Verification

- Added WebSocket coverage for both a replay-window overflow and a simulated
  event-retention gap.
- Full focused market suite -> `158 passed`.
- `python -m ruff check` -> passed.
- `npm exec vite build` in `desktop/` -> passed.
- FastAPI lifespan smoke initialized `app.state.market_jobs` and returned
  `/api/health` successfully.

## 2026-07-11 - Deep remap and live rollout harness

### Implemented

- When a completed validated-web job has no continuation cursor after its
  target increases, resume queues a new mapping operation instead of returning
  a completed no-op. The remap can discover and probe fresh classification
  partitions, while exhausted root shards are not scanned again.
- Mapping checkpoints are durable evidence but no longer replace the latest
  analysis/result checkpoint, so a failed deep-remap attempt cannot hide the
  prior completed result.
- Server-owned market workers now acquire fresh Session Hub cookies for each
  web-catalog adapter creation; the service cache limits the actual Hub reads.
- Added an opt-in live rollout test for `60 -> 500 -> 2000 -> 10000` using one
  durable job and explicit Session Hub cookies. It is skipped unless
  `KWORK_LIVE_MARKET_ROLLOUT=1` is set.

### Verification

- Added exhausted-root deep-remap coverage with a newly validated partition.
- `python -m pytest tests/unit/test_market_job_repository.py
  tests/unit/test_market_job_coordinator.py tests/unit/test_market_operation_executor.py
  tests/integration/test_market_job_resume.py -q` -> `35 passed`.
- Live rollout harness collection -> `1 skipped` without its opt-in variable.

## 2026-07-11 - Control-plane bundle split

### Implemented

- The workspace, job control plane, and legacy market screen now load through
  route-level React lazy chunks. The default application bundle no longer pays
  for the market monitor until an operator opens a market route.

### Verification

- `npm exec vite build` -> passed. Main bundle dropped from about `860 kB` to
  about `722 kB`; the active job control chunk is `33 kB` before gzip.

## 2026-07-11 - Automatic partition expansion to 10k

### Implemented

- An uncapped job that drains all known shards below its target now queues a
  durable `partition_mapping` wave instead of finalizing early. Each later
  wave excludes filters already represented by durable shards, so it probes the
  next classification candidates rather than repeating exhausted scopes.
- When no new valid partition remains, the job records an explicit
  `target_unreached_source_exhausted` warning and proceeds to deterministic
  analysis/export. Explicit `request_budget` remains an operator cap and does
  not trigger automatic expansion.

### Verification

- Added an actor-level synthetic run that collects exactly `10,000` unique
  cards through one root plus nine classification partitions over two mapping
  waves: `1 passed` in about ten seconds.

## 2026-07-11 - Controlled live rollout completed

### Verification

- Session Hub supplied a non-empty local Kwork web session to the durable
  worker runtime.
- The opt-in live gate passed sequentially for the same job at
  `60 -> 500 -> 2,000 -> 10,000` unique cards.
- Command: `KWORK_LIVE_MARKET_ROLLOUT=1 python -m pytest
  tests/integration/test_market_job_rollout_live.py -q` with the four default
  targets.
- Result: `1 passed in 862.60s` (`14:22`).

## 2026-07-11 - Final UI route check

### Verification

- Playwright opened `#/kwork-market` after the lazy-route split. The workspace
  rendered its controls and job table; no chunk-loading error appeared.
- The only browser console failures were expected API/WebSocket connection
  refusals because the frontend-only dev server ran without the backend on
  `127.0.0.1:7788`.

## 2026-07-11 - Create latency gate

### Verification

- The job-route test now measures the local `POST /api/kwork/market/jobs`
  response and enforces the plan's sub-second `202 Accepted` requirement.

## 2026-07-11 - Final focused verification

### Verification

- `python -m pytest` across the market contracts, durable control plane,
  transport manager, resume paths, live-harness collection, and 10k smoke
  suite -> `164 passed, 1 skipped`.
- `python -m ruff check` for all changed market runtime, API, and focused tests
  -> passed.

## 2026-07-11 - Completion audit

- Durable job creation, stable workers, leases, heartbeats, checkpoints, and
  restart recovery are covered by the coordinator, repository, supervisor, and
  integration tests.
- The UI route, worker controls, replay/resync path, cursors, paginated views,
  truthful metric labels, result evidence, and lazy-loading control plane are
  implemented and build-verified.
- Mobile page-one enforcement, web continuation contracts, 403/429 control,
  VPNTE transport lifecycle, deep resume/remap, global dedupe, bounded AI
  evidence, compressed export, and 10k synthetic actor coverage are verified.
- The final controlled live rollout passed in sequence through `60`, `500`,
  `2,000`, and `10,000` unique cards using the durable worker path.
