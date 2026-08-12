export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }
export type JsonRecord = Record<string, JsonValue>

export type MarketJobState =
  | 'preparing'
  | 'mapping'
  | 'planning'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'completing'
  | 'enriching'
  | 'analyzing'
  | 'finalizing'
  | 'completed'
  | 'stopping'
  | 'stopped'
  | 'blocked'
  | 'failed'

export type MarketJobPhase = 'prepare' | 'map' | 'plan' | 'collect' | 'enrich' | 'analyze' | 'export'

export type MarketOperationState =
  | 'queued'
  | 'leased'
  | 'running'
  | 'succeeded'
  | 'retry_wait'
  | 'failed'
  | 'cancelled'
  | 'contract_violation'
  | 'blocked'

export type MarketWorkerState =
  | 'starting'
  | 'connecting'
  | 'idle'
  | 'leasing'
  | 'busy'
  | 'cooldown'
  | 'backoff'
  | 'blocked'
  | 'draining'
  | 'stopped'
  | 'crashed'

export type MarketWorkerDesiredState = 'running' | 'draining' | 'disabled' | 'stopped'
export type MarketTransportHealth = 'unknown' | 'starting' | 'healthy' | 'degraded' | 'quarantined' | 'stopped'
export type MarketNetworkPolicy = 'direct_only' | 'vpnte_only' | 'prefer_vpnte' | 'explicit_pool'
export type MarketSourcePolicy = 'validated_only' | 'mobile_first_page_only'
export type MarketStreamState = 'connecting' | 'live' | 'disconnected' | 'resyncing' | 'stale'

export interface MarketScope {
  category_id: number
  category_name?: string
  classifier_id?: number | null
  classifier_name?: string
  canonical_alias?: string | null
  filters?: JsonRecord
}

export interface CreateMarketJobPayload {
  scope: MarketScope
  profile?: string
  target_unique_cards?: number
  desired_workers?: number
  account_registration_ids?: string[]
  network_policy?: MarketNetworkPolicy
  source_policy?: MarketSourcePolicy
  include_ai?: boolean
  request_budget?: number | null
  time_budget_seconds?: number | null
}

export interface UpdateMarketJobPayload {
  profile?: string
  target_unique_cards?: number
  desired_workers?: number
  request_budget?: number | null
  time_budget_seconds?: number | null
  expected_revision?: number
}

export interface MarketJob {
  job_id: string
  scope: MarketScope
  profile: string
  target_unique_cards: number
  desired_workers: number
  account_registration_ids?: string[]
  network_policy: MarketNetworkPolicy
  source_policy: MarketSourcePolicy
  include_ai: boolean
  state: MarketJobState
  phase: MarketJobPhase
  revision: number
  request_budget?: number | null
  time_budget_seconds?: number | null
  counters: Record<string, number>
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  latest_checkpoint_id?: string | null
  last_error?: string | null
  last_warning?: string | null
}

export interface MarketWorker {
  worker_id: string
  generation: number
  desired_state: MarketWorkerDesiredState
  actual_state: MarketWorkerState
  runtime_kind: string
  transport_id?: string | null
  current_operation_id?: string | null
  heartbeat_at?: string | null
  counters: Record<string, number>
  last_error?: string | null
}

export interface MarketTransport {
  transport_id: string
  kind: 'direct' | 'vpnte'
  health: MarketTransportHealth
  slot?: number | null
  proxy_url?: string | null
  profile_id?: string | null
  profile_name?: string | null
  country?: string | null
  pid?: number | null
  generation: number
  lease_owner?: string | null
  quarantine_until?: string | null
  last_rotate_reason?: string | null
}

export interface MarketShard {
  shard_id: string
  job_id: string
  source: string
  alias: string
  filters: JsonRecord
  expected_count?: number | null
  state: string
  priority: number
  cursor?: JsonRecord | null
  created_at?: string
  updated_at?: string
}

export interface MarketOperation {
  operation_id: string
  job_id: string
  shard_id?: string | null
  kind: 'map_scope' | 'resolve_alias' | 'fetch_batch' | 'enrich_listing' | 'analyze_snapshot' | 'export_snapshot'
  state: MarketOperationState
  priority: number
  idempotency_key?: string | null
  payload: JsonRecord
  not_before?: string | null
  lease_owner?: string | null
  lease_deadline?: string | null
  current_attempt: number
  last_error?: string | null
}

export interface MarketOperationAttempt {
  attempt_id: string
  operation_id: string
  attempt_number: number
  state: string
  worker_id?: string | null
  transport_id?: string | null
  requested_cursor?: JsonRecord | null
  reported_cursor?: JsonRecord | null
  response_status?: number | null
  response_bytes?: number | null
  duration_ms?: number | null
  received_count: number
  new_unique_count: number
  duplicate_count: number
  page_fingerprint?: string | null
  raw_response_ref?: string | null
  failure_kind?: string | null
  retry_after?: string | null
  started_at: string
  finished_at?: string | null
  error?: string | null
}

export interface MarketListing {
  listing_id: number
  job_id: string
  listing_key: string
  title?: string | null
  seller_key?: string | null
  price?: number | null
  first_seen_at: string
  last_seen_at: string
  observation_count: number
}

export interface MarketJobSnapshot {
  schema_version?: number
  job_id?: string
  revision?: number
  state?: MarketJobState
  phase?: MarketJobPhase
  counters?: Record<string, number>
  job: MarketJob
  operations?: MarketOperation[]
  workers?: MarketWorker[]
  transports?: MarketTransport[]
  shards?: MarketShard[]
  last_event_sequence?: number
  generated_at?: string
}

export interface CursorPage<T> {
  items: T[]
  limit?: number
  cursor?: string | number | null
  next_cursor?: string | number | null
}

export type MarketJobPage = CursorPage<MarketJob>
export type MarketWorkerPage = CursorPage<MarketWorker>
export type MarketTransportPage = CursorPage<MarketTransport>
export type MarketShardPage = CursorPage<MarketShard>
export type MarketOperationPage = CursorPage<MarketOperation>
export type MarketOperationAttemptPage = CursorPage<MarketOperationAttempt>
export type MarketListingPage = CursorPage<MarketListing>

export interface CreateMarketJobResponse {
  job_id: string
  state: MarketJobState
  phase: MarketJobPhase
  revision: number
  status_url: string
  events_url: string
  initial_operation: MarketOperation
}

export interface MarketResults {
  job_id: string
  revision: number
  state: MarketJobState
  phase: MarketJobPhase
  target_unique_cards: number
  counters: Record<string, number>
  latest_checkpoint?: JsonRecord | null
  metrics?: JsonRecord
  analysis?: JsonRecord
  execution?: JsonRecord
}

export interface MarketRecommendationCreatePayload {
  category_id: number
  classifier_id?: number | null
  service_summary: string
  price: number
  work_time: number
  source_cluster_id?: string | null
  terra_result: Record<string, unknown>
}

export interface MarketRecommendation {
  recommendation_id: string
  job_id: string
  source_cluster_id?: string | null
  state: 'proposed' | 'recommendation_confirmed' | 'rejected' | string
  category_id: number
  classifier_id?: number | null
  service_summary: string
  price: number
  work_time: number
  evidence_ids: string[]
  terra_result: Record<string, unknown>
  content_hash: string
  revision: number
  created_at: string
  updated_at: string
}

export interface MarketDraftHandoff {
  handoff_id: string
  job_id: string
  recommendation_id: string
  state: 'mapping' | 'fields_confirmed' | 'draft_generated' | string
  category_id: number
  classifier_id?: number | null
  service_summary: string
  price: number
  work_time: number
  attribute_manifest: Record<string, unknown>
  attribute_manifest_hash?: string | null
  attribute_selection: Record<string, unknown>
  selection_hash?: string | null
  validation: Record<string, unknown>
  generator_request: Record<string, unknown>
  draft: Record<string, any>
  draft_hash?: string | null
  revision: number
  created_at: string
  updated_at: string
}

export interface MarketPublishedListing {
  published_listing_id: string
  job_id: string
  recommendation_id: string
  handoff_id: string
  source_cluster_id?: string | null
  kwork_id?: string | null
  draft_hash?: string | null
  publish_result: Record<string, unknown>
  feedback: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface MarketRecommendationTransitionResponse {
  recommendation: MarketRecommendation
  handoff: MarketDraftHandoff | null
}

export interface MarketHandoffDraftResponse {
  handoff: MarketDraftHandoff
  draft: Record<string, any>
  image?: Record<string, any> | null
  images?: Array<Record<string, any> | null>
  variants?: Array<Record<string, any>>
}

export interface MarketHandoffPublishResponse {
  handoff: MarketDraftHandoff
  publish: Record<string, any>
  published_listing: MarketPublishedListing | null
}

export interface MarketAssistantResponse {
  context_id: string
  answer: string
  refresh?: JsonRecord | null
  error?: string
}

export type MarketKnownEventType =
  | 'job.snapshot'
  | 'job.state_changed'
  | 'job.phase_changed'
  | 'job.metrics'
  | 'worker.registered'
  | 'worker.state_changed'
  | 'worker.heartbeat'
  | 'worker.identity_bound'
  | 'worker.concurrency_recommended'
  | 'transport.state_changed'
  | 'request.started'
  | 'request.finished'
  | 'request.wave_waiting'
  | 'request.wave_released'
  | 'operation.queued'
  | 'operation.started'
  | 'operation.completed'
  | 'operation.failed'
  | 'operation.contract_violation'
  | 'enrichment.queued'
  | 'enrichment.completed'
  | 'shard.progress'
  | 'checkpoint.saved'
  | 'warning'
  | 'result.ready'
  | 'publication.state_changed'

export interface MarketJobEvent {
  schema_version: number
  seq: number
  job_id: string
  revision?: number
  type: MarketKnownEventType
  emitted_at: string
  worker_id?: string | null
  operation_id?: string | null
  payload: JsonRecord
}

export interface MarketResyncRequired {
  schema_version?: number
  seq?: number
  job_id: string
  type: 'resync_required'
  payload?: JsonRecord
}

export type MarketStreamMessage = MarketJobEvent | MarketResyncRequired

export interface MarketEventPage extends CursorPage<MarketJobEvent> {
  last_seq?: number
}

export interface MarketPaginationParams {
  cursor?: string | number | null
  limit?: number
}

export interface MarketJobsQuery {
  offset?: number
  limit?: number
  state?: MarketJobState | null
}

export interface MarketOperationsQuery extends MarketPaginationParams {
  state?: MarketOperationState | null
}

export interface MarketListingsQuery extends MarketPaginationParams {
  shard?: string | null
}

export interface MarketEventsQuery {
  after_seq?: number
  limit?: number
  tail?: boolean
}

export interface StopMarketJobPayload {
  force?: boolean
}

export interface MarketWorkerCommand {
  command_id: string
  job_id?: string | null
  worker_id?: string | null
  command_type: 'drain' | 'restart' | 'disable' | 'rotate' | 'reconnect'
  state: 'queued' | 'acknowledged' | 'completed' | 'failed'
  payload: JsonRecord
  created_at: string
  acknowledged_at?: string | null
  completed_at?: string | null
  error?: string | null
}

export interface MarketAccountPoolSummary {
  accounts_total: number
  accounts_eligible: number
  accounts_selected?: number
  account_selection_mode?: 'automatic' | 'manual'
  accounts_active: number
  routes_healthy: number
  provider_profiles_total?: number | null
  routes_verified: number
  egress_ips_distinct: number
  egress_ips_active: number
  effective_capacity: number
}

export interface MarketAccountPoolAccount {
  registration_id: string
  username: string
  email: string
  status: string
  market_enabled: boolean
  session_cookie_count: number
  signup_ip?: string | null
  registration_slot?: number | null
  registration_transport_id?: string | null
  preferred_slot?: number | null
  preferred_transport_id?: string | null
  persona_id?: string | null
  last_used_at?: string | null
  last_error?: string | null
  created_at?: string | null
  updated_at?: string | null
  selected_for_job?: boolean
}

export interface MarketAccountPoolBinding {
  worker_id: string
  job_id: string
  registration_id: string
  username?: string | null
  transport_id?: string | null
  slot?: number | null
  proxy_url?: string | null
  current_egress_ip?: string | null
  egress_ip?: string | null
  signup_ip?: string | null
  preferred_slot?: number | null
  persona_id?: string | null
  binding_mode?: string | null
  state: string
  route_health?: string | null
  lease_deadline?: string | null
  assigned_at?: string | null
  released_at?: string | null
  last_error?: string | null
}

export interface MarketAccountPoolRoute {
  transport_id: string
  slot?: number | null
  proxy_url?: string | null
  egress_ip?: string | null
  health?: string | null
  profile_id?: string | null
  profile_name?: string | null
  country?: string | null
  lease_owner?: string | null
}

export interface MarketAccountPoolSnapshot {
  job_id?: string | null
  summary: MarketAccountPoolSummary
  accounts: MarketAccountPoolAccount[]
  bindings: MarketAccountPoolBinding[]
  routes: MarketAccountPoolRoute[]
}

export interface MarketAccountEnabledPayload {
  enabled: boolean
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isKnownEventType(value: unknown): value is MarketKnownEventType {
  return typeof value === 'string' && [
    'job.snapshot',
    'job.state_changed',
    'job.phase_changed',
    'job.metrics',
    'worker.registered',
    'worker.state_changed',
    'worker.heartbeat',
    'worker.identity_bound',
    'worker.concurrency_recommended',
    'transport.state_changed',
    'request.started',
    'request.finished',
    'request.wave_waiting',
    'request.wave_released',
    'operation.queued',
    'operation.started',
    'operation.completed',
    'operation.failed',
    'operation.contract_violation',
    'enrichment.queued',
    'enrichment.completed',
    'shard.progress',
    'checkpoint.saved',
    'warning',
    'result.ready',
    'publication.state_changed',
  ].includes(value as MarketKnownEventType)
}

export function parseMarketStreamMessage(value: unknown): MarketStreamMessage | null {
  if (!isRecord(value) || typeof value.type !== 'string' || typeof value.job_id !== 'string') return null
  if (value.type === 'resync_required') {
    return {
      schema_version: typeof value.schema_version === 'number' ? value.schema_version : undefined,
      seq: typeof value.seq === 'number' ? value.seq : undefined,
      job_id: value.job_id,
      type: 'resync_required',
      payload: isRecord(value.payload) ? (value.payload as JsonRecord) : undefined,
    }
  }
  if (
    !isKnownEventType(value.type) ||
    typeof value.schema_version !== 'number' ||
    typeof value.seq !== 'number' ||
    typeof value.emitted_at !== 'string' ||
    !isRecord(value.payload)
  ) {
    return null
  }
  return {
    schema_version: value.schema_version,
    seq: value.seq,
    job_id: value.job_id,
    revision: typeof value.revision === 'number' ? value.revision : undefined,
    type: value.type,
    emitted_at: value.emitted_at,
    worker_id: typeof value.worker_id === 'string' ? value.worker_id : null,
    operation_id: typeof value.operation_id === 'string' ? value.operation_id : null,
    payload: value.payload as JsonRecord,
  }
}

export function isMarketJobSnapshot(value: unknown): value is MarketJobSnapshot {
  if (!isRecord(value) || !isRecord(value.job)) return false
  return typeof value.job.job_id === 'string' && typeof value.job.revision === 'number'
}
