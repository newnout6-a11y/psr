import { useState } from 'react'
import { Bot, Send, Sparkles } from 'lucide-react'

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
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div className="flex items-center gap-3">
          <span className="market-icon-tile"><Bot className="h-4 w-4" /></span>
          <div>
            <div className="market-section-label">Ассистент</div>
            <h2 className="mt-1 text-base font-semibold text-white">Спросить по этому запуску</h2>
          </div>
        </div>
        <button type="button" className="market-compact-action" disabled={busy} onClick={() => void ask(SUMMARY_QUESTION)}>
          <Sparkles className="h-3.5 w-3.5" />
          {busy ? 'Готовим ответ…' : 'Краткий вывод'}
        </button>
      </div>
      <div className="p-4">
        {results && !results.latest_checkpoint && (
          <p className="market-notice mb-3">
          Это разбор частичного среза: {formatCount(uniqueCards)} из {formatCount(targetCards)} карточек. Итоговый срез ещё не создан.
          </p>
        )}
        <form className="market-assistant-compose" onSubmit={(event) => { event.preventDefault(); void ask(message) }}>
          <textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Например: какая ниша выглядит наименее перегретой?" disabled={busy} />
          <button type="submit" className="market-icon-action" title="Отправить вопрос" aria-label="Отправить вопрос" disabled={busy || !message.trim()}><Send className="h-4 w-4" /></button>
        </form>
        {error && <p className="market-error-banner mt-3">{error}</p>}
        {answer && <div className="market-assistant-answer">{answer}</div>}
      </div>
    </section>
  )
}
