export interface BuyerConversationContext {
  project_id: string | null
  proposal_draft_id: string | null
  proposal_intent_id: string | null
  buyer_remote_user_id: string | null
  metadata: Record<string, unknown>
}

export interface BuyerConversationAttachment {
  remote_attachment_id: string | null
  filename: string | null
  content_type: string | null
  remote_url: string | null
  metadata: Record<string, unknown>
}

export interface BuyerConversation {
  conversation_id: string
  platform: 'kwork' | string
  account_registration_id: string
  remote_dialog_id: string
  context: BuyerConversationContext
  remote_title: string | null
  last_remote_message_at: string | null
  synced_at: string | null
}

export interface BuyerConversationMessage {
  conversation_id: string
  platform: 'kwork' | string
  account_registration_id: string
  remote_dialog_id: string
  message_id: string
  remote_message_id: string
  direction: 'incoming' | 'outgoing'
  body: string | null
  sender_account_registration_id: string | null
  remote_sender_id: string | null
  remote_created_at: string | null
  observed_at: string | null
  delivery_state: string
  read_at: string | null
  attachments: BuyerConversationAttachment[]
  context: BuyerConversationContext
  message_hash: string
}

export interface BuyerConversationDraft {
  conversation_id: string
  platform: 'kwork' | string
  account_registration_id: string
  remote_dialog_id: string
  draft_id: string
  sender_account_registration_id: string
  body: string | null
  context: BuyerConversationContext
  created_at: string | null
  source: string
  state: 'draft'
  draft_hash: string
}

export interface BuyerConversationCopilotDraftResult {
  draft: BuyerConversationDraft
  draft_id: string
  conversation_id: string
  account_registration_id: string
  task: 'conversation_reply' | string
  resolved_provider: string
  resolved_model: string
  provider: string
  model: string
  prompt_hash: string
  context_hash: string
  context_manifest: Record<string, unknown>
  outbox_only: true
  auto_send: false
}

export interface BuyerConversationSendIntent {
  intent_id: string
  draft_id: string
  sender_account_registration_id: string
  state: 'pending' | 'sending' | 'sent' | 'unknown' | 'failed' | string
  send_attempts: number
  remote_message_id: string | null
  remote_receipt: string | null
  failure_reason: string | null
  idempotency_key: string
}

export interface BuyerConversationSendResult {
  send_intent: BuyerConversationSendIntent
  idempotent: boolean
  auto_send: false
  requires_reconciliation: boolean
}

export interface BuyerConversationDraftSendPayload {
  requested_by: string
  command_id: string
  confirm_send: true
}

export interface CreateBuyerConversationDraftPayload {
  draft_id?: string
  body: string
  context?: Partial<BuyerConversationContext>
  created_at?: string
  source?: 'operator' | string
}

export interface CreateBuyerConversationCopilotDraftPayload {
  draft_id?: string
  operator_instruction?: string
  created_at?: string
}

export interface BuyerConversationListResponse<T> {
  items: T[]
}
