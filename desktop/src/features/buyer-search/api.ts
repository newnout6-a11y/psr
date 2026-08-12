import { API_BASE } from '../../lib/api'
import type {
  BuyerSearchBatchProjectActionPayload,
  BuyerSearchBatchProjectActionResult,
  BuyerSearchEnrichmentResult,
  BuyerSearchExport,
  BuyerSearchExportRequest,
  BuyerSearchFinalScoreRequest,
  BuyerSearchFacets,
  BuyerSearchFleet,
  BuyerSearchProjectDetail,
  BuyerSearchProjectListOptions,
  BuyerSearchPage,
  BuyerSearchProject,
  BuyerSearchQuery,
  BuyerSearchQueryGenerationRequest,
  BuyerSearchQueryGenerationResult,
  BuyerSearchQueryCollisionRegenerationRequest,
  BuyerSearchQueryCollisionRegenerationResult,
  BuyerSearchQueryCollisionReport,
  BuyerSearchQueryBundles,
  BuyerSearchQueryPatch,
  BuyerSearchQueryUpdateResult,
  BuyerSearchRun,
  BuyerSearchRunPatch,
  CreateBuyerSearchRunPayload,
  BuyerSearchWorkspaceSnapshot,
  BuyerSearchWorkspaceState,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    let detail = body
    try {
      const parsed = body ? JSON.parse(body) : null
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // Keep a non-JSON error body intact for diagnostics.
    }
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

function websocketUrl(path: string, values: Record<string, string | number | boolean | undefined | null> = {}): string {
  const origin = new URL(API_BASE)
  origin.protocol = origin.protocol === 'https:' ? 'wss:' : 'ws:'
  origin.pathname = path
  origin.search = queryString(values)
  return origin.toString()
}

type QueryValue = string | number | boolean | string[] | undefined | null

function queryString(values: Record<string, QueryValue>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value)) {
      value.filter(Boolean).forEach((item) => params.append(key, item))
    } else if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value))
    }
  }
  const serialized = params.toString()
  return serialized ? `?${serialized}` : ''
}

export const buyerSearchApi = {
  getWorkspace: (signal?: AbortSignal) =>
    request<BuyerSearchWorkspaceSnapshot>('/api/kwork/buyer-search/workspace', { signal }),
  putWorkspace: (state: BuyerSearchWorkspaceState, options: { keepalive?: boolean } = {}) =>
    request<BuyerSearchWorkspaceSnapshot>('/api/kwork/buyer-search/workspace', {
      method: 'PUT',
      body: JSON.stringify({ schema_version: 1, state }),
      keepalive: options.keepalive,
    }),
  createRun: (payload: CreateBuyerSearchRunPayload) =>
    request<BuyerSearchRun>('/api/kwork/buyer-search/runs', { method: 'POST', body: JSON.stringify(payload) }),
  listRuns: (options: { limit?: number; cursor?: string; signal?: AbortSignal } = {}) =>
    request<BuyerSearchPage<BuyerSearchRun>>(
      `/api/kwork/buyer-search/runs${queryString({ limit: options.limit ?? 50, cursor: options.cursor })}`,
      { signal: options.signal },
    ),
  getRun: (runId: string) => request<BuyerSearchRun>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}`),
  getFleet: (runId: string, signal?: AbortSignal) =>
    request<BuyerSearchFleet>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/fleet`, { signal }),
  patchRun: (runId: string, payload: BuyerSearchRunPatch) =>
    request<BuyerSearchRun>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteRun: (runId: string) =>
    request<{ run_id: string; deleted: boolean }>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}`, {
      method: 'DELETE',
    }),
  restartRun: (runId: string) =>
    request<BuyerSearchRun>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/restart`, {
      method: 'POST',
    }),
  listQueries: (runId: string, state?: string, options: { cursor?: string; signal?: AbortSignal } = {}) =>
    request<BuyerSearchPage<BuyerSearchQuery>>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries${queryString({ state, limit: 500, cursor: options.cursor })}`,
      { signal: options.signal },
    ),
  patchQuery: (runId: string, queryId: string, payload: BuyerSearchQueryPatch) =>
    request<BuyerSearchQueryUpdateResult>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries/${encodeURIComponent(queryId)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    ),
  generateQueries: (runId: string, payload: BuyerSearchQueryGenerationRequest) =>
    request<BuyerSearchQueryGenerationResult>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries/generate`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  getQueryCollisions: (runId: string, options: { afterSeq?: number; limit?: number; signal?: AbortSignal } = {}) =>
    request<BuyerSearchQueryCollisionReport>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries/collisions${queryString({
        after_seq: options.afterSeq ?? 0,
        limit: options.limit ?? 500,
      })}`,
      { signal: options.signal },
    ),
  regenerateQueryCollisions: (runId: string, payload: BuyerSearchQueryCollisionRegenerationRequest) =>
    request<BuyerSearchQueryCollisionRegenerationResult>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries/regenerate-collisions`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  getQueryBundles: (runId: string, includeDisabled = false, signal?: AbortSignal) =>
    request<BuyerSearchQueryBundles>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/query-bundles${queryString({ include_disabled: includeDisabled })}`,
      { signal },
    ),
  distributeQueries: (runId: string) =>
    request<{ queued_count: number; queued_query_ids: string[]; skipped_query_ids: string[] }>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/queries/distribute`,
      { method: 'POST', body: JSON.stringify({}) },
    ),
  listProjects: (runId: string, options: BuyerSearchProjectListOptions = {}) =>
    request<BuyerSearchPage<BuyerSearchProject>>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects${queryString({
        cursor: options.cursor,
        limit: options.limit ?? 100,
        sort: options.sort,
        shortlist: options.shortlist,
        min_score: options.minScore,
        max_budget: options.maxBudget,
        max_offers: options.maxOffers,
        category_id: options.categoryId,
        text: options.text,
        max_score: options.maxScore,
        min_budget: options.minBudget,
        min_offers: options.minOffers,
        min_views: options.minViews,
        max_views: options.maxViews,
        min_buyer_hired_percent: options.minBuyerHiredPercent,
        min_age_seconds: options.minAgeSeconds,
        max_age_seconds: options.maxAgeSeconds,
        has_attachments: options.hasAttachments,
        attachment_parse_state: options.attachmentParseState,
        attachment_type: options.attachmentType,
        tag: options.tags,
        proposal_state: options.proposalState,
        conversation_state: options.conversationState,
        unseen: options.unseen,
      })}`,
      { signal: options.signal },
    ),
  getFacets: (runId: string, shortlist?: string, signal?: AbortSignal) =>
    request<BuyerSearchFacets>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/facets${queryString({ shortlist })}`,
      { signal },
    ),
  getProject: (runId: string, projectId: string, signal?: AbortSignal) =>
    request<BuyerSearchProjectDetail>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}`,
      { signal },
    ),
  batchProjectAction: (runId: string, payload: BuyerSearchBatchProjectActionPayload) =>
    request<BuyerSearchBatchProjectActionResult>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/batch-action`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  rescoreProject: (runId: string, projectId: string) =>
    request<Record<string, unknown>>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}/rescore`,
      { method: 'POST' },
    ),
  finalScoreProject: (runId: string, projectId: string, payload: BuyerSearchFinalScoreRequest) =>
    request<Record<string, unknown>>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}/score-final`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  enrichProject: (runId: string, projectId: string, accountRegistrationId: string) =>
    request<BuyerSearchEnrichmentResult>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}/enrich`,
      { method: 'POST', body: JSON.stringify({ account_registration_id: accountRegistrationId }) },
    ),
  createExport: (runId: string, payload: BuyerSearchExportRequest) =>
    request<BuyerSearchExport>(`/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/exports`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getExport: (runId: string, exportId: string, signal?: AbortSignal) =>
    request<BuyerSearchExport>(
      `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/exports/${encodeURIComponent(exportId)}`,
      { signal },
    ),
  exportDownloadUrl: (runId: string, exportId: string) =>
    `${API_BASE}/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/exports/${encodeURIComponent(exportId)}/download`,
  attachmentPreviewUrl: (runId: string, projectId: string, attachmentId: string) =>
    `${API_BASE}/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}/attachments/${encodeURIComponent(attachmentId)}/preview`,
  runEventsUrl: (runId: string, afterSeq = 0) =>
    websocketUrl(`/ws/kwork-buyer-search/runs/${encodeURIComponent(runId)}`, { after_seq: afterSeq }),
}
