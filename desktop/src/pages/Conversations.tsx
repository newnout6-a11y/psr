import { useState, useCallback } from 'react'
import { MessageSquare, Send, ArrowLeft, Loader2 } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, ConversationRow, ConversationMessage } from '../lib/api'
import { cn } from '../lib/utils'

const STATUS_LABELS: Record<string, string> = {
  new: 'новый',
  awaiting_reply: 'ждёт ответа',
  replied: 'отвечено',
  confirmed: 'подтверждено',
  completed: 'завершён',
}

const STATUS_COLORS: Record<string, string> = {
  new: 'text-zinc-400',
  awaiting_reply: 'text-amber-400',
  replied: 'text-blue-400',
  confirmed: 'text-emerald-400',
  completed: 'text-zinc-500',
}

export default function Conversations() {
  const { data: conversations, loading } = useApi(() => (api as any).getConversations(100), [])
  const [selected, setSelected] = useState<ConversationRow | null>(null)
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [replyText, setReplyText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const openConversation = useCallback(async (conv: ConversationRow) => {
    setSelected(conv)
    setLoadingMsgs(true)
    setError('')
    setMessages([])
    try {
      const result = await (api as any).getConversationHistory(conv.project_id, conv.platform)
      setMessages(result.messages || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка загрузки')
    } finally {
      setLoadingMsgs(false)
    }
  }, [])

  const handleSend = useCallback(async () => {
    if (!selected || !replyText.trim() || sending) return
    setSending(true)
    setError('')
    try {
      const candidate = selected.candidate_id
      if (!candidate) {
        setError('Нет candidate_id для отправки')
        return
      }
      await (api as any).sendMessageToClient(candidate, '', replyText.trim())
      setReplyText('')
      const result = await (api as any).getConversationHistory(selected.project_id, selected.platform)
      setMessages(result.messages || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка отправки')
    } finally {
      setSending(false)
    }
  }, [selected, replyText, sending])

  const convs: ConversationRow[] = conversations || []

  if (selected) {
    return (
      <div className="flex h-full flex-col">
        <div className="shrink-0 border-b border-white/10 p-4">
          <button
            onClick={() => setSelected(null)}
            className="flex items-center gap-2 text-sm text-zinc-400 hover:text-white"
          >
            <ArrowLeft className="h-4 w-4" />
            Назад к списку
          </button>
          <h2 className="mt-2 text-sm font-semibold text-white">
            {selected.project_title || selected.project_id}
          </h2>
          <div className="mt-1 text-xs text-zinc-500">
            {selected.platform} · id={selected.project_id} ·{' '}
            <span className={STATUS_COLORS[selected.status] || 'text-zinc-400'}>
              {STATUS_LABELS[selected.status] || selected.status}
            </span>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-2">
          {loadingMsgs ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-zinc-600" />
            </div>
          ) : messages.length === 0 ? (
            <div className="text-center text-sm text-zinc-500 py-12">Нет сообщений</div>
          ) : (
            messages.map((msg) => (
              <div
                key={msg.message_id}
                className={cn(
                  'max-w-2xl rounded-lg px-4 py-2.5 text-sm',
                  msg.sender === 'customer'
                    ? 'bg-surface-800 border border-white/5'
                    : 'ml-auto bg-brand-600/15 border border-brand-500/20',
                )}
              >
                <div className="mb-1 flex items-center gap-2 text-[10px] text-zinc-500">
                  <span className={msg.sender === 'customer' ? 'text-amber-400' : 'text-brand-300'}>
                    {msg.sender === 'customer' ? 'Клиент' : 'Вы'}
                  </span>
                  <span>{msg.created_at?.slice(11, 16)}</span>
                </div>
                <div className="text-zinc-200 whitespace-pre-wrap">{msg.message_text || '—'}</div>
              </div>
            ))
          )}
          {error && <div className="text-sm text-red-400">{error}</div>}
        </div>

        <div className="shrink-0 border-t border-white/10 p-4">
          <div className="flex gap-2">
            <input
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSend()}
              placeholder="Ответ клиенту..."
              disabled={sending}
              className="input flex-1 text-sm"
            />
            <button onClick={handleSend} disabled={sending || !replyText.trim()} className="btn btn-primary px-4">
              {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4 p-6 max-lg:p-4">
      <div>
        <div className="page-kicker">client chats</div>
        <h1 className="text-xl font-semibold text-white">Диалоги с клиентами</h1>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-zinc-600" />
        </div>
      ) : convs.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-16 text-center">
          <MessageSquare className="h-10 w-10 text-zinc-700" />
          <p className="mt-3 text-sm text-zinc-500">Нет активных диалогов</p>
        </div>
      ) : (
        <div className="card overflow-visible">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-700 text-zinc-500">
                <th className="text-left py-2 px-2">Проект</th>
                <th className="text-left py-2 px-2 w-20">Платформа</th>
                <th className="text-left py-2 px-2 w-28">Статус</th>
                <th className="text-right py-2 px-2 w-28">Обновлён</th>
              </tr>
            </thead>
            <tbody>
              {convs.map((conv) => (
                <tr
                  key={conv.conversation_id}
                  onClick={() => openConversation(conv)}
                  className="cursor-pointer border-b border-surface-700/40 hover:bg-surface-800/50"
                >
                  <td className="py-2 px-2">
                    <div className="text-zinc-200 truncate max-w-md">
                      {conv.project_title || conv.project_id}
                    </div>
                    <div className="text-zinc-600 text-[10px]">id={conv.project_id}</div>
                  </td>
                  <td className="py-2 px-2 text-zinc-500">{conv.platform}</td>
                  <td className="py-2 px-2">
                    <span className={STATUS_COLORS[conv.status] || 'text-zinc-400'}>
                      {STATUS_LABELS[conv.status] || conv.status}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-right text-zinc-600">
                    {conv.updated_at?.slice(5, 16) || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
