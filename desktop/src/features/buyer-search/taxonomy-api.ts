import { API_BASE } from '../../lib/api'
import type {
  BuyerTaxonomyCategoryContext,
  BuyerTaxonomyCategoryPage,
  BuyerTaxonomySnapshot,
} from './taxonomy-types'

type QueryValue = string | number | boolean | null | undefined

function queryString(values: Record<string, QueryValue>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  const serialized = params.toString()
  return serialized ? `?${serialized}` : ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    let detail = body
    try {
      const parsed = body ? (JSON.parse(body) as { detail?: unknown }) : null
      if (typeof parsed?.detail === 'string' && parsed.detail) detail = parsed.detail
    } catch {
      // Preserve a non-JSON response body for the operator.
    }
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

export interface BuyerTaxonomyCategoryListOptions {
  snapshotId: string
  parentCategoryId?: number | null
  query?: string
  afterCategoryId?: number | null
  limit?: number
  signal?: AbortSignal
}

export interface BuyerTaxonomyRefreshOptions {
  runId: string
  rubricIds?: number[]
  categoryIds?: number[]
  rubricLimit?: number
  categoryLimit?: number
  includeAttributes?: boolean
  includeFilters?: boolean
  catalogSeedLimit?: number
  signal?: AbortSignal
}

export interface BuyerTaxonomySnapshotPayload {
  source: string
  sourceRevision?: string
  capturedAt?: string
  provenance?: Record<string, unknown>
  categories: Array<Record<string, unknown>>
  attributes?: Array<Record<string, unknown>>
  filters?: Array<Record<string, unknown>>
  catalogSeeds?: Array<Record<string, unknown> | string>
  suggestions?: Array<Record<string, unknown> | string>
  terms?: Array<Record<string, unknown> | string>
}

export const buyerTaxonomyApi = {
  listSnapshots: (options: { limit?: number; signal?: AbortSignal } = {}) =>
    request<{ items: BuyerTaxonomySnapshot[] }>(
      `/api/kwork/buyer-search/taxonomy/snapshots${queryString({ limit: options.limit ?? 20 })}`,
      { signal: options.signal },
    ),
  listCategories: (options: BuyerTaxonomyCategoryListOptions) =>
    request<BuyerTaxonomyCategoryPage>(
      `/api/kwork/buyer-search/taxonomy/categories${queryString({
        snapshot_id: options.snapshotId,
        parent_category_id: options.query ? undefined : options.parentCategoryId,
        query: options.query,
        after_category_id: options.afterCategoryId,
        limit: options.limit ?? 100,
      })}`,
      { signal: options.signal },
    ),
  getCategoryContext: (categoryId: number, options: { snapshotId: string; signal?: AbortSignal }) =>
    request<BuyerTaxonomyCategoryContext>(
      `/api/kwork/buyer-search/taxonomy/categories/${encodeURIComponent(categoryId)}/generation-context${queryString({
        snapshot_id: options.snapshotId,
      })}`,
      { signal: options.signal },
    ),
  recordSnapshot: (payload: BuyerTaxonomySnapshotPayload) =>
    request<BuyerTaxonomySnapshot>(
      '/api/kwork/buyer-search/taxonomy/snapshots',
      {
        method: 'POST',
        body: JSON.stringify({
          source: payload.source,
          source_revision: payload.sourceRevision,
          captured_at: payload.capturedAt,
          provenance: payload.provenance ?? {},
          categories: payload.categories,
          attributes: payload.attributes ?? [],
          filters: payload.filters ?? [],
          catalog_seeds: payload.catalogSeeds ?? [],
          suggestions: payload.suggestions ?? [],
          terms: payload.terms ?? [],
        }),
      },
    ),
  refreshSnapshot: (accountRegistrationId: string, options: BuyerTaxonomyRefreshOptions) =>
    request<BuyerTaxonomySnapshot>(
      `/api/kwork/buyer-search/accounts/${encodeURIComponent(accountRegistrationId)}/taxonomy/refresh`,
      {
        method: 'POST',
        body: JSON.stringify({
          run_id: options.runId,
          rubric_ids: options.rubricIds ?? [],
          category_ids: options.categoryIds ?? [],
          rubric_limit: options.rubricLimit ?? 20,
          category_limit: options.categoryLimit ?? 80,
          include_attributes: options.includeAttributes ?? true,
          include_filters: options.includeFilters ?? true,
          catalog_seed_limit: options.catalogSeedLimit ?? 80,
        }),
        signal: options.signal,
      },
    ),
}
