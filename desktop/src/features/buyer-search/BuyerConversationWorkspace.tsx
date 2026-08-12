import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Bot,
  Clock3,
  FileText,
  Loader2,
  MessageSquareText,
  Paperclip,
  RefreshCw,
  Save,
  Send,
  Sparkles,
  UserRound,
} from 'lucide-react'

import { cn } from '../../lib/utils'
import { buyerConversationApi } from './conversationApi'
import type {
  BuyerConversation,
  BuyerConversationDraft,
  BuyerConversationMessage,
  BuyerConversationSendIntent,
} from './conversationTypes'

export interface BuyerConversationWorkspaceProps {
  accountRegistrationId: string | null | undefined
  projectId?: string | null
  initialDialogId?: string | null
  className?: string
  onDialogChange?: (dialog: BuyerConversation | null) => void
}

type DraftOperation = 'operator' | 'copilot' | 'send' | null

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return 'Время неизвестно'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function conversationTitle(conversation: BuyerConversation): string {
  return conversation.remote_title || `Диалог ${conversation.remote_dialog_id}`
}

function draftSourceLabel(source: string): string {
  return {
    operator: 'Оператор',
    ai_copilot: 'ИИ-помощник',
  }[source] || source
}

function sendStateLabel(state: string): string {
  return {
    pending: 'ожидает отправки',
    sending: 'отправляется',
    sent: 'отправлено',
    unknown: 'требует сверки',
    failed: 'ошибка отправки',
  }[state] || state
}

function mergeDraft(drafts: BuyerConversationDraft[], draft: BuyerConversationDraft): BuyerConversationDraft[] {
  const withoutDuplicate = drafts.filter((item) => item.draft_id !== draft.draft_id)
  return [draft, ...withoutDuplicate]
}

function ConversationListItem({
  conversation,
  selected,
  onSelect,
}: {
  conversation: BuyerConversation
  selected: boolean
  onSelect: (remoteDialogId: string) => void
}) {
  return (
    <button
      type="button"
      className={cn(
        'w-full border-b border-surface-800 px-3 py-2.5 text-left transition-colors',
        selected ? 'bg-cyan-400/10 text-zinc-100' : 'text-zinc-300 hover:bg-surface-800',
      )}
      aria-pressed={selected}
      onClick={() => onSelect(conversation.remote_dialog_id)}
    >
      <span className="block truncate text-xs font-medium">{conversationTitle(conversation)}</span>
      <span className="mt-1 flex items-center gap-1 truncate font-mono text-[10px] text-zinc-500">
        <Clock3 className="h-3 w-3 shrink-0" />
        {formatTimestamp(conversation.last_remote_message_at || conversation.synced_at)}
      </span>
    </button>
  )
}

function HistoryMessage({ message }: { message: BuyerConversationMessage }) {
  const incoming = message.direction === 'incoming'
  const attachmentCount = message.attachments.length
  return (
    <article
      className={cn(
        'max-w-[90%] border px-3 py-2 text-xs',
        incoming
          ? 'border-surface-700 bg-surface-800 text-zinc-200'
          : 'ml-auto border-cyan-500/20 bg-cyan-400/10 text-cyan-50',
      )}
    >
      <div className="mb-1 flex items-center gap-1.5 text-[10px] text-zinc-500">
        {incoming ? <UserRound className="h-3 w-3 text-amber-300" /> : <MessageSquareText className="h-3 w-3 text-cyan-300" />}
        <span>{incoming ? 'Заказчик' : 'Аккаунт'}</span>
        <span className="font-mono">{formatTimestamp(message.remote_created_at || message.observed_at)}</span>
      </div>
      {message.body ? <p className="whitespace-pre-wrap break-words leading-5">{message.body}</p> : null}
      {attachmentCount ? (
        <div className="mt-2 flex items-center gap-1 text-[10px] text-cyan-200">
          <Paperclip className="h-3 w-3" />
          {attachmentCount}
        </div>
      ) : null}
    </article>
  )
}

function DraftRecord({
  draft,
  sendIntent,
  sending,
  onSend,
}: {
  draft: BuyerConversationDraft
  sendIntent?: BuyerConversationSendIntent
  sending: boolean
  onSend: (draft: BuyerConversationDraft) => void
}) {
  return (
    <article className="border-t border-surface-800 px-3 py-2.5 text-xs">
      <div className="flex items-center justify-between gap-2 text-[10px] text-zinc-500">
        <span className="flex min-w-0 items-center gap-1 truncate">
          {draft.source === 'ai_copilot' ? <Bot className="h-3 w-3 text-cyan-300" /> : <FileText className="h-3 w-3 text-zinc-400" />}
          <span className="truncate">{draftSourceLabel(draft.source)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1"><span className="font-mono">{formatTimestamp(draft.created_at)}</span><button type="button" className="btn btn-ghost h-6 w-6 justify-center px-0" title="Отправить этот черновик" aria-label="Отправить этот черновик" disabled={sending || Boolean(sendIntent && sendIntent.state !== 'pending')} onClick={() => onSend(draft)}>{sending ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3 text-lime-300" />}</button></span>
      </div>
      <p className="mt-1.5 whitespace-pre-wrap break-words leading-5 text-zinc-300">{draft.body || 'Пустой черновик'}</p>
      {sendIntent ? <div className={cn('mt-1 font-mono text-[10px]', sendIntent.state === 'sent' ? 'text-emerald-300' : sendIntent.state === 'unknown' || sendIntent.state === 'failed' ? 'text-rose-300' : 'text-cyan-200')}>{sendStateLabel(sendIntent.state)}{sendIntent.remote_receipt ? ` ${sendIntent.remote_receipt}` : ''}</div> : null}
    </article>
  )
}

export function BuyerConversationWorkspace({
  accountRegistrationId,
  projectId,
  initialDialogId,
  className,
  onDialogChange,
}: BuyerConversationWorkspaceProps) {
  const [conversations, setConversations] = useState<BuyerConversation[]>([])
  const [selectedDialogId, setSelectedDialogId] = useState<string | null>(null)
  const [messages, setMessages] = useState<BuyerConversationMessage[]>([])
  const [drafts, setDrafts] = useState<BuyerConversationDraft[]>([])
  const [sendIntents, setSendIntents] = useState<Record<string, BuyerConversationSendIntent>>({})
  const [composeText, setComposeText] = useState('')
  const [loadingConversations, setLoadingConversations] = useState(false)
  const [loadingDetails, setLoadingDetails] = useState(false)
  const [draftOperation, setDraftOperation] = useState<DraftOperation>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)

  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.remote_dialog_id === selectedDialogId) || null,
    [conversations, selectedDialogId],
  )

  useEffect(() => {
    onDialogChange?.(selectedConversation)
  }, [onDialogChange, selectedConversation])

  useEffect(() => {
    if (!accountRegistrationId) {
      setConversations([])
      setSelectedDialogId(null)
      setMessages([])
      setDrafts([])
      setSendIntents({})
      setError(null)
      return undefined
    }

    const controller = new AbortController()
    setLoadingConversations(true)
    setError(null)
    void buyerConversationApi.listConversations(accountRegistrationId, controller.signal)
      .then(({ items }) => {
        setConversations(items)
        setSelectedDialogId((current) => {
          if (initialDialogId && items.some((item) => item.remote_dialog_id === initialDialogId)) return initialDialogId
          if (current && items.some((item) => item.remote_dialog_id === current)) return current
          return items[0]?.remote_dialog_id || null
        })
      })
      .catch((loadError: unknown) => {
        if (!isAbortError(loadError)) setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить диалоги')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingConversations(false)
      })
    return () => controller.abort()
  }, [accountRegistrationId, initialDialogId, refreshToken])

  useEffect(() => {
    if (!accountRegistrationId || !selectedDialogId) {
      setMessages([])
      setDrafts([])
      return undefined
    }

    const controller = new AbortController()
    setLoadingDetails(true)
    setError(null)
    void Promise.all([
      buyerConversationApi.listMessages(accountRegistrationId, selectedDialogId, controller.signal),
      buyerConversationApi.listDrafts(accountRegistrationId, selectedDialogId, controller.signal),
    ])
      .then(([messageResponse, draftResponse]) => {
      setMessages(messageResponse.items)
      setDrafts(draftResponse.items)
      })
      .catch((loadError: unknown) => {
        if (!isAbortError(loadError)) setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить историю диалога')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingDetails(false)
      })
    return () => controller.abort()
  }, [accountRegistrationId, selectedDialogId, refreshToken])

  const saveOperatorDraft = useCallback(async () => {
    if (!accountRegistrationId || !selectedDialogId || !composeText.trim() || draftOperation) return
    setDraftOperation('operator')
    setError(null)
    try {
      const draft = await buyerConversationApi.createOperatorDraft(accountRegistrationId, selectedDialogId, {
        body: composeText.trim(),
        context: projectId ? { project_id: projectId } : undefined,
      })
      setDrafts((current) => mergeDraft(current, draft))
      setComposeText('')
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : 'Не удалось создать черновик')
    } finally {
      setDraftOperation(null)
    }
  }, [accountRegistrationId, composeText, draftOperation, projectId, selectedDialogId])

  const createCopilotDraft = useCallback(async () => {
    if (!accountRegistrationId || !selectedDialogId || draftOperation) return
    setDraftOperation('copilot')
    setError(null)
    try {
      const result = await buyerConversationApi.createCopilotDraft(accountRegistrationId, selectedDialogId, {
        operator_instruction: composeText.trim() || undefined,
      })
      setDrafts((current) => mergeDraft(current, result.draft))
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : 'Не удалось создать черновик с ИИ')
    } finally {
      setDraftOperation(null)
    }
  }, [accountRegistrationId, composeText, draftOperation, selectedDialogId])

  const sendDraft = useCallback(async (draft: BuyerConversationDraft) => {
    if (!accountRegistrationId || !selectedDialogId || draftOperation) return
    setDraftOperation('send')
    setError(null)
    try {
      const result = await buyerConversationApi.sendDraft(accountRegistrationId, selectedDialogId, draft.draft_id, {
        requested_by: 'operator',
        command_id: crypto.randomUUID(),
        confirm_send: true,
      })
      setSendIntents((current) => ({ ...current, [draft.draft_id]: result.send_intent }))
      setRefreshToken((current) => current + 1)
    } catch (sendError) {
      setError(sendError instanceof Error ? sendError.message : 'Не удалось отправить черновик сообщения')
    } finally {
      setDraftOperation(null)
    }
  }, [accountRegistrationId, draftOperation, selectedDialogId])

  if (!accountRegistrationId) {
    return (
      <section className={cn('border border-surface-700 bg-surface-925', className)} aria-label="Рабочая область диалогов с заказчиками">
        <div className="flex min-h-40 items-center justify-center px-4 text-center text-xs text-zinc-500">
          Выберите аккаунт, чтобы просмотреть диалоги с заказчиками.
        </div>
      </section>
    )
  }

  return (
    <section className={cn('flex min-h-[30rem] flex-col border border-surface-700 bg-surface-925', className)} aria-label="Рабочая область диалогов с заказчиками">
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-surface-700 px-3 py-2.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-medium text-zinc-100">
            <MessageSquareText className="h-4 w-4 shrink-0 text-cyan-300" />
            <span className="truncate">Диалоги с заказчиками</span>
          </div>
          <div className="mt-1 truncate font-mono text-[10px] text-zinc-500" title={accountRegistrationId}>{accountRegistrationId}</div>
        </div>
        <button
          type="button"
          className="btn btn-ghost h-7 w-7 shrink-0 justify-center px-0"
          title="Обновить диалоги"
          aria-label="Обновить диалоги"
          disabled={loadingConversations || loadingDetails}
          onClick={() => setRefreshToken((current) => current + 1)}
        >
          <RefreshCw className={cn('h-3.5 w-3.5', (loadingConversations || loadingDetails) && 'animate-spin')} />
        </button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 xl:grid-cols-[minmax(10rem,0.72fr)_minmax(0,1.5fr)]">
        <nav className="min-h-0 border-b border-surface-700 xl:border-b-0 xl:border-r" aria-label="Диалоги с заказчиками">
          <div className="flex h-full max-h-40 overflow-auto xl:max-h-none xl:flex-col">
            {loadingConversations && !conversations.length ? (
              <div className="flex min-h-20 flex-1 items-center justify-center text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" /></div>
            ) : null}
            {!loadingConversations && !conversations.length ? (
              <div className="flex min-h-20 flex-1 items-center justify-center px-3 text-center text-xs text-zinc-500">Диалогов нет</div>
            ) : null}
            {conversations.map((conversation) => (
              <ConversationListItem
                key={conversation.conversation_id}
                conversation={conversation}
                selected={conversation.remote_dialog_id === selectedDialogId}
                onSelect={setSelectedDialogId}
              />
            ))}
          </div>
        </nav>

        <div className="grid min-h-0 grid-rows-[minmax(10rem,1fr)_auto_auto]">
          <div className="min-h-0 overflow-auto px-3 py-3">
            {selectedConversation ? (
              <div className="mb-3 border-b border-surface-800 pb-2">
                <h3 className="truncate text-xs font-medium text-zinc-100">{conversationTitle(selectedConversation)}</h3>
                <div className="mt-1 truncate font-mono text-[10px] text-zinc-500">{selectedConversation.remote_dialog_id}</div>
              </div>
            ) : null}
            {loadingDetails ? (
              <div className="flex min-h-24 items-center justify-center text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" /></div>
            ) : null}
            {!loadingDetails && selectedConversation && !messages.length ? (
              <div className="py-6 text-center text-xs text-zinc-500">История сообщений пуста</div>
            ) : null}
            <div className="space-y-2">
              {messages.map((message) => <HistoryMessage key={message.message_id} message={message} />)}
            </div>
          </div>

          <div className="border-t border-surface-700 px-3 py-2.5">
            <textarea
              className="input min-h-20 w-full resize-y py-2 text-xs leading-5"
              value={composeText}
              disabled={!selectedConversation || draftOperation !== null}
              placeholder="Заметка к черновику или инструкция для ИИ"
              aria-label="Содержание черновика сообщения или инструкция для ИИ"
              onChange={(event) => setComposeText(event.target.value)}
            />
            <div className="mt-2 flex items-center justify-between gap-2">
              <span className="text-[10px] uppercase tracking-wide text-zinc-500">Только черновики</span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  className="btn btn-ghost h-7 w-7 justify-center px-0"
                  title="Создать черновик с ИИ"
                  aria-label="Создать черновик с ИИ"
                  disabled={!selectedConversation || draftOperation !== null}
                  onClick={() => void createCopilotDraft()}
                >
                  {draftOperation === 'copilot' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-cyan-300" />}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost h-7 w-7 justify-center px-0"
                  title="Сохранить операторский черновик"
                  aria-label="Сохранить операторский черновик"
                  disabled={!selectedConversation || !composeText.trim() || draftOperation !== null}
                  onClick={() => void saveOperatorDraft()}
                >
                  {draftOperation === 'operator' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5 text-lime-300" />}
                </button>
              </div>
            </div>
          </div>

          <div className="max-h-44 overflow-auto border-t border-surface-700">
            <div className="flex items-center justify-between border-b border-surface-800 px-3 py-2 text-[10px] uppercase tracking-wide text-zinc-500">
              <span>Сохраненные черновики</span>
              <span className="font-mono">{drafts.length}</span>
            </div>
            {!drafts.length ? <div className="px-3 py-4 text-xs text-zinc-500">Локальных черновиков нет</div> : null}
            {drafts.map((draft) => <DraftRecord key={draft.draft_id} draft={draft} sendIntent={sendIntents[draft.draft_id]} sending={draftOperation === 'send'} onSend={sendDraft} />)}
          </div>
        </div>
      </div>

      {error ? <div className="border-t border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{error}</div> : null}
    </section>
  )
}

export default BuyerConversationWorkspace
