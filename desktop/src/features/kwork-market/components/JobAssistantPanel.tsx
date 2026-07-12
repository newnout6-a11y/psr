import { useState } from 'react'
import { Send, Sparkles } from 'lucide-react'

import { marketJobsApi } from '../api'
import type { MarketResults } from '../types'
import { formatCount } from './shared'

const SUMMARY_QUESTION = 'Сделай краткий вывод по текущему срезу: что видно по ценам, продавцам и границам выборки?'

export function JobAssistantPanel({ jobId, results }: { jobId: string; results: MarketResults | null }) {
  const [message, setMessage] = useState('')
  const [answer, setAnswer] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const uniqueCards = results?.counters.unique_cards ?? 0
  const targetCards = results?.target_unique_cards ?? 0

  async function ask(question: string) {
    const normalized = question.trim()
    if (!normalized || busy) return
    setBusy(true)
    setError(null)
    try {
      const response = await marketJobsApi.askAssistant(jobId, normalized)
      setAnswer(response.answer)
      setMessage('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="factory-panel p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-medium text-white"><Sparkles className="h-4 w-4 text-sky-300" />AI-разбор текущего запуска</h2>
          <p className="mt-1 text-xs text-zinc-500">Контекст привязан к этому запуску и обновляется из его сохранённых карточек.</p>
        </div>
        <button type="button" className="btn btn-ghost h-8 px-3 text-xs" disabled={busy} onClick={() => void ask(SUMMARY_QUESTION)}>
          {busy ? 'Готовим ответ…' : 'Краткий вывод'}
        </button>
      </div>
      {results && !results.latest_checkpoint && (
        <p className="mt-3 border-l-2 border-amber-500/50 pl-3 text-xs text-zinc-400">
          Это разбор частичного среза: {formatCount(uniqueCards)} из {formatCount(targetCards)} карточек. Итоговый срез ещё не создан.
        </p>
      )}
      <form className="mt-3 flex gap-2 max-md:flex-col" onSubmit={(event) => { event.preventDefault(); void ask(message) }}>
        <textarea className="input min-h-16 flex-1 px-3 py-2 text-xs" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Задайте вопрос по этому запуску" disabled={busy} />
        <button type="submit" className="btn h-auto min-h-10 px-3 max-md:self-end" title="Отправить вопрос" aria-label="Отправить вопрос" disabled={busy || !message.trim()}><Send className="h-4 w-4" /></button>
      </form>
      {error && <p className="mt-3 border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</p>}
      {answer && <div className="mt-3 whitespace-pre-wrap border-l-2 border-sky-500/50 pl-3 text-sm leading-6 text-zinc-200">{answer}</div>}
    </section>
  )
}
