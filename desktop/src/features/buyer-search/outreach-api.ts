import { API_BASE } from '../../lib/api'
import type {
  BuyerOutreachCreateIntentPayload,
  BuyerOutreachDeliveryPayload,
  BuyerOutreachDeliveryResult,
  BuyerOutreachDraft,
  BuyerOutreachEditDraftPayload,
  BuyerOutreachGenerateDraftPayload,
  BuyerOutreachPreflight,
  BuyerOutreachPreflightPayload,
  BuyerOutreachPreflightResponse,
  BuyerOutreachPromotion,
  BuyerOutreachPromotePayload,
  BuyerOutreachSendIntent,
  BuyerOutreachSendIntentResponse,
} from './outreach-types'

export class BuyerOutreachApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'BuyerOutreachApiError'
    this.status = status
  }
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
      const parsed = body ? JSON.parse(body) : null
      if (parsed && typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // Preserve a non-JSON response body for diagnostics.
    }
    throw new BuyerOutreachApiError(response.status, detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

function projectBase(runId: string, projectId: string): string {
  return `/api/kwork/buyer-search/runs/${encodeURIComponent(runId)}/projects/${encodeURIComponent(projectId)}/outreach`
}

export const buyerOutreachApi = {
  promote: (runId: string, projectId: string, payload: BuyerOutreachPromotePayload) =>
    request<BuyerOutreachPromotion>(projectBase(runId, projectId) + '/promote', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getPromotion: (runId: string, projectId: string, signal?: AbortSignal) =>
    request<BuyerOutreachPromotion>(projectBase(runId, projectId), { signal }),
  listDrafts: (runId: string, projectId: string, signal?: AbortSignal) =>
    request<BuyerOutreachDraft[]>(projectBase(runId, projectId) + '/drafts', { signal }),
  generateDraft: (runId: string, projectId: string, payload: BuyerOutreachGenerateDraftPayload) =>
    request<BuyerOutreachDraft>(projectBase(runId, projectId) + '/drafts', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getDraft: (draftId: string, signal?: AbortSignal) =>
    request<BuyerOutreachDraft>(`/api/kwork/buyer-search/outreach/drafts/${encodeURIComponent(draftId)}`, { signal }),
  editDraft: (draftId: string, payload: BuyerOutreachEditDraftPayload) =>
    request<BuyerOutreachDraft>(`/api/kwork/buyer-search/outreach/drafts/${encodeURIComponent(draftId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  getPreflight: (draftId: string, signal?: AbortSignal) =>
    request<BuyerOutreachPreflight | null>(`/api/kwork/buyer-search/outreach/drafts/${encodeURIComponent(draftId)}/preflight`, { signal }),
  preflight: (draftId: string, payload: BuyerOutreachPreflightPayload) =>
    request<BuyerOutreachPreflightResponse>(`/api/kwork/buyer-search/outreach/drafts/${encodeURIComponent(draftId)}/preflight`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createSendIntent: (draftId: string, payload: BuyerOutreachCreateIntentPayload) =>
    request<BuyerOutreachSendIntentResponse>(`/api/kwork/buyer-search/outreach/drafts/${encodeURIComponent(draftId)}/send-intents`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getSendIntent: (intentId: string, signal?: AbortSignal) =>
    request<BuyerOutreachSendIntent>(`/api/kwork/buyer-search/outreach/send-intents/${encodeURIComponent(intentId)}`, { signal }),
  deliverSendIntent: (intentId: string, payload: BuyerOutreachDeliveryPayload) =>
    request<BuyerOutreachDeliveryResult>(`/api/kwork/buyer-search/outreach/send-intents/${encodeURIComponent(intentId)}/delivery`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  reconcileDelivery: (intentId: string, senderAccountRegistrationId: string) =>
    request<BuyerOutreachDeliveryResult>(`/api/kwork/buyer-search/outreach/send-intents/${encodeURIComponent(intentId)}/delivery/reconcile`, {
      method: 'POST',
      body: JSON.stringify({ sender_account_registration_id: senderAccountRegistrationId }),
    }),
}
