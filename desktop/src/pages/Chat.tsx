import { useState, useEffect, useRef, useCallback } from 'react'
import { Send, Zap, Trash2, Bot, User, Loader2, CheckCircle2, XCircle, RefreshCw } from 'lucide-react'
import { api, ProviderInfo, ChatMessage } from '../lib/api'
import { cn } from '../lib/utils'

interface UIMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  ts: number
  error?: boolean
}

const PROVIDER_LABELS: Record<string, string> = {
  openai: 'GPT (byesu)',
  deepseek: 'DeepSeek',
  groq: 'Groq',
}

const STORAGE_KEY = 'psr.chat.history.v1'
const PROVIDER_KEY = 'psr.chat.provider.v1'

export default function Chat() {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [selectedProvider, setSelectedProvider] = useState('openai')
  const [model, setModel] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<UIMessage[]>([])
  const [sending, setSending] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; latency: number; reply: string; error: string } | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    (api as any).getChatProviders().then((ps: ProviderInfo[]) => {
      setProviders(ps)
      const saved = localStorage.getItem(PROVIDER_KEY)
      const configured = ps.filter(p => p.configured)
      if (saved && ps.find(p => p.name === saved && p.configured)) {
        setSelectedProvider(saved)
        const p = ps.find(p => p.name === saved)
        if (p) setModel(p.model)
      } else if (configured.length > 0) {
        setSelectedProvider(configured[0].name)
        setModel(configured[0].model)
      }
    }).catch(() => {})
  }, [])

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (raw) setMessages(JSON.parse(raw))
    } catch {}
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-50)))
    } catch {}
  }, [messages])

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages])

  const handleProviderChange = useCallback((name: string) => {
    setSelectedProvider(name)
    localStorage.setItem(PROVIDER_KEY, name)
    const p = providers.find(p => p.name === name)
    if (p) setModel(p.model)
    setTestResult(null)
  }, [providers])

  const handleTest = useCallback(async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await (api as any).testChatProvider(selectedProvider, model)
      setTestResult({
        ok: result.ok,
        latency: result.latency_ms,
        reply: result.reply || '',
        error: result.error || '',
      })
    } catch (e) {
      setTestResult({ ok: false, latency: 0, reply: '', error: e instanceof Error ? e.message : 'Ошибка' })
    } finally {
      setTesting(false)
    }
  }, [selectedProvider, model])

  const handleSend = useCallback(async () => {
    const text = input.trim()
    if (!text || sending) return

    const userMsg: UIMessage = { role: 'user', content: text, ts: Date.now() }
    const newMessages = [...messages, userMsg]
    setMessages(newMessages)
    setInput('')
    setSending(true)

    const apiMessages: ChatMessage[] = newMessages.map(m => ({ role: m.role, content: m.content }))

    try {
      const result = await (api as any).sendChatMessage(selectedProvider, apiMessages, model, systemPrompt)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: result.reply,
        ts: Date.now(),
      }])
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: e instanceof Error ? e.message : 'Ошибка запроса',
        ts: Date.now(),
        error: true,
      }])
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }, [input, sending, messages, selectedProvider, model, systemPrompt])

  const handleClear = useCallback(() => {
    setMessages([])
    localStorage.removeItem(STORAGE_KEY)
  }, [])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const configuredProviders = providers.filter(p => p.configured)
  const currentProvider = providers.find(p => p.name === selectedProvider)
  const canSend = configuredProviders.length > 0 && !sending && input.trim().length > 0

  return (
    <div className="flex h-full flex-col">
      {/* Header / Provider selector */}
      <div className="shrink-0 border-b border-white/10 p-4">
        <div className="flex items-center justify-between gap-4 max-lg:flex-col max-lg:items-start">
          <div className="flex items-center gap-3">
            <Bot className="h-5 w-5 text-brand-400" />
            <div>
              <div className="page-kicker">ai chat</div>
              <h2 className="text-base font-semibold text-white">Чат с ИИ</h2>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Provider selector */}
            <select
              value={selectedProvider}
              onChange={(e) => handleProviderChange(e.target.value)}
              className="input h-9 w-40 text-xs"
            >
              {providers.map(p => (
                <option key={p.name} value={p.name} disabled={!p.configured}>
                  {PROVIDER_LABELS[p.name] || p.name}{!p.configured ? ' (нет ключа)' : ''}
                </option>
              ))}
            </select>

            {/* Model */}
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="model"
              className="input h-9 w-36 text-xs font-mono"
            />

            {/* Test button */}
            <button
              onClick={handleTest}
              disabled={testing || !currentProvider?.configured}
              className="btn btn-ghost h-9 px-3 text-xs"
            >
              {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
              Тест
            </button>

            {/* Clear button */}
            <button
              onClick={handleClear}
              disabled={messages.length === 0}
              className="btn btn-ghost h-9 px-3 text-xs"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        {/* Test result */}
        {testResult && (
          <div className={cn(
            'mt-3 flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs',
            testResult.ok
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
              : 'border-red-500/30 bg-red-500/10 text-red-300'
          )}>
            {testResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
            {testResult.ok ? (
              <>
                <span>Подключён · {testResult.latency}ms</span>
                {testResult.reply && <span className="text-zinc-400">· ответ: "{testResult.reply.slice(0, 60)}"</span>}
              </>
            ) : (
              <span>{testResult.error}</span>
            )}
          </div>
        )}

        {/* System prompt */}
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-zinc-500 hover:text-zinc-300">System prompt</summary>
          <textarea
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="Системный промпт (optional)"
            className="input mt-1 min-h-16 resize-none text-xs"
          />
        </details>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center text-center">
            <div className="space-y-2">
              <Bot className="mx-auto h-12 w-12 text-zinc-700" />
              <p className="text-sm text-zinc-500">
                Напишите сообщение для {PROVIDER_LABELS[selectedProvider] || selectedProvider}
              </p>
              <p className="text-xs text-zinc-600">
                {currentProvider?.base_url} · {currentProvider?.wire_api}
              </p>
            </div>
          </div>
        ) : (
          messages.map((msg, i) => (
            <div
              key={i}
              className={cn(
                'flex gap-3 max-w-3xl',
                msg.role === 'user' ? 'ml-auto flex-row-reverse' : ''
              )}
            >
              <div className={cn(
                'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
                msg.role === 'user'
                  ? 'bg-brand-600/20 text-brand-300'
                  : msg.error
                    ? 'bg-red-600/20 text-red-300'
                    : 'bg-surface-700 text-zinc-400'
              )}>
                {msg.role === 'user' ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
              </div>
              <div className={cn(
                'rounded-xl px-4 py-2.5 text-sm whitespace-pre-wrap break-words',
                msg.role === 'user'
                  ? 'bg-brand-600/15 text-stone-100'
                  : msg.error
                    ? 'bg-red-600/10 text-red-300 border border-red-500/20'
                    : 'bg-surface-800 text-zinc-200 border border-white/5'
              )}>
                {msg.content}
              </div>
            </div>
          ))
        )}
        {sending && (
          <div className="flex gap-3 max-w-3xl">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface-700 text-zinc-400">
              <Bot className="h-4 w-4" />
            </div>
            <div className="rounded-xl bg-surface-800 border border-white/5 px-4 py-2.5">
              <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="shrink-0 border-t border-white/10 p-4">
        <div className="flex gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Сообщение для ${PROVIDER_LABELS[selectedProvider] || selectedProvider}...`}
            disabled={sending}
            className="input flex-1 resize-none text-sm"
            rows={2}
          />
          <button
            onClick={handleSend}
            disabled={!canSend}
            className="btn btn-primary px-4"
          >
            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </button>
        </div>
        <div className="mt-1.5 flex items-center justify-between text-[11px] text-zinc-600">
          <span>Enter — отправить · Shift+Enter — перенос строки</span>
          <span>{messages.length} сообщ.</span>
        </div>
      </div>
    </div>
  )
}
