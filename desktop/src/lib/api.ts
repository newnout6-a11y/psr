const BASE = 'http://127.0.0.1:7788'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(body.detail ?? res.statusText)
  }
  return res.json()
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

  inspectKworkProject: (projectId: string) =>
    request<{
      ok: boolean
      url: string
      state_found: boolean
      detail?: string
      project?: KworkInspectProject
    }>(`/api/kwork/inspect/${encodeURIComponent(projectId)}`),

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
  unread_notifications: number
  username: string
  level: string
  rating: number
  reviews_count: number
  paused_kworks: number[]
}

// ── API methods (appended to existing api object) ───────────────────────────

// Earnings
;(api as any).getEarnings = () => request<EarningsSummary>('/api/dashboard/earnings')
;(api as any).getEarningsList = (limit = 50) => request<EarningRow[]>(`/api/dashboard/earnings/list?limit=${limit}`)
// Funnel
;(api as any).getFunnel = (days = 30) => request<FunnelData>(`/api/dashboard/funnel?days=${days}`)
// Health
;(api as any).getHealth = () => request<HealthData>('/api/dashboard/health')
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
