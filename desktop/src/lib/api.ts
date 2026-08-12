const BASE = 'http://127.0.0.1:7788'
export const API_BASE = BASE

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let body: Record<string, unknown> = {}
    try {
      body = text ? JSON.parse(text) : {}
    } catch {
      body = {}
    }
    const code = typeof body.code === 'string' && body.code ? ` (${body.code})` : ''
    const detail =
      typeof body.detail === 'string' && body.detail
        ? body.detail
        : typeof body.message === 'string' && body.message
          ? body.message
          : text || res.statusText
    throw new Error(`HTTP ${res.status}${code}: ${detail}`)
  }
  return res.json()
}

const formManifestCache = new Map<string, { expiresAt: number; value: KworkFormManifest }>()
const formManifestRequests = new Map<string, Promise<KworkFormManifest>>()

function formManifestCacheKey(categoryId: number, payload: KworkFormManifestRequest): string {
  const selection = Object.fromEntries(Object.entries(payload.selection || {}).sort(([left], [right]) => left.localeCompare(right)))
  return JSON.stringify({ categoryId, classifierId: payload.classifier_id || null, lang: payload.lang || 'ru', selection })
}

function getKworkFormManifestCached(categoryId: number, payload: KworkFormManifestRequest): Promise<KworkFormManifest> {
  const key = formManifestCacheKey(categoryId, payload)
  const cached = formManifestCache.get(key)
  if (cached && cached.expiresAt > Date.now()) return Promise.resolve(cached.value)
  const pending = formManifestRequests.get(key)
  if (pending) return pending
  const next = request<KworkFormManifest>(`/api/kwork/market/category/${categoryId}/form-manifest`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((manifest) => {
    if (manifest.success !== false && manifest.code !== 'manual_verification_required') {
      formManifestCache.set(key, { expiresAt: Date.now() + 45_000, value: manifest })
    }
    return manifest
  }).finally(() => formManifestRequests.delete(key))
  formManifestRequests.set(key, next)
  return next
}

// ── Orchestrator ────────────────────────────────────────────────────────────

export interface RuntimeConfig {
  platforms?: string[]
  pages_to_parse?: number
  query_count?: number
  top_projects?: number
  discovery_mode?: string
  max_pages_per_query?: number
  max_projects_per_cycle?: number
  max_parse_seconds?: number
  ai_score_mode?: string
  ai_score_batch_size?: number
  ai_score_max_candidates?: number
  browser_headless?: boolean
  osint_enabled?: boolean
  probiv_enabled?: boolean
  telegram_configured?: boolean
  session_hub_required?: boolean
}

export interface StartCycleParams {
  dry_run?: boolean
  limit?: number
  continuous?: boolean
  platforms?: string[]
  pages_to_parse?: number
  query_count?: number
  top_projects?: number
  discovery_mode?: string
  max_pages_per_query?: number
  max_projects_per_cycle?: number
  max_parse_seconds?: number
  ai_score_mode?: string
  ai_score_batch_size?: number
  ai_score_max_candidates?: number
  search_brief?: string
  browser_headless?: boolean
  osint_enabled?: boolean
  probiv_enabled?: boolean
  telegram_enabled?: boolean
  session_hub_required?: boolean
}

export const api = {
  health: () => request<{ status: string }>('/api/health'),
  clearLogs: () => request<{ ok: boolean }>('/api/logs', { method: 'DELETE' }),

  // Orchestrator
  getStatus: () =>
    request<{
      cycle_running: boolean
      execution_mode: string
      paused_platforms: string[]
      last_cycle_stats: Record<string, any> | null
      last_error: string | null
      runtime_config?: RuntimeConfig
    }>('/api/orchestrator/status'),

  startCycle: (params: StartCycleParams) =>
    request<{ ok: boolean; message: string }>('/api/orchestrator/start', {
      method: 'POST',
      body: JSON.stringify(params),
    }),

  stopCycle: () =>
    request<{ ok: boolean; message: string }>('/api/orchestrator/stop', { method: 'POST' }),

  getTelegramStatus: () =>
    request<{
      configured: boolean
      admin_chat_id_set: boolean
      transport: string
      bot_api_base: string
      bot_api: { ok: boolean; detail: string; status_code?: number }
      mtproto_configured: boolean
    }>('/api/telegram/status'),

  testTelegram: (payload?: { text?: string; chat_id?: string; transport?: string }) =>
    request<{
      ok: boolean
      method: string
      detail: string
      attempts: Array<{ method: string; ok: boolean; detail: string; status_code?: number }>
    }>('/api/telegram/test', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),

  getKworkStatus: () =>
    request<{
      configured: boolean
      api_ok: boolean
      api_error?: string | null
      session_hub_url: string
      session_hub_required: boolean
      state_parser: string
    }>('/api/kwork/status'),

  registerKworkAccount: (payload: KworkRegistrationRequest) =>
    request<KworkRegistrationResult>('/api/kwork/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  registerKworkAccountsBatch: (payload: KworkRegistrationBatchRequest) =>
    request<KworkRegistrationBatchResult>('/api/kwork/register/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  verifyKworkRegistration: (payload: KworkRegistrationVerificationRequest) =>
    request<KworkRegistrationResult>('/api/kwork/register/verify', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkRegistrationCredentials: (registrationId: string) =>
    request<KworkRegistrationCredentials>('/api/kwork/register/credentials', {
      method: 'POST',
      body: JSON.stringify({ registration_id: registrationId }),
    }),

  listKworkRegistrationAccounts: () =>
    request<{ accounts: KworkRegistrationAccount[]; total: number }>('/api/kwork/register/accounts'),

  checkKworkRegistrationAccountSession: (registrationId: string) =>
    request<KworkRegistrationAccountSessionCheck>(
      `/api/kwork/register/accounts/${encodeURIComponent(registrationId)}/session-check`,
      { method: 'POST' },
    ),

  deleteKworkRegistrationAccount: (registrationId: string) =>
    request<{ ok: boolean; registration_id: string }>(
      `/api/kwork/register/accounts/${encodeURIComponent(registrationId)}`,
      { method: 'DELETE' },
    ),

  getKworkVerificationStatus: (writeFile = false) =>
    request<KworkVerificationStatus>(`/api/kwork/verification-status?write_file=${writeFile ? 'true' : 'false'}`),

  inspectKworkProject: (projectId: string) =>
    request<{
      ok: boolean
      url: string
      state_found: boolean
      detail?: string
      project?: KworkInspectProject
    }>(`/api/kwork/inspect/${encodeURIComponent(projectId)}`),

  getKworkMarketCategories: () =>
    request<{ categories: KworkCategoryNode[]; raw?: Record<string, unknown> }>('/api/kwork/market/categories'),

  getKworkCatalogAliases: () =>
    request<{ schema_version: number; items: KworkCatalogAlias[]; total: number }>('/api/kwork/market/catalog-aliases'),

  getKworkCategoryAttributes: (categoryId: number) =>
    request<KworkCategoryAttributes>(`/api/kwork/market/category/${categoryId}/attributes`),

  getKworkCategoryPrices: (categoryId: number, attributeId?: number) => {
    const qs = attributeId ? `?attribute_id=${attributeId}` : ''
    return request<KworkPriceRules>(`/api/kwork/market/category/${categoryId}/prices${qs}`)
  },

  getKworkFormManifest: (categoryId: number, payload: KworkFormManifestRequest) =>
    getKworkFormManifestCached(categoryId, payload),

  suggestKworkAttribute: (categoryId: number, payload: KworkAttributeSuggestRequest) =>
    request<KworkAttributeSuggestResult>(`/api/kwork/market/category/${categoryId}/attribute-suggest`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkMarketMetrics: (params: {
    category_id: number
    classifier_id?: number
    include_demand?: boolean
    include_competitor_details?: boolean
    competitor_detail_limit?: number
    page?: number
    attribute_selection?: Record<string, unknown>
    attribute_controls?: KworkFormControl[]
  }) =>
    request<KworkMarketMetrics>('/api/kwork/market/metrics', {
      method: 'POST',
      body: JSON.stringify(params),
    }),

  getKworkSupplyScan: (payload: KworkSupplyScanRequest) =>
    request<KworkSupplyScan>('/api/kwork/market/supply-scan', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  askKworkMarketAssistant: (payload: KworkMarketAssistantRequest) =>
    request<KworkMarketAssistantResponse>('/api/kwork/market/assistant', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkWebCatalog: (alias: string, options?: { page?: number; page_size?: number; include_raw?: boolean }) => {
    const qs = new URLSearchParams()
    if (options?.page !== undefined) qs.set('page', String(options.page))
    if (options?.page_size !== undefined) qs.set('page_size', String(options.page_size))
    if (options?.include_raw !== undefined) qs.set('include_raw', String(options.include_raw))
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    return request<KworkWebCatalogSummary>(`/api/kwork/market/web-catalog/${encodeURIComponent(alias)}${suffix}`)
  },

  getKworkWebCatalogSnapshot: (payload: KworkWebCatalogSnapshotRequest = {}) =>
    request<KworkWebCatalogSnapshot>('/api/kwork/market/web-catalog-snapshot', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkMarketIntelligenceSnapshot: (payload: KworkMarketIntelligenceRequest = {}) =>
    request<KworkMarketIntelligenceSnapshot>('/api/kwork/market/intelligence-snapshot', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkBuyerScout: (payload: KworkBuyerScoutRequest = {}) =>
    request<KworkBuyerScout>('/api/kwork/market/buyer-scout', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKworkMarketIntelligenceHistory: (limit = 20) =>
    request<KworkMarketIntelligenceHistory>(`/api/kwork/market/intelligence-history?limit=${limit}`),

  createKworkDraft: (payload: KworkDraftRequest) =>
    request<KworkDraftResult>('/api/kwork/autopublish/draft', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  preflightKworkPublish: (draft: Record<string, unknown>) =>
    request<KworkPublishPreflightResult>('/api/kwork/autopublish/preflight', {
      method: 'POST',
      body: JSON.stringify({ draft }),
    }),

  publishKworkDraft: (
    draft: Record<string, unknown>,
    dryRun = true,
    options?: { confirm_token?: string; confirmation?: string },
  ) =>
    request<KworkPublishResult>('/api/kwork/autopublish/publish', {
      method: 'POST',
      body: JSON.stringify({ draft, dry_run: dryRun, ...(options || {}) }),
    }),

  setMode: (mode: string) =>
    request<{ ok: boolean; mode: string }>('/api/orchestrator/mode', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),

  pausePlatform: (platform: string) =>
    request<{ ok: boolean }>(`/api/orchestrator/pause/${platform}`, { method: 'POST' }),

  resumePlatform: (platform: string) =>
    request<{ ok: boolean }>(`/api/orchestrator/resume/${platform}`, { method: 'POST' }),

  // Candidates
  getCandidates: (params?: { status?: string; platform?: string; page?: number; page_size?: number }) => {
    const qs = new URLSearchParams()
    if (params?.status) qs.set('status', params.status)
    if (params?.platform) qs.set('platform', params.platform)
    if (params?.page !== undefined) qs.set('page', String(params.page))
    if (params?.page_size !== undefined) qs.set('page_size', String(params.page_size))
    return request<{ items: Candidate[]; total: number }>(`/api/candidates?${qs}`)
  },

  getCandidate: (id: number) => request<Candidate>(`/api/candidates/${id}`),

  approveCandidate: (id: number, payload?: { proposal_text?: string; chosen_price?: string }) =>
    request<{ ok: boolean; message: string }>(`/api/candidates/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),

  skipCandidate: (id: number) =>
    request<{ ok: boolean }>(`/api/candidates/${id}/skip`, { method: 'POST' }),

  snoozeCandidate: (id: number, minutes: number) =>
    request<{ ok: boolean }>(`/api/candidates/${id}/snooze`, {
      method: 'POST',
      body: JSON.stringify({ minutes }),
    }),

  editText: (id: number, proposal_text: string) =>
    request<{ ok: boolean }>(`/api/candidates/${id}/text`, {
      method: 'PATCH',
      body: JSON.stringify({ proposal_text }),
    }),

  editPrice: (id: number, chosen_price: string) =>
    request<{ ok: boolean }>(`/api/candidates/${id}/price`, {
      method: 'PATCH',
      body: JSON.stringify({ chosen_price }),
    }),

  // Dashboard
  getOverview: (days = 14) => request<OverviewMetrics>(`/api/dashboard/overview?days=${days}`),
  getTimeline: (days = 14) => request<TimelineItem[]>(`/api/dashboard/timeline?days=${days}`),
  getScoring: (days = 14) => request<ScoringRow[]>(`/api/dashboard/scoring?days=${days}`),
  getStatusBreakdown: (days = 14) => request<StatusRow[]>(`/api/dashboard/status-breakdown?days=${days}`),
  getQueryMemory: (limit = 20) => request<QueryMemoryRow[]>(`/api/dashboard/query-memory?limit=${limit}`),
  getParseStats: (days = 14) => request<ParseStatRow[]>(`/api/dashboard/parse-stats?days=${days}`),
  getSendStats: (days = 14) => request<SendStatRow[]>(`/api/dashboard/send-stats?days=${days}`),
  getBreaker: () => request<BreakerRow[]>('/api/dashboard/breaker'),
  getGenerationStats: (days = 14) => request<GenerationRow[]>(`/api/dashboard/generation-stats?days=${days}`),
  getActivity: () => request<Record<string, string | null>>('/api/dashboard/activity'),
  getErrors: (limit = 50) => request<ErrorRow[]>(`/api/dashboard/errors?limit=${limit}`),
  getRuntimeState: () => request<{ execution_mode: string; paused_platforms: string[] }>('/api/dashboard/runtime-state'),

  // Settings
  getEnv: () => request<{ values: Record<string, string>; secret_keys: string[]; mask: string }>('/api/settings/env'),
  revealEnvValue: (key: string) =>
    request<{ key: string; value: string }>(`/api/settings/env/reveal/${encodeURIComponent(key)}`),
  updateEnv: (values: Record<string, string>) =>
    request<{ ok: boolean; updated: string[] }>('/api/settings/env', {
      method: 'PUT',
      body: JSON.stringify({ values }),
    }),
  getNetworkStatus: (probe = true) =>
    request<NetworkStatus>(`/api/settings/network/status?probe=${probe ? 'true' : 'false'}`),
  startVpnteProxy: (payload?: VpnteActionPayload) =>
    request<VpnteActionResult>('/api/settings/network/vpnte/start', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),
  rotateVpnteProxy: (payload?: VpnteActionPayload) =>
    request<VpnteActionResult>('/api/settings/network/vpnte/rotate', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),
  connectVpnteProxy: (payload: VpnteActionPayload) =>
    request<VpnteActionResult>('/api/settings/network/vpnte/connect', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  triggerVpnteProxy: (payload: VpnteActionPayload) =>
    request<VpnteActionResult>('/api/settings/network/vpnte/trigger', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  stopVpnteProxy: (payload: Pick<VpnteActionPayload, 'slot'>) =>
    request<VpnteActionResult>('/api/settings/network/vpnte/stop', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getFilters: () => request<{ data: FiltersData }>('/api/settings/filters'),
  updateFilters: (data: FiltersData) =>
    request<{ ok: boolean }>('/api/settings/filters', {
      method: 'PUT',
      body: JSON.stringify({ data }),
    }),

  // OSINT
  osintCheck: (params: { username: string; description?: string; email?: string; phone?: string; telegram?: string }) =>
    request<OSINTResult>('/api/osint/check', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
}

// ── Types ───────────────────────────────────────────────────────────────────

export interface ProjectFile {
  fname?: string
  name?: string
  url?: string
  size?: number
}

export interface PlatformData {
  files?: ProjectFile[]
  category_id?: number
  possible_price_limit?: number
  allow_higher_price?: boolean
  available_durations?: number[]
  date_create?: string
  source?: string
  [key: string]: unknown
}

export interface Candidate {
  candidate_id: number
  project_id: string
  platform: string
  title: string
  description?: string
  budget?: number
  currency?: string
  skills?: string[]
  url?: string
  status: string
  stage?: string
  ai_score?: number
  ai_score_source?: string
  vet_score?: number
  risk_level?: string
  priority?: number
  offers_count?: number
  proposal_text?: string
  chosen_price?: string
  decision_reason?: string
  vet_reasons?: string[]
  vet_red_flags?: string[]
  client_context?: Record<string, unknown>
  platform_data?: PlatformData
  search_query?: string
  updated_at?: string
  created_at?: string
  snoozed_until?: string
  competitor_prices?: Array<{ price?: string; kwork_name?: string }>
  proposal_image_path?: string
}

export interface KworkInspectProject {
  id: string
  title: string
  description?: string
  budget?: number
  offers_count?: number
  client_hired_percent?: number
  client_user_id?: string
  platform_data?: Record<string, unknown>
}

export interface KworkRegistrationRequest {
  email?: string
  mail_provider: 'catchmail' | 'firstmail'
  mail_password?: string
  user_type: 1 | 2
  promo?: string
  use_simple?: boolean
  track_client_id?: string
  action_after?: string
  is_subscribed?: boolean
  captcha_token?: string
  captcha_field?: 'smart-token' | 'g-recaptcha-response'
  firstmail_api_key?: string
  dry_run?: boolean
}

export interface KworkRegistrationBatchRequest extends KworkRegistrationRequest {
  account_count: number
  vpnte_slots: number[]
  avoid_used_ips?: boolean
}

export interface KworkRegistrationBatchResult {
  ok: boolean
  requested_count: number
  completed_count: number
  activated_count: number
  proxy_count: number
  selected_proxy_count?: number
  reachable_proxy_count?: number
  healthy_proxy_count?: number
  reserve_proxy_count?: number
  failed_proxy_count?: number
  duplicate_proxy_count?: number
  used_ip_proxy_count?: number
  avoid_used_ips?: boolean
  parallel_limit: number
  vpnte_slots: number[]
  selected_vpnte_slots?: number[]
  reserve_vpnte_slots?: number[]
  failed_vpnte_slots?: number[]
  duplicate_vpnte_slots?: number[]
  used_ip_vpnte_slots?: number[]
  results: KworkRegistrationResult[]
}

export interface KworkRegistrationVerificationRequest {
  registration_id: string
  mail_password?: string
  firstmail_api_key?: string
}

export interface KworkRegistrationCredentials {
  registration_id: string
  email: string
  username: string
  password: string
}

export interface KworkRegistrationAccount {
  registration_id: string
  email: string
  username: string
  user_type: 1 | 2
  mail_provider: 'catchmail' | 'firstmail'
  status: string
  created_at?: string
  registration_started_at?: string
  activated_at?: string | null
  signup_ip?: string | null
  activation_ip?: string | null
  session_cookie_count?: number
  last_error?: string | null
}

export interface KworkRegistrationSessionCheck {
  ok: boolean
  auth_mode?: string
  status_code?: number
  final_url?: string
  reason?: string | null
}

export interface KworkRegistrationAccountSessionCheck extends KworkRegistrationAccount {
  session_check: KworkRegistrationSessionCheck
}

export interface KworkRegistrationResult {
  ok: boolean
  status: string
  registration_id?: string
  email: string
  username: string
  mail_provider?: 'catchmail' | 'firstmail'
  registration_started_at?: string
  password_generated?: boolean
  signup_ip?: string
  activation_ip?: string
  preflight_ip?: string
  session_cookie_count?: number
  user_type: 1 | 2
  activated: boolean
  captcha_required: boolean
  phone_fields_sent: boolean
  message?: string
  code?: string
  reason?: string
  status_code?: number
  final_url?: string
  signup_fields?: string[]
  signup_http_status?: number
  signup_fallback_http_status?: number
  activation_link_found?: boolean
  batch_index?: number
  proxy_index?: number | null
  proxy_configured?: boolean
  vpnte_slot?: number
  post_activation?: {
    ok?: boolean
    username?: string
    verified?: boolean
    actor_status?: string
    reason?: string
  }
  session_proof?: {
    ok?: boolean
    auth_mode?: string
    reason?: string
  }
}

export interface KworkCategoryNode {
  id: number
  name: string
  parent_id?: number | string | null
  alias?: string | null
  kworks_count?: number
  raw?: Record<string, unknown>
  children?: KworkCategoryNode[]
}

export interface KworkCatalogAlias {
  category_id: number
  category_name: string
  label: string
  alias: string
  recommended: boolean
}

export interface KworkCategoryAttributes {
  category_id: number
  attributes: KworkAttributeRaw[]
  flat: KworkAttributeFlat[]
  raw?: Record<string, unknown>
}

export interface KworkAttributeRaw {
  id?: number
  name?: string
  required?: boolean
  children?: KworkAttributeRaw[]
  [key: string]: unknown
}

export interface KworkAttributeFlat {
  id: number
  name: string
  path: string
  path_ids: number[]
  required: boolean
  allow_multiple: boolean
  allow_custom: boolean
  percent_usage?: number | string | null
  kworks_count: number
  orders_inprogress_limit?: number | string | null
  has_children: boolean
  raw?: Record<string, unknown>
}

export interface KworkClassifier {
  id: number
  name: string
  kworks_count: number
  raw?: Record<string, unknown>
}

export interface KworkCompetitor {
  id?: number | string
  title?: string
  price?: number | string
  classifier_id?: number | string
  image_url?: string
  share_url?: string
  worker?: string
  worker_avatar?: string
  seller_level?: string
  rating?: number | string
  reviews?: number | string
  is_best?: boolean
  description?: string
  instruction?: string
  service_size?: string
  practice_context?: string
  detail_status?: string
  detail_error?: string
  queue_count?: number
  work_time_seconds?: number
  raw?: Record<string, unknown>
}

export interface KworkDemandSnapshot {
  status: 'ok' | 'needs_cookies' | 'error' | 'skipped' | string
  label?: string
  wants_count?: number
  sample_count?: number
  detail?: string
  sample?: Record<string, unknown>[]
  meta?: Record<string, unknown>
  scope?: Record<string, unknown>
  filter_params?: Record<string, unknown>
}

export interface KworkMarketInsights {
  sample_size?: number
  kworks_count?: number
  price?: {
    min?: number | null
    median?: number | null
    max?: number | null
    sample_size?: number
  }
  seller_review_strength?: {
    reviews_100_plus?: number
    sample_size?: number
    threshold?: number
  }
  seller_repetition_in_sample?: {
    repeated_sellers?: Array<{ seller: string; cards: number }>
    repeated_cards?: number
    sample_size?: number
    repeat_share?: number
  }
  title_terms?: Array<{ term: string; count: number }>
  description_terms?: Array<{ term: string; count: number }>
  top_classifiers?: Array<{ id?: number | string; name?: string; kworks_count?: number }>
  search_queries?: Array<{ query?: string; count?: number; why?: string; source?: string }>
  bullets?: string[]
  recommendations?: string[]
}

export interface KworkMarketMetrics {
  category_id?: number
  classifier_id?: number
  page: number
  kworks_count: number
  classifiers: KworkClassifier[]
  competitors: KworkCompetitor[]
  practice_context?: Array<Record<string, unknown>>
  raw_keys: string[]
  filter_scope?: {
    params?: Record<string, unknown>
    selected?: Array<Record<string, unknown>>
    count?: number
    effective_classifier_ids?: number[]
  }
  filter_requests?: Array<Record<string, unknown>>
  demand: KworkDemandSnapshot
  market_insights?: KworkMarketInsights
}

export interface KworkSupplyScanRequest {
  category_id: number
  category_name?: string
  classifier_id?: number
  classifier_name?: string
  coverage?: 'quick' | 'balanced' | 'deep'
  include_llm?: boolean
  write_file?: boolean
}

export interface KworkSupplyRequestDiagnostic {
  slice_id?: number | string | null
  slice_name?: string
  source?: string
  page?: number | string | null
  requested_page?: number | string | null
  requested_cursor?: { kind?: string; page?: number | null } | number | string | null
  reported_page?: number | string | null
  response_page?: number | string | null
  reported_cursor?: { kind?: string; page?: number | null } | number | string | null
  cursor?: number | string | null
  status?: string
  cached?: boolean
  transport_slot?: number | string | null
  declared_count?: number | null
  card_count?: number | null
  received?: number | null
  received_count?: number | null
  new_unique?: number | null
  new?: number | null
  new_count?: number | null
  duplicates?: number | null
  duplicate?: number | null
  duplicate_count?: number | null
  fingerprint?: string
  page_fingerprint?: string
  response_fingerprint?: string
  contract_violation?: boolean | string
  contract_state?: string
  contract_reason_codes?: string[]
  source_new_unique?: number | null
  source_duplicates?: number | null
  duplicate_rate?: number | null
  failure_kind?: string
  detail?: string
  retry_after_seconds?: number | null
}

export interface KworkSupplyScan {
  source: string
  generated_at: string
  transport?: {
    client_pool_size?: number
    source?: string
    uses_configured_proxy_pool?: boolean
  }
  scope: {
    category_id?: number
    category_name?: string
    classifier_id?: number | null
    classifier_name?: string
    reported_category_total?: number | null
    reported_selected_slices_total?: number | null
  }
  coverage: {
    profile?: { name?: string; label?: string }
    minimum_cards_target?: number
    minimum_cards_target_met?: boolean
    estimated_requests_for_minimum_cards?: number
    request_budget_can_reach_minimum_target?: boolean
    observed_listing_count?: number
    requests_planned?: number
    requests_started?: number
    requests_completed?: number
    request_errors?: number
    slice_count_available?: number
    slice_count_scanned?: number
    confidence?: string
    scanned_slices_share_of_category_percent?: number | null
    stopped_after_protection_signal?: boolean
    aborted_after_upstream_errors?: boolean
  }
  sample: {
    observed_listings?: number
    unique_sellers?: number
    reported_selected_scope_total?: number | null
    observed_share_of_scanned_scope_percent?: number | null
    price_sample?: { count?: number; min?: number | null; median?: number | null; max?: number | null }
    seller_repetition_in_observed_cards?: {
      repeated_cards?: number
      repeat_share_percent?: number | null
      sellers?: Array<{ seller?: string; cards?: number }>
    }
  }
  slices: Array<Record<string, unknown>>
  raw_listings: Array<Record<string, unknown>>
  requests?: KworkSupplyRequestDiagnostic[]
  analysis?: {
    status?: string
    provider_output?: Record<string, unknown>
    evidence_window?: {
      raw_listing_count?: number
      included_listing_count?: number
      listing_limit?: number
      evidence_char_limit?: number
      included_chars?: number
      truncated?: boolean
    }
    detail?: string
  }
  assistant_context_id?: string
  file_path?: string
  timings_ms?: Record<string, number>
}

export interface KworkMarketAssistantRequest {
  context_id: string
  message: string
}

export interface KworkMarketAssistantResponse {
  context_id: string
  answer: string
  refresh?: null | Record<string, unknown>
  refresh_rejected?: string
  error?: string
}

export interface KworkWebCatalogSummary {
  alias: string
  endpoint: string
  url: string
  request_params: Record<string, unknown>
  status_code: number
  content_type?: string
  bytes: number
  protection_status: 'ok' | 'blocked' | 'http_error' | 'parse_error' | 'empty_or_invalid_alias' | string
  success?: boolean
  state_keys?: string[]
  view_keys?: string[]
  filters?: Record<string, unknown>
  kworks?: Record<string, unknown>
  body_prefix?: string
  raw?: Record<string, unknown>
}

export interface KworkWebCatalogSnapshotRequest {
  aliases?: string[]
  page?: number
  page_size?: number
  delay_seconds?: number
  include_raw?: boolean
  write_file?: boolean
}

export interface KworkWebCatalogSnapshot {
  generated_at: string
  source: string
  config: Record<string, unknown>
  results: KworkWebCatalogSummary[]
  errors: Array<Record<string, unknown>>
  aggregate: {
    alias_count: number
    ok_count: number
    blocked_count: number
    empty_or_invalid_alias_count: number
    status_counts: Array<[string, number]>
  }
  timings_ms?: Record<string, number>
  file_path?: string
}

export interface KworkMarketIntelligenceRequest {
  seeds?: Array<Record<string, unknown>>
  max_seeds?: number
  pages?: number
  include_demand?: boolean
  demand_queries?: string[]
  include_competitor_details?: boolean
  competitor_detail_limit?: number
  include_seller_details?: boolean
  seller_detail_limit?: number
  include_want_details?: boolean
  want_detail_limit?: number
  include_price_rules?: boolean
  include_account_context?: boolean
  write_file?: boolean
}

export interface KworkMarketIntelligenceSnapshot {
  generated_at: string
  source: string
  config: Record<string, unknown>
  seeds: Array<Record<string, unknown>>
  supply: Array<Record<string, unknown>>
  query_demand: Record<string, unknown>
  seller_intelligence: Array<Record<string, unknown>>
  account_context?: Record<string, unknown>
  market_rankings: Array<Record<string, unknown>>
  aggregate: {
    seed_count: number
    cards_seen: number
    unique_sellers_seen: number
    seller_profiles_collected?: number
    top_opportunities?: Array<Record<string, unknown>>
    top_sellers: Array<[string, number]>
    category_counts: Array<[string, number]>
  }
  timings_ms?: Record<string, number>
  file_path?: string
  latest_path?: string
  index_path?: string
}

export interface KworkBuyerScoutRequest {
  probes?: Array<Record<string, unknown>>
  category_id?: number
  classifier_id?: number
  category_name?: string
  classifier_name?: string
  attribute_selection?: Record<string, unknown>
  attribute_controls?: Array<Record<string, unknown>>
  max_probes?: number
  page?: number
  project_page_limit?: number
  per_probe_limit?: number
  top_limit?: number
  include_project_details?: boolean
  include_want_details?: boolean
  include_buyer_history?: boolean
  detail_limit?: number
  buyer_history_limit?: number
  budget_max?: number
  include_query_suggestions?: boolean
  query_suggestion_limit?: number
  include_control_windows?: boolean
  control_window_limit?: number
  write_file?: boolean
}

export interface KworkBuyerScout {
  generated_at: string
  source: string
  status: string
  config: Record<string, unknown>
  probes: Array<Record<string, unknown>>
  top: Array<Record<string, unknown>>
  aggregate: {
    probe_count: number
    unique_projects: number
    zero_offer_count: number
    low_offer_count: number
    top_score?: number | null
    market_signals?: Array<Record<string, unknown>>
    query_suggestions?: Array<Record<string, unknown>>
    buyer_summary?: Record<string, unknown>
    search_recommendations?: Array<Record<string, unknown>>
    control_windows?: Array<Record<string, unknown>>
    rubric_context?: Record<string, unknown>
  }
  endpoint_errors?: Array<Record<string, unknown>>
  timings_ms?: Record<string, number>
  file_path?: string
}

export interface KworkMarketIntelligenceHistory {
  root: string
  latest_path: string
  index_path: string
  latest?: Record<string, unknown> | null
  entries: Array<Record<string, unknown>>
  entry_count: number
  exists: boolean
}

export interface KworkPriceRules {
  success?: boolean
  status?: 'ok' | 'stale' | 'unavailable' | string
  detail?: string
  cache_status?: 'hit' | 'miss' | 'stale' | string
  category_id?: number
  attribute_id?: number | null
  prices?: {
    priceGradation?: number[] | Record<string, number[] | Record<string, number>>
    typicalPriceGradation?: number[]
    minPrice?: number
    maxPrice?: number
    [key: string]: unknown
  } | null
  [key: string]: unknown
}

export interface KworkFormOption {
  id: number
  value: number
  label: string
  selected?: boolean
  disabled?: boolean
  has_child?: boolean
  data?: Record<string, unknown>
}

export interface KworkFormControl {
  group_id: number
  name: string
  custom_name?: string
  label?: string
  question?: string
  type: 'radio' | 'checkbox' | 'select' | string
  multiple: boolean
  required?: boolean
  disabled?: boolean
  options: KworkFormOption[]
  value?: string
  placeholder?: string
  data?: Record<string, unknown>
  parent_option_ids?: number[]
}

export interface KworkFormManifest {
  category_id: number
  classifier_id?: number | null
  lang: string
  success?: boolean
  code?: string
  detail?: string
  final_url?: string
  http_status?: number | null
  selected: Record<string, unknown>
  controls: KworkFormControl[]
  fragments?: Array<Record<string, unknown>>
  unresolved_required?: string[]
  manifest_hash?: string
  selection_hash?: string
  selection_validation?: {
    valid?: boolean
    clean?: boolean
    issues?: Record<string, unknown>
    active_controls?: string[]
  }
}

export interface KworkFormManifestRequest {
  classifier_id?: number
  selection?: Record<string, unknown>
  lang?: string
}

export interface KworkAttributeSuggestRequest {
  classifier_id?: number
  category_name?: string
  classifier_name?: string
  service_summary?: string
  audience?: string
  control: KworkFormControl
  controls?: KworkFormControl[]
  selection?: Record<string, unknown>
  market_context?: Record<string, unknown>
  mode?: string
  use_llm?: boolean
  lang?: string
}

export interface KworkAttributeSuggestResult {
  ok: boolean
  source: 'llm' | 'fallback' | string
  control: string
  selected_ids: number[]
  selection: Record<string, unknown>
  reason?: string
  confidence?: number
  selection_validation?: Record<string, unknown>
}

export interface KworkDraftRequest {
  category_id: number
  category_name?: string
  classifier_id?: number
  classifier_name?: string
  service_summary?: string
  brief?: string
  audience?: string
  market_context?: Record<string, unknown>
  portfolio_context?: string
  attributes?: Record<string, unknown>
  attribute_manifest?: KworkFormManifest | Record<string, unknown>
  attribute_selection?: Record<string, unknown>
  price?: number
  work_time?: number
  lang?: string
  use_llm?: boolean
  generate_image?: boolean
  image_context?: string
  use_cover_prompt_llm?: boolean
  cover_prompt_provider?: string
  cover_prompt_model?: string
  cover_prompt_temperature?: number
  use_competitor_image_analysis?: boolean
  cover_vision_provider?: string
  cover_vision_model?: string
  cover_vision_temperature?: number
  cover_vision_max_tokens?: number
  provider?: string
  model?: string
  temperature?: number
}

export interface KworkCoverImageResult {
  status?: string
  path?: string
  asset_url?: string
  filename?: string
  detail?: string
  prompt?: string
  prompt_source?: string
  requested_image_model?: string
  served_image_model?: string
  image_model_verification?: 'response_reported' | 'request_only' | string
  image_size?: string
  image_quality?: string
  image_request_id?: string
  image_latency_ms?: number
  visual_pipeline_version?: string
  image_generation?: Record<string, unknown>
  visual_strategy?: Record<string, unknown>
  quality_gate?: Record<string, unknown>
  visual_analysis_status?: 'analyzed' | 'empty' | 'failed' | 'disabled' | 'no_images' | 'download_failed' | string
  competitor_images_seen?: number
  visual_style_brief?: string
  visual_analysis_detail?: string
  cover_prompt_context?: Record<string, unknown>
  sidecar_path?: string
}

export interface KworkDraftResult {
  ok: boolean
  draft: Record<string, any>
  image?: KworkCoverImageResult | null
}

export interface KworkPublishResult {
  ok: boolean
  dry_run: boolean
  payload: Record<string, any>
  code?: string
  detail?: string
  preflight?: Record<string, any>
  confirmation?: Record<string, any>
  web_state?: Record<string, any>
  save_result?: Record<string, any>
  verify_result?: Record<string, any>
  upload?: Record<string, any>
}

export interface KworkPublishPreflightResult {
  ok: boolean
  dry_run: boolean
  payload: Record<string, any>
  preflight: {
    ok: boolean
    missing?: string[]
    code?: string
    detail?: string
  }
  token?: string
  draft_hash?: string
  expires_at?: number
  ttl_seconds?: number
  confirmation_phrase?: string
}

export interface VpnteStatus {
  ok: boolean
  enabled: boolean
  running: boolean
  instances?: Array<Record<string, unknown>>
  proxy_url?: string
  cached_proxy?: string
  fallback_proxy?: string
  detail?: string
  rotate_on_next?: boolean
  strict?: boolean
  country?: string
  profile_id?: string
  slot?: string
  port?: string
  control_url?: string
  control_url_source?: string
  token_configured?: boolean
  token_source?: string
  endpoint_file_exists?: boolean
  token_file_exists?: boolean
  appdata_dir?: string
  raw?: Record<string, unknown>
}

export interface SessionHubNetworkStatus {
  ok: boolean
  url: string
  health_url: string
  status_code?: number | null
  detail?: string
  cache_entries?: number | null
  proxy_isolated: boolean
}

export interface NetworkStatus {
  vpnte: VpnteStatus
  session_hub: SessionHubNetworkStatus
}

export interface VpnteActionPayload {
  slot?: number
  country?: string
  profile_id?: string
  port?: number
  id?: string
}

export interface VpnteActionResult {
  ok: boolean
  detail?: string
  status?: Record<string, unknown>
  network: NetworkStatus
}

export interface OverviewMetrics {
  parsed: number
  queued: number
  manual_sent: number
  auto_sent: number
  draft: number
  responses: number
}

export interface TimelineItem {
  day: string
  action: string
  total: number
}

export interface ScoringRow {
  ai_score_source: string
  total: number
  avg_ai_score: number
  avg_vet_score: number
  shipped: number
  skipped: number
}

export interface StatusRow {
  status: string
  total: number
}

export interface QueryMemoryRow {
  platform: string
  query_text: string
  runs: number
  shortlisted_count: number
  auto_ready_count: number
  sent_count: number
  responded_count: number
  skipped_count: number
  user_preferred: number
}

export interface ParseStatRow {
  platform: string
  runs: number
  ok: number
  err: number
  projects: number
  avg_ms: number
}

export interface SendStatRow {
  platform: string
  status: string
  total: number
  avg_ms: number
}

export interface BreakerRow {
  key: string
  state: string
  consecutive_failures: number
  paused_seconds_left: number
  last_error?: string
  updated_at?: string
}

export interface GenerationRow {
  provider: string
  calls: number
  ok: number
  err: number
  avg_ms: number
}

export interface ErrorRow {
  timestamp: string
  module?: string
  error_type?: string
  message: string
}

export interface FiltersData {
  required_skills?: string[]
  stop_words?: string[]
  min_budget?: number
  max_budget?: number
  per_page?: number
  max_age_hours?: number
  max_proposals?: number
  min_client_score?: number
  [key: string]: unknown
}

export interface OSINTResult {
  username: string
  findings: OSINTFinding[]
  probiv_findings: OSINTFinding[]
  contacts: Record<string, string[]>
  summary: string
  reputation_score: number
  red_flags: string[]
  positive_signals: string[]
}

export interface OSINTFinding {
  source: string
  kind: string
  title: string
  url?: string
  snippet?: string
  confidence?: number
  meta?: Record<string, unknown>
}

// ── Earnings ────────────────────────────────────────────────────────────────

export interface EarningsSummary {
  total: number
  paid_count: number
  pending_count: number
  paid_amount: number
  pending_amount: number
  total_amount: number
}

export interface EarningRow {
  earning_id: number
  candidate_id?: number
  project_id: string
  platform: string
  amount: number
  currency: string
  status: string
  paid_at?: string
  created_at: string
}

// ── Funnel ──────────────────────────────────────────────────────────────────

export interface FunnelData {
  sent: number
  responded: number
  hired: number
  completed: number
  paid: number
  response_rate: number
  hire_rate: number
  completion_rate: number
  payment_rate: number
}

// ── Health ──────────────────────────────────────────────────────────────────

export interface HealthData {
  connects_free: number
  connects_total: number
  success_rate: number
  completed: number
  cancelled: number
  active_orders: number
  busy_risk: boolean
  captcha_required: boolean
  captcha_api_flag?: boolean
  captcha_status?: string
  captcha_check_error?: string
  manual_verification_required?: boolean
  unread_notifications: number
  username: string
  level: string
  rating: number
  reviews_count: number
  paused_kworks: number[]
}

export interface KworkVerificationStatus {
  generated_at: string
  source: string
  status: 'ok' | 'manual_required' | 'api_flag_only' | 'error' | string
  detail: string
  captcha_required: boolean
  manual_verification_required: boolean
  web_session_ok: boolean
  smartcaptcha_scripts_seen: boolean
  cookie_count: number
  captcha_status?: {
    ok?: boolean
    required?: boolean
    source?: string
    error?: string
    response_type?: string
  }
  pages: Array<{
    path: string
    status_code: number
    final_url: string
    evidence?: {
      manual_required?: boolean
      script_matches?: string[]
      strong_matches?: string[]
      weak_matches?: string[]
      challenge_url?: boolean
      challenge_status?: boolean
      has_new_form?: boolean
    }
    error?: string
    timings_ms?: Record<string, number>
  }>
  errors: Array<{ stage?: string; path?: string; detail?: string }>
  timings_ms?: Record<string, number>
  file_path?: string
}

// ── Skipped ─────────────────────────────────────────────────────────────────

export interface SkippedCandidate {
  candidate_id: number
  project_id: string
  platform: string
  title: string
  budget: string
  status: string
  decision_reason: string
  ai_score?: number | null
  ai_score_source?: string
  updated_at: string
  search_query?: string
}

// ── API methods (appended to existing api object) ───────────────────────────

// Earnings
;(api as any).getEarnings = () => request<EarningsSummary>('/api/dashboard/earnings')
;(api as any).getEarningsList = (limit = 50) => request<EarningRow[]>(`/api/dashboard/earnings/list?limit=${limit}`)
// Funnel
;(api as any).getFunnel = (days = 30) => request<FunnelData>(`/api/dashboard/funnel?days=${days}`)
// Health
;(api as any).getHealth = () => request<HealthData>('/api/dashboard/health')
;(api as any).getSkipped = (limit = 50) => request<{ items: SkippedCandidate[]; total: number }>(`/api/dashboard/skipped?limit=${limit}`)
// Lifecycle
;(api as any).hireCandidate = (id: number) => request<{ ok: boolean }>(`/api/candidates/${id}/hire`, { method: 'POST' })
;(api as any).declineCandidate = (id: number) => request<{ ok: boolean }>(`/api/candidates/${id}/decline`, { method: 'POST' })
;(api as any).completeCandidate = (id: number) => request<{ ok: boolean }>(`/api/candidates/${id}/complete`, { method: 'POST' })
;(api as any).recordEarning = (id: number, amount: number, currency = 'RUB') =>
  request<{ ok: boolean; earning_id: number }>(`/api/candidates/${id}/earn`, {
    method: 'POST',
    body: JSON.stringify({ amount, currency }),
  })

// ── AI Chat ──────────────────────────────────────────────────────────────────

export interface ProviderInfo {
  name: string
  configured: boolean
  base_url: string
  wire_api: string
  model: string
  key_count: number
}

export interface ChatMessage {
  role: string
  content: string
}

export interface ChatSendResult {
  ok: boolean
  reply: string
  provider: string
  model: string
  error?: string
}

export interface ChatTestResult {
  ok: boolean
  provider: string
  model: string
  reply?: string
  latency_ms: number
  error?: string
}

;(api as any).getChatProviders = () => request<ProviderInfo[]>('/api/chat/providers')
;(api as any).testChatProvider = (provider: string, model = '') =>
  request<ChatTestResult>('/api/chat/test', {
    method: 'POST',
    body: JSON.stringify({ provider, model, messages: [] }),
  })
;(api as any).sendChatMessage = (
  provider: string,
  messages: ChatMessage[],
  model = '',
  systemPrompt = '',
  temperature = 0.7,
  maxTokens = 2048,
) =>
  request<ChatSendResult>('/api/chat/send', {
    method: 'POST',
    body: JSON.stringify({ provider, model, messages, system_prompt: systemPrompt, temperature, max_tokens: maxTokens }),
  })

// ── Conversations ────────────────────────────────────────────────────────────

export interface ConversationRow {
  conversation_id: number
  candidate_id?: number
  project_id: string
  platform: string
  project_title?: string
  status: string
  last_message_at?: string
  message_count?: number
  created_at: string
  updated_at: string
}

export interface ConversationMessage {
  message_id: number
  sender: string
  message_text?: string
  created_at: string
}

;(api as any).getConversations = (limit = 50) =>
  request<ConversationRow[]>(`/api/dashboard/conversations?limit=${limit}`)
;(api as any).getConversationHistory = (projectId: string, platform: string) =>
  request<{ messages: ConversationMessage[]; username: string; read_state?: Record<string, unknown> | null }>(
    `/api/dashboard/conversations/${encodeURIComponent(projectId)}/${encodeURIComponent(platform)}`,
  )
;(api as any).draftConversationReply = (projectId: string, platform: string, tone = 'friendly') =>
  request<{ ok: boolean; text: string }>(
    `/api/dashboard/conversations/${encodeURIComponent(projectId)}/${encodeURIComponent(platform)}/draft`,
    {
      method: 'POST',
      body: JSON.stringify({ tone }),
    },
  )
;(api as any).sendConversationMessage = (projectId: string, platform: string, text: string) =>
  request<{ ok: boolean; message_id?: number }>(
    `/api/dashboard/conversations/${encodeURIComponent(projectId)}/${encodeURIComponent(platform)}/message`,
    {
      method: 'POST',
      body: JSON.stringify({ text }),
    },
  )
;(api as any).getCandidateDialog = (id: number) =>
  request<{ messages: any[]; username: string }>(`/api/candidates/${id}/dialog`)
;(api as any).sendMessageToClient = (id: number, userId: string, text: string) =>
  request<{ ok: boolean }>(`/api/candidates/${id}/message`, {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, text }),
  })

// ── Blacklist ────────────────────────────────────────────────────────────────

;(api as any).getBlacklist = () =>
  request<{ items: any[] }>(`/api/candidates/blacklist/list`)
;(api as any).addBlacklist = (clientUserId: string, platform = 'kwork', reason = '') =>
  request<{ ok: boolean }>(`/api/candidates/blacklist/add`, {
    method: 'POST',
    body: JSON.stringify({ client_user_id: clientUserId, platform, reason }),
  })
;(api as any).removeBlacklist = (clientUserId: string, platform: string) =>
  request<{ ok: boolean }>(
    `/api/candidates/blacklist/${encodeURIComponent(clientUserId)}/${encodeURIComponent(platform)}`,
    { method: 'DELETE' },
  )

// ── Kwork Orders ─────────────────────────────────────────────────────────────

export interface KworkOrder {
  id?: number
  name?: string
  status?: string
  price?: number
  date_create?: string
  date_to?: string
  user_username?: string
}

;(api as any).getKworkOrders = (status = 'all') =>
  request<{ orders: KworkOrder[] }>(`/api/dashboard/kwork-orders?status=${status}`)

// ── Connects Check ───────────────────────────────────────────────────────────

export interface ConnectsCheck {
  connects: any
  free_amount: number
  can_send: boolean
  warn_threshold: number
  block_threshold: number
}

;(api as any).getConnectsCheck = () => request<ConnectsCheck>('/api/dashboard/kwork-connects-check')

// ── Alerts ───────────────────────────────────────────────────────────────────

export interface AlertItem {
  type: string
  severity: string
  project_id: string
  platform: string
  title: string
  detail: string
}

;(api as any).getAlerts = () => request<{ alerts: AlertItem[]; count: number }>('/api/dashboard/alerts')
