import { API_BASE } from '../../lib/api'
import type {
  BuyerConversation,
  BuyerConversationCopilotDraftResult,
  BuyerConversationDraft,
  BuyerConversationDraftSendPayload,
  BuyerConversationListResponse,
  BuyerConversationMessage,
  BuyerConversationSendIntent,
  BuyerConversationSendResult,
  CreateBuyerConversationCopilotDraftPayload,
  CreateBuyerConversationDraftPayload,
} from './conversationTypes'

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
      // Keep non-JSON response text available to the workspace diagnostics.
    }
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

function conversationPath(accountRegistrationId: string, remoteDialogId?: string): string {
  const account = encodeURIComponent(accountRegistrationId)
  const base = `/api/kwork/buyer-search/accounts/${account}/conversations`
  return remoteDialogId ? `${base}/${encodeURIComponent(remoteDialogId)}` : base
}

export const buyerConversationApi = {
  listConversations: (accountRegistrationId: string, signal?: AbortSignal) =>
    request<BuyerConversationListResponse<BuyerConversation>>(conversationPath(accountRegistrationId), { signal }),
  getConversation: (accountRegistrationId: string, remoteDialogId: string, signal?: AbortSignal) =>
    request<BuyerConversation>(conversationPath(accountRegistrationId, remoteDialogId), { signal }),
  listMessages: (accountRegistrationId: string, remoteDialogId: string, signal?: AbortSignal) =>
    request<BuyerConversationListResponse<BuyerConversationMessage>>(
      `${conversationPath(accountRegistrationId, remoteDialogId)}/messages`,
      { signal },
    ),
  listDrafts: (accountRegistrationId: string, remoteDialogId: string, signal?: AbortSignal) =>
    request<BuyerConversationListResponse<BuyerConversationDraft>>(
      `${conversationPath(accountRegistrationId, remoteDialogId)}/drafts`,
      { signal },
    ),
  createOperatorDraft: (
    accountRegistrationId: string,
    remoteDialogId: string,
    payload: CreateBuyerConversationDraftPayload,
  ) =>
    request<BuyerConversationDraft>(`${conversationPath(accountRegistrationId, remoteDialogId)}/drafts`, {
      method: 'POST',
      body: JSON.stringify({ ...payload, source: payload.source || 'operator' }),
    }),
  createCopilotDraft: (
    accountRegistrationId: string,
    remoteDialogId: string,
    payload: CreateBuyerConversationCopilotDraftPayload = {},
  ) =>
    request<BuyerConversationCopilotDraftResult>(`${conversationPath(accountRegistrationId, remoteDialogId)}/reply-drafts`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  sendDraft: (
    accountRegistrationId: string,
    remoteDialogId: string,
    draftId: string,
    payload: BuyerConversationDraftSendPayload,
  ) =>
    request<BuyerConversationSendResult>(
      `${conversationPath(accountRegistrationId, remoteDialogId)}/drafts/${encodeURIComponent(draftId)}/send`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  getSendIntent: (accountRegistrationId: string, remoteDialogId: string, intentId: string, signal?: AbortSignal) =>
    request<BuyerConversationSendResult>(
      `${conversationPath(accountRegistrationId, remoteDialogId)}/send-intents/${encodeURIComponent(intentId)}`,
      { signal },
    ),
}
