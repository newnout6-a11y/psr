import { API_BASE } from '../../lib/api'
import type {
  CreateMarketJobPayload,
  CreateMarketJobResponse,
  MarketEventPage,
  MarketEventsQuery,
  MarketAssistantResponse,
  MarketAccountEnabledPayload,
  MarketAccountPoolAccount,
  MarketAccountPoolSnapshot,
  MarketJob,
  MarketJobPage,
  MarketJobsQuery,
  MarketJobSnapshot,
  MarketListingPage,
  MarketListingsQuery,
  MarketOperationPage,
  MarketOperationAttemptPage,
  MarketOperation,
  MarketOperationsQuery,
  MarketResults,
  MarketRecommendation,
  MarketRecommendationCreatePayload,
  MarketRecommendationTransitionResponse,
  MarketDraftHandoff,
  MarketHandoffDraftResponse,
  MarketHandoffPublishResponse,
  MarketPublishedListing,
  MarketShardPage,
  MarketPaginationParams,
  MarketTransportPage,
  MarketWorkerCommand,
  MarketWorkerPage,
  StopMarketJobPayload,
  UpdateMarketJobPayload,
} from './types'

const JOBS_PATH = '/api/kwork/market/jobs'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function withQuery(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) query.set(key, String(value))
  }
  const suffix = query.toString()
  return suffix ? `${path}?${suffix}` : path
}

async function marketRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (!response.ok) {
    const body = await response.json().catch((): unknown => null)
    const detail = isRecord(body) && typeof body.detail === 'string' ? body.detail : response.statusText
    throw new Error(`HTTP ${response.status}: ${detail}`)
  }
  return response.json() as Promise<T>
}

function jobPath(jobId: string): string {
  return `${JOBS_PATH}/${encodeURIComponent(jobId)}`
}

export interface MarketJobsApiClient {
  createJob(payload: CreateMarketJobPayload): Promise<CreateMarketJobResponse>
  listJobs(params?: MarketJobsQuery): Promise<MarketJobPage>
  getJob(jobId: string): Promise<MarketJobSnapshot>
  updateJob(jobId: string, payload: UpdateMarketJobPayload): Promise<MarketJobSnapshot>
  pauseJob(jobId: string): Promise<MarketJob>
  resumeJob(jobId: string): Promise<MarketJob>
  stopJob(jobId: string, payload?: StopMarketJobPayload): Promise<MarketJob>
  setWorkerPool(jobId: string, desiredWorkers: number, expectedRevision?: number): Promise<MarketJobSnapshot>
  getWorkers(jobId: string, params?: MarketPaginationParams): Promise<MarketWorkerPage>
  getTransports(jobId: string, params?: MarketPaginationParams): Promise<MarketTransportPage>
  getShards(jobId: string, params?: MarketPaginationParams): Promise<MarketShardPage>
  getOperations(jobId: string, params?: MarketOperationsQuery): Promise<MarketOperationPage>
  getOperationAttempts(jobId: string, operationId: string): Promise<MarketOperationAttemptPage>
  getListings(jobId: string, params?: MarketListingsQuery): Promise<MarketListingPage>
  getEvents(jobId: string, params?: MarketEventsQuery): Promise<MarketEventPage>
  getResults(jobId: string): Promise<MarketResults>
  listRecommendations(jobId: string): Promise<{ job_id: string; items: MarketRecommendation[] }>
  createRecommendation(jobId: string, payload: MarketRecommendationCreatePayload): Promise<{ recommendation: MarketRecommendation }>
  confirmRecommendation(jobId: string, recommendationId: string, expectedRevision?: number, loadManifest?: boolean): Promise<MarketRecommendationTransitionResponse>
  rejectRecommendation(jobId: string, recommendationId: string, expectedRevision?: number): Promise<MarketRecommendationTransitionResponse>
  getDraftHandoff(jobId: string, handoffId: string): Promise<MarketDraftHandoff>
  refreshDraftHandoffManifest(jobId: string, handoffId: string): Promise<{ handoff: MarketDraftHandoff }>
  updateDraftHandoffSelection(jobId: string, handoffId: string, manifestHash: string, selection: Record<string, unknown>, confirm: boolean): Promise<{ handoff: MarketDraftHandoff }>
  generateDraftHandoff(jobId: string, handoffId: string, generationOptions?: Record<string, unknown>): Promise<MarketHandoffDraftResponse>
  publishDraftHandoff(jobId: string, handoffId: string, payload: { dry_run: boolean; confirm_token?: string; confirmation?: string; variant_index?: number }): Promise<MarketHandoffPublishResponse>
  listPublishedListings(jobId: string): Promise<{ job_id: string; items: MarketPublishedListing[] }>
  getAccountPool(): Promise<MarketAccountPoolSnapshot>
  syncAccountPool(): Promise<MarketAccountPoolSnapshot>
  setAccountPoolAccount(registrationId: string, payload: MarketAccountEnabledPayload): Promise<{ account: MarketAccountPoolAccount }>
  getFleet(jobId: string): Promise<MarketAccountPoolSnapshot>
  reconcileFleet(jobId: string): Promise<MarketAccountPoolSnapshot>
  rebindWorker(jobId: string, workerId: string): Promise<unknown>
  askAssistant(jobId: string, message: string): Promise<MarketAssistantResponse>
  commandWorker(jobId: string, workerId: string, command: 'drain' | 'restart' | 'disable' | 'rotate' | 'reconnect'): Promise<MarketWorkerCommand>
  retryOperation(jobId: string, operationId: string): Promise<MarketOperation>
}

export const marketJobsApi: MarketJobsApiClient = {
  createJob: (payload) => marketRequest<CreateMarketJobResponse>(JOBS_PATH, { method: 'POST', body: JSON.stringify(payload) }),
  listJobs: (params = {}) =>
    marketRequest<MarketJobPage>(withQuery(JOBS_PATH, { offset: params.offset, limit: params.limit, state: params.state })),
  getJob: (jobId) => marketRequest<MarketJobSnapshot>(jobPath(jobId)),
  updateJob: (jobId, payload) => marketRequest<MarketJobSnapshot>(jobPath(jobId), { method: 'PATCH', body: JSON.stringify(payload) }),
  pauseJob: (jobId) => marketRequest<MarketJob>(`${jobPath(jobId)}/pause`, { method: 'POST' }),
  resumeJob: (jobId) => marketRequest<MarketJob>(`${jobPath(jobId)}/resume`, { method: 'POST' }),
  stopJob: (jobId, payload = {}) => marketRequest<MarketJob>(`${jobPath(jobId)}/stop`, { method: 'POST', body: JSON.stringify(payload) }),
  setWorkerPool: (jobId, desiredWorkers, expectedRevision) =>
    marketRequest<MarketJobSnapshot>(`${jobPath(jobId)}/worker-pool`, {
      method: 'PATCH',
      body: JSON.stringify({ desired_workers: desiredWorkers, expected_revision: expectedRevision }),
    }),
  getWorkers: (jobId, params = {}) =>
    marketRequest<MarketWorkerPage>(withQuery(`${jobPath(jobId)}/workers`, { cursor: params.cursor, limit: params.limit })),
  getTransports: (jobId, params = {}) =>
    marketRequest<MarketTransportPage>(withQuery(`${jobPath(jobId)}/transports`, { cursor: params.cursor, limit: params.limit })),
  getShards: (jobId, params = {}) =>
    marketRequest<MarketShardPage>(withQuery(`${jobPath(jobId)}/shards`, { cursor: params.cursor, limit: params.limit })),
  getOperations: (jobId, params = {}) =>
    marketRequest<MarketOperationPage>(withQuery(`${jobPath(jobId)}/operations`, {
      cursor: params.cursor,
      limit: params.limit,
      state: params.state,
    })),
  getOperationAttempts: (jobId, operationId) =>
    marketRequest<MarketOperationAttemptPage>(`${jobPath(jobId)}/operations/${encodeURIComponent(operationId)}/attempts`),
  getListings: (jobId, params = {}) =>
    marketRequest<MarketListingPage>(withQuery(`${jobPath(jobId)}/listings`, {
      cursor: params.cursor,
      limit: params.limit,
      shard: params.shard,
    })),
  getEvents: (jobId, params = {}) =>
    marketRequest<MarketEventPage>(withQuery(`${jobPath(jobId)}/events`, {
      after_seq: params.after_seq,
      limit: params.limit,
      tail: params.tail,
    })),
  getResults: (jobId) => marketRequest<MarketResults>(`${jobPath(jobId)}/results`),
  listRecommendations: (jobId) => marketRequest<{ job_id: string; items: MarketRecommendation[] }>(`${jobPath(jobId)}/recommendations`),
  createRecommendation: (jobId, payload) =>
    marketRequest<{ recommendation: MarketRecommendation }>(`${jobPath(jobId)}/recommendations`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  confirmRecommendation: (jobId, recommendationId, expectedRevision, loadManifest = false) =>
    marketRequest<MarketRecommendationTransitionResponse>(`${jobPath(jobId)}/recommendations/${encodeURIComponent(recommendationId)}/confirm`, {
      method: 'POST',
      body: JSON.stringify({
        ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
        load_manifest: loadManifest,
      }),
    }),
  rejectRecommendation: (jobId, recommendationId, expectedRevision) =>
    marketRequest<MarketRecommendationTransitionResponse>(`${jobPath(jobId)}/recommendations/${encodeURIComponent(recommendationId)}/reject`, {
      method: 'POST',
      body: JSON.stringify(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
    }),
  getDraftHandoff: (jobId, handoffId) =>
    marketRequest<MarketDraftHandoff>(`${jobPath(jobId)}/draft-handoffs/${encodeURIComponent(handoffId)}`),
  refreshDraftHandoffManifest: (jobId, handoffId) =>
    marketRequest<{ handoff: MarketDraftHandoff }>(`${jobPath(jobId)}/draft-handoffs/${encodeURIComponent(handoffId)}/refresh-manifest`, {
      method: 'POST',
      body: JSON.stringify({ lang: 'ru' }),
    }),
  updateDraftHandoffSelection: (jobId, handoffId, manifestHash, selection, confirm) =>
    marketRequest<{ handoff: MarketDraftHandoff }>(`${jobPath(jobId)}/draft-handoffs/${encodeURIComponent(handoffId)}/selection`, {
      method: 'PUT',
      body: JSON.stringify({ manifest_hash: manifestHash, selection, confirm }),
    }),
  generateDraftHandoff: (jobId, handoffId, generationOptions = {}) =>
    marketRequest<MarketHandoffDraftResponse>(`${jobPath(jobId)}/draft-handoffs/${encodeURIComponent(handoffId)}/draft`, {
      method: 'POST',
      body: JSON.stringify({ generation_options: generationOptions }),
    }),
  publishDraftHandoff: (jobId, handoffId, payload) =>
    marketRequest<MarketHandoffPublishResponse>(`${jobPath(jobId)}/draft-handoffs/${encodeURIComponent(handoffId)}/publish`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listPublishedListings: (jobId) =>
    marketRequest<{ job_id: string; items: MarketPublishedListing[] }>(`${jobPath(jobId)}/published-listings`),
  getAccountPool: () => marketRequest<MarketAccountPoolSnapshot>('/api/kwork/market/account-pool'),
  syncAccountPool: () => marketRequest<MarketAccountPoolSnapshot>('/api/kwork/market/account-pool/sync', { method: 'POST' }),
  setAccountPoolAccount: (registrationId, payload) =>
    marketRequest<{ account: MarketAccountPoolAccount }>(`/api/kwork/market/account-pool/accounts/${encodeURIComponent(registrationId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  getFleet: (jobId) => marketRequest<MarketAccountPoolSnapshot>(`${jobPath(jobId)}/fleet`),
  reconcileFleet: (jobId) => marketRequest<MarketAccountPoolSnapshot>(`${jobPath(jobId)}/fleet/reconcile`, { method: 'POST' }),
  rebindWorker: (jobId, workerId) =>
    marketRequest<unknown>(`${jobPath(jobId)}/workers/${encodeURIComponent(workerId)}/rebind`, { method: 'POST' }),
  askAssistant: (jobId, message) =>
    marketRequest<MarketAssistantResponse>(`${jobPath(jobId)}/assistant`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
  commandWorker: (jobId, workerId, command) =>
    marketRequest<MarketWorkerCommand>(`${jobPath(jobId)}/workers/${encodeURIComponent(workerId)}/${command}`, { method: 'POST' }),
  retryOperation: (jobId, operationId) =>
    marketRequest<MarketOperation>(`${jobPath(jobId)}/operations/${encodeURIComponent(operationId)}/retry`, { method: 'POST' }),
}

export function marketJobWebSocketUrl(jobId: string, afterSeq: number): string {
  const base = API_BASE.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')
  return `${base}/ws/kwork-market/jobs/${encodeURIComponent(jobId)}?after_seq=${Math.max(0, afterSeq)}`
}
