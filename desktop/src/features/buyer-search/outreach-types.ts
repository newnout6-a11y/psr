export type BuyerOutreachJson = Record<string, unknown>

export interface BuyerOutreachPromotion {
  promotion_id: string
  run_id: string
  project_id: string
  platform: string
  sender_account_registration_id: string
  project: BuyerOutreachJson
  project_hash: string
  service_profile: BuyerOutreachJson
  score: BuyerOutreachJson
  additional_context: BuyerOutreachJson
  attachment_context_hash: string | null
  attachment_count: number
}

export interface BuyerOutreachDraft {
  draft_id: string
  promotion_id: string
  run_id: string
  project_id: string
  platform: string
  body: string
  price: number | null
  delivery_days: number | null
  currency: string
  context: BuyerOutreachJson
  context_hash: string
  draft_hash: string
  model_alias: string | null
  version: number
  sender_account_registration_id: string
  resolved_provider: string
  resolved_model: string
  provider: string
  model: string
  task: string
  prompt_version: string
  prompt_hash: string
  parent_draft_id: string | null
  source: string
  generated_body: string | null
  context_manifest: BuyerOutreachJson
}

export interface BuyerOutreachPreflightDiagnostic {
  code: string
  passed: boolean
  message: string
  severity: 'error' | 'warning' | 'info' | string
}

export interface BuyerOutreachPreflight {
  passed: boolean
  checks: Record<string, boolean>
  failures: string[]
  diagnostics: BuyerOutreachPreflightDiagnostic[]
  current_project_hash?: string | null
}

export interface BuyerOutreachPreflightResponse {
  draft: BuyerOutreachDraft
  preflight: BuyerOutreachPreflight
}

export interface BuyerOutreachSendIntent {
  intent_id: string
  draft_id: string
  project_id: string
  platform: string
  sender_account_registration_id: string
  idempotency_key: string
  context_hash: string
  draft_hash: string
  state: string
  preflight: Pick<BuyerOutreachPreflight, 'passed' | 'checks' | 'failures'> | null
  send_attempts: number
  remote_receipt: string | null
  failure_reason: string | null
}

export interface BuyerOutreachConfirmation {
  confirmation_id: string
  intent_id: string
  confirmed_by: string
  confirmed_at: string
}

export interface BuyerOutreachSendIntentResponse {
  draft: BuyerOutreachDraft
  preflight: BuyerOutreachPreflight
  send_intent: BuyerOutreachSendIntent
  confirmation: BuyerOutreachConfirmation
  outbox_only: true
  auto_send: false
}

export interface BuyerOutreachDeliveryPayload {
  sender_account_registration_id: string
  confirmed_by: string
  delivery_confirmation_id: string
  explicit_delivery_confirmation: true
}

export interface BuyerOutreachDeliveryResult {
  send_intent: BuyerOutreachSendIntent
  auto_send: false
  delivery: {
    mode: string
    delivery_confirmation_id?: string
    idempotent_replay?: boolean
    remote_request_performed?: boolean
    requires_reconciliation?: boolean
  }
  evidence?: BuyerOutreachJson | null
}

export interface BuyerOutreachPromotePayload {
  sender_account_registration_id: string
  service_profile: BuyerOutreachJson
  additional_context?: BuyerOutreachJson
}

export interface BuyerOutreachGenerateDraftPayload {
  price?: number | null
  delivery_days?: number | null
  currency?: string
  model_alias?: string | null
  additional_context?: BuyerOutreachJson
}

export interface BuyerOutreachEditDraftPayload {
  body: string
  price?: number | null
  delivery_days?: number | null
  currency?: string | null
}

export interface BuyerOutreachPreflightPayload {
  project_is_active?: boolean | null
  has_offer?: boolean | null
  already_work?: boolean | null
  duplicate_send_intent?: boolean
  sender_account_eligible?: boolean | null
  account_session_valid?: boolean | null
  connects_sufficient?: boolean | null
  template_valid?: boolean | null
  portfolio_requirements_met?: boolean | null
  outgoing_attachment_count?: number
  attachment_upload_capability_verified?: boolean
}

export interface BuyerOutreachCreateIntentPayload {
  sender_account_registration_id: string
  confirmed_by: string
  confirmation_id?: string
}
