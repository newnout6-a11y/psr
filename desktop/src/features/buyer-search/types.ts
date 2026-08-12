export type BuyerRunMode = 'brief' | 'category' | 'manual' | 'hybrid'
export type BuyerRunState = 'draft' | 'planning' | 'ready' | 'running' | 'pausing' | 'paused' | 'completing' | 'completed' | 'stopping' | 'stopped' | 'blocked' | 'failed'
export type BuyerQueryState = 'generated' | 'approved' | 'assigned' | 'running' | 'exhausted' | 'disabled' | 'rejected' | 'failed'

export interface BuyerSearchRun {
  run_id: string
  name: string
  mode: BuyerRunMode
  state: BuyerRunState
  brief: string | null
  category_scope: Record<string, unknown>
  filters: Record<string, unknown>
  requested_workers: number
  query_batch_size: number
  target_unique_projects: number
  config_version: number
  counters: Record<string, number>
  created_at: string
  started_at: string | null
  completed_at: string | null
  stopped_at: string | null
  last_error: string | null
  updated_at: string
}

export interface BuyerSearchQuery {
  query_id: string
  run_id: string
  text: string
  normalized_text: string
  origin: string
  rationale: string
  category_id: number | null
  category_path: number[]
  filters: Record<string, unknown>
  priority: number
  approved: boolean
  enabled: boolean
  state: BuyerQueryState
  pages_scheduled: number
  pages_completed: number
  projects_seen: number
  unique_projects: number
  predicted_total: number | null
}

export interface BuyerSearchQueryUpdateResult {
  query: BuyerSearchQuery
  distribution: {
    queued_count: number
    queued_query_ids: string[]
    skipped_query_ids: string[]
  } | null
}

export interface BuyerSearchQueryGenerationRequest {
  mode?: BuyerRunMode
  brief?: string
  category_scope?: Record<string, unknown>
  filters?: Record<string, unknown>
  queries?: Array<{ text: string; rationale?: string }>
  requested_workers?: number
  query_batch_size?: number
  minimum_count?: number
  auto_approve?: boolean
}

export interface BuyerSearchGeneratedQuery {
  query_id: string
  run_id: string
  text: string
  normalized_text: string
  origin: string
  parent_query_id?: string | null
  rationale: string
  priority: number
  category_id: number | null
  category_path: number[]
  filters: Record<string, unknown>
  filter_json?: string
  filter_hash?: string
  semantic_fingerprint?: string
  predicted_total?: number | null
  approved: boolean
  enabled: boolean
  state: BuyerQueryState
  stable_hash?: string
}

export interface BuyerSearchQueryGenerationResult {
  items: BuyerSearchGeneratedQuery[]
  collision_count: number
  generator: string
  used_fallback: boolean
  distribution: {
    queued_count: number
    queued_query_ids: string[]
    skipped_query_ids: string[]
  }
}

export interface BuyerSearchQueryCollision {
  seq: number
  created_at?: string | null
  kind?: string
  first_query_id?: string
  second_query_id?: string
  first_text?: string
  second_text?: string
  score?: number | null
  key?: unknown
}

export interface BuyerSearchQueryCollisionReport {
  run_id: string
  items: BuyerSearchQueryCollision[]
  scanned_event_count: number
  next_after_seq: number
}

export interface BuyerSearchQueryCollisionRegenerationRequest extends BuyerSearchQueryGenerationRequest {
  collision_query_ids: string[]
}

export interface BuyerSearchQueryCollisionRegenerationResult {
  collision_query_ids: string[]
  generation: BuyerSearchQueryGenerationResult
  collision_report: BuyerSearchQueryCollisionReport
}

export interface BuyerSearchQueryBundleItem {
  query_id: string
  text: string | null
  priority: number | null
  state: string | null
  approved: boolean
  enabled: boolean
  assignment_order: number
  sources: string[]
  task_count: number
}

export interface BuyerSearchQueryBundle {
  worker_index: number
  queries: BuyerSearchQueryBundleItem[]
  task_count: number
  sources: string[]
}

export interface BuyerSearchQueryBundles {
  run_id: string
  requested_workers: number
  bundles: BuyerSearchQueryBundle[]
  eligible_query_count: number
  excluded_query_ids: string[]
}

export interface BuyerSearchProject {
  project_id: string
  remote_project_id: string
  title: string
  description_excerpt: string
  budget_min: number | null
  budget_max: number | null
  offers: number | null
  views: number | null
  age_seconds: number | null
  category_id: number | null
  buyer_hired_percent: number | null
  buyer_username?: string | null
  buyer_projects_count?: number | null
  buyer_active_projects_count?: number | null
  orders?: number | null
  expires_at?: string | null
  attachment_count: number
  attachment_parse_state: string | null
  preliminary_score: number | null
  final_score: number | null
  matched_query_count: number
  shortlist_state: string
  proposal_state: string | null
  conversation_state: string | null
  unseen: boolean
  tags?: string[]
  updated_at: string
}

export interface BuyerSearchAttachment {
  attachment_id: string
  filename: string | null
  detected_type: string | null
  state: string | null
  size_bytes: number | null
  sha256?: string | null
  content_type?: string | null
  error?: string | null
  object_ref?: string | null
  derivatives?: BuyerSearchAttachmentDerivative[]
}

export interface BuyerSearchAttachmentDerivative {
  derivative_id?: string
  kind?: string
  parser_name?: string | null
  parser_version?: string | null
  state?: string | null
  token_count?: number | null
  error?: string | null
  metadata?: Record<string, unknown>
}

export interface BuyerSearchScoreRecord {
  score_id?: string
  score_kind?: string
  score_profile_id?: string
  score_profile_version?: string
  total_score?: number | null
  rationale?: string | null
  breakdown?: Record<string, unknown>
  created_at?: string | null
}

export interface BuyerSearchShortlist {
  state?: string
  tags?: string[]
  notes?: Array<{ body?: string; author?: string; created_at?: string }>
  selected_at?: string | null
}

export interface BuyerSearchProjectDetail extends BuyerSearchProject {
  canonical_url?: string | null
  description?: string | null
  attachments?: BuyerSearchAttachment[]
  matches?: Array<{ query_id?: string; query_text?: string; normalized_query_text?: string }>
  observations?: Array<Record<string, unknown>>
  scores?: BuyerSearchScoreRecord[]
  shortlist?: BuyerSearchShortlist | null
}

export interface BuyerSearchFacet {
  key: string
  values: Array<{ value: string | number | boolean | null; count: number }>
}

export interface BuyerSearchFacetValue {
  value: string | number | boolean | null
  count: number
}

export interface BuyerSearchFacets {
  total: number
  budget?: { min: number | null; max: number | null }
  preliminary_score?: { min: number | null; max: number | null }
  final_score?: { min: number | null; max: number | null }
  score?: { min: number | null; max: number | null }
  categories?: BuyerSearchFacetValue[]
  shortlist_states?: BuyerSearchFacetValue[]
  attachment_parse_states?: BuyerSearchFacetValue[]
  attachment_types?: BuyerSearchFacetValue[]
  proposal_states?: BuyerSearchFacetValue[]
  conversation_states?: BuyerSearchFacetValue[]
  tags?: BuyerSearchFacetValue[]
}

export interface BuyerSearchPage<T> {
  items: T[]
  next_cursor: string | null
  total?: number
  run_total?: number
}

export interface BuyerSearchFleetCapacity {
  run_id: string
  requested_workers: number
  canary_cap: number
  candidate_count: number
  eligible_accounts: number
  healthy_transports: number
  transports_with_fresh_verified_egress: number
  unique_egress_ips: number
  collision_count: number
  effective_capacity: number
  reasons: string[]
}

export interface BuyerSearchFleetCurrentTask {
  task_id?: string
  query_id?: string
  query_text?: string
  title?: string
  source?: string
  page?: number
}

export interface BuyerSearchExecutionWorker {
  worker_id?: string | null
  account_registration_id: string
  transport_id: string
  egress_ip: string
  pages: number
  projects_seen: number
}

export interface BuyerSearchExecutionMetrics {
  duration_seconds: number
  completed_requests: number
  projects_seen: number
  unique_projects: number
  duplicate_observations: number
  uniqueness_percent: number
  requests_per_second: number
  projects_per_second: number
  unique_projects_per_second: number
  average_latency_ms: number | null
  measured_latency_count: number
  sources: Array<{ source: string; pages: number; projects_seen: number }>
  worker_history: BuyerSearchExecutionWorker[]
}

export interface BuyerSearchFleetWorker {
  worker_id: string
  account_registration_id: string
  transport_id: string
  egress_ip: string
  state: string
  started_at: string | null
  last_heartbeat_at: string | null
  last_outcome: string | null
  last_error: string | null
  current_task?: string | BuyerSearchFleetCurrentTask | null
  current_task_id?: string | null
  task_id?: string | null
}

export interface BuyerSearchFleet {
  run_id: string
  state: string
  requested_workers: number
  configured_max_workers?: number
  canary_cap?: number
  effective_workers: number
  capacity_mode?: string
  live_discovery_enabled?: boolean
  capacity_reason?: string | null
  reason?: string | null
  capacity?: BuyerSearchFleetCapacity
  workers?: BuyerSearchFleetWorker[]
  metrics?: BuyerSearchExecutionMetrics
}

export type BuyerSearchProjectSort =
  | 'updated_desc'
  | 'score_desc'
  | 'preliminary_score_desc'
  | 'final_score_desc'
  | 'budget_desc'
  | 'offers_asc'
  | 'views_desc'
  | 'attachment_count_desc'
  | 'matched_queries_desc'
  | 'project_id_asc'

export interface BuyerSearchProjectListOptions {
  cursor?: string
  limit?: number
  sort?: BuyerSearchProjectSort
  shortlist?: string
  minScore?: number
  maxBudget?: number
  maxOffers?: number
  categoryId?: number
  text?: string
  maxScore?: number
  minBudget?: number
  minOffers?: number
  minViews?: number
  maxViews?: number
  minBuyerHiredPercent?: number
  minAgeSeconds?: number
  maxAgeSeconds?: number
  hasAttachments?: boolean
  attachmentParseState?: string[]
  attachmentType?: string[]
  tags?: string[]
  proposalState?: string[]
  conversationState?: string[]
  unseen?: boolean
  signal?: AbortSignal
}

export interface BuyerSearchWorkspaceBuilder {
  name: string
  brief: string
  exact_queries: string[]
  taxonomy_selections: Array<Record<string, unknown>>
  min_budget: string
  max_budget: string
  max_offers: string
  min_buyer_hired_percent: string
  max_age_hours: string
  target_projects: number
  workers: number
  query_batch_size: number
  account_registration_ids: string[]
  enrichment_enabled: boolean
  scoring_profile_id: string
  advanced_open: boolean
  preview_queries: Array<{ text: string; enabled?: boolean; rationale?: string }>
}

export type BuyerSearchWorkspaceSection = 'setup' | 'projects' | 'shortlist' | 'outreach' | 'monitoring'

export interface BuyerSearchWorkspaceView {
  active_section: BuyerSearchWorkspaceSection
  selected_run_id: string | null
  selected_project_id: string | null
  project_filters: Record<string, unknown>
  project_sort: BuyerSearchProjectSort
  cursor: string | null
  cursor_history: string[]
  selected_project_ids: string[]
  shortlist_tags: string
  shortlist_note: string
  include_attachments: boolean
  inspector_tab: 'details' | 'proposal' | 'conversation'
  inspector_open: boolean
  runs_pane_width: number
  inspector_width: number
}

export interface BuyerSearchWorkspaceState {
  builder: BuyerSearchWorkspaceBuilder
  view: BuyerSearchWorkspaceView
}

export interface BuyerSearchWorkspaceSnapshot {
  workspace_id?: string
  schema_version: number
  state: BuyerSearchWorkspaceState | Record<string, never>
  updated_at: string | null
}

export interface BuyerSearchBatchProjectActionPayload {
  action: 'shortlist' | 'unshortlist'
  project_ids: string[]
  tags?: string[]
  note?: string
  selected_by?: string
}

export interface BuyerSearchBatchProjectActionResult {
  action: string
  applied_count: number
  items: BuyerSearchShortlist[]
}

export type BuyerSearchExportFormat = 'jsonl' | 'csv' | 'markdown' | 'txt' | 'zip'

export interface BuyerSearchExportRequest {
  format: BuyerSearchExportFormat
  filters?: Record<string, unknown>
  selected_project_ids?: string[]
  include_attachments?: boolean
  include_raw?: boolean
}

export interface BuyerSearchExport {
  export_id: string
  run_id?: string
  format: BuyerSearchExportFormat | string
  selection?: Record<string, unknown>
  include_attachments?: boolean
  include_raw?: boolean
  state: string
  progress?: Record<string, unknown>
  filename?: string | null
  content_type?: string | null
  bytes?: number | null
  manifest?: Record<string, unknown> | null
  sha256?: string | null
  error?: string | null
  created_at?: string | null
  completed_at?: string | null
}

export interface BuyerSearchEnrichmentResult {
  run_id: string
  project_id: string
  items: Array<{
    attachment_id?: string
    filename?: string | null
    state?: string
    error?: string | null
  }>
  bytes_downloaded: number
  parsed_count: number
  context?: { context_hash?: string; character_count?: number; attachment_count?: number }
  context_manifest?: Record<string, unknown>
}

export interface BuyerSearchFinalScoreRequest {
  profile_id?: string
  profile_version?: string
}

export interface BuyerSearchEvent {
  seq: number
  type: string
  payload: Record<string, unknown>
  created_at?: string
}

export type BuyerSearchWebSocketMessage =
  | { type: 'snapshot'; run: BuyerSearchRun; after_seq: number }
  | { type: 'event'; event: BuyerSearchEvent }

export interface CreateBuyerSearchRunPayload {
  name: string
  mode: BuyerRunMode
  brief?: string
  category_scope?: Record<string, unknown>
  filters?: Record<string, unknown>
  requested_workers: number
  query_batch_size?: number
  target_unique_projects?: number
  enrichment_policy?: Record<string, unknown>
  scoring_profile_id?: string
  queries?: Array<{
    text: string
    rationale?: string
    category_id?: number | null
    category_path?: number[]
    filters?: Record<string, unknown>
    priority?: number
  }>
}

export interface BuyerSearchRunPatch {
  state?: Extract<BuyerRunState, 'paused' | 'running' | 'stopped'>
  name?: string
  filters?: Record<string, unknown>
  requested_workers?: number
}

export interface BuyerSearchQueryPatch {
  text?: string
  filters?: Record<string, unknown>
  category_id?: number | null
  category_path?: number[]
  rationale?: string
  origin?: string
  parent_query_id?: string | null
  priority?: number
  predicted_total?: number | null
  approved?: boolean
  enabled?: boolean
  state?: BuyerQueryState
}
