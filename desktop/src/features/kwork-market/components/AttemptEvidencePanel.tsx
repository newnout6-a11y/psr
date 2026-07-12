import { X } from 'lucide-react'

import type { MarketOperation, MarketOperationAttempt } from '../types'
import { IconButton, formatCount, formatTimestamp, operationStateLabel } from './shared'

interface AttemptEvidencePanelProps {
  operation: MarketOperation | null
  attempts: MarketOperationAttempt[]
  loading: boolean
  error: string | null
  onClose(): void
}

function cursorSummary(cursor: MarketOperationAttempt['requested_cursor']): string {
  if (!cursor) return 'не передавалась'
  const excluded = Array.isArray(cursor.exclude_ids) ? cursor.exclude_ids.length : 0
  if (excluded) return `исключено карточек: ${formatCount(excluded)}`
  if (typeof cursor.page === 'number') return `страница ${formatCount(cursor.page)}`
  if (typeof cursor.token === 'string' && cursor.token) return 'продолжение сохранено'
  return 'сохранена'
}

export function AttemptEvidencePanel({ operation, attempts, loading, error, onClose }: AttemptEvidencePanelProps) {
  if (!operation) return null

  return (
    <section className="factory-panel overflow-hidden" aria-label="Детали выполнения">
      <div className="table-head flex items-center justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-white">Детали выполнения</h2>
          <p className="mt-0.5 truncate font-mono text-xs text-zinc-500">{operation.operation_id}</p>
        </div>
        <IconButton title="Закрыть детали" onClick={onClose}>
          <X className="h-4 w-4" />
        </IconButton>
      </div>
      {loading && <div className="px-4 py-5 text-xs text-zinc-500">Загружаем сохранённые сведения о попытках…</div>}
      {error && <div className="border-t border-red-500/25 bg-red-500/10 px-4 py-3 text-xs text-red-200">{error}</div>}
      {!loading && !error && !attempts.length && (
        <div className="px-4 py-5 text-xs text-zinc-500">Для этого действия нет сохранённых попыток.</div>
      )}
      {!loading && !error && attempts.map((attempt) => (
        <article key={attempt.attempt_id} className="border-t border-surface-700/60 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs">
            <div className="font-mono text-zinc-200">Попытка {attempt.attempt_number} / {operationStateLabel(attempt.state)}</div>
            <div className="text-zinc-500">{formatTimestamp(attempt.started_at)} → {formatTimestamp(attempt.finished_at)}</div>
          </div>
          <dl className="mt-3 grid grid-cols-4 gap-x-4 gap-y-2 text-xs max-xl:grid-cols-2 max-sm:grid-cols-1">
            <div><dt className="mono-label">Получено / новых / повторов</dt><dd className="mt-1 text-zinc-300">{formatCount(attempt.received_count)} / {formatCount(attempt.new_unique_count)} / {formatCount(attempt.duplicate_count)}</dd></div>
            <div><dt className="mono-label">Ответ / размер / время</dt><dd className="mt-1 text-zinc-300">{attempt.response_status ?? '—'} / {attempt.response_bytes === null || attempt.response_bytes === undefined ? '—' : formatCount(attempt.response_bytes)} / {attempt.duration_ms === null || attempt.duration_ms === undefined ? '—' : `${formatCount(attempt.duration_ms)} мс`}</dd></div>
            <div><dt className="mono-label">Исполнитель / маршрут</dt><dd className="mt-1 break-all font-mono text-zinc-400">{attempt.worker_id ?? '—'} / {attempt.transport_id ?? 'не зафиксирован'}</dd></div>
            <div><dt className="mono-label">Отпечаток ответа</dt><dd className="mt-1 break-all font-mono text-zinc-400">{attempt.page_fingerprint ?? '—'}</dd></div>
          </dl>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-xs max-lg:grid-cols-1">
            <div><dt className="mono-label">Позиция до запроса</dt><dd className="mt-1 text-zinc-400">{cursorSummary(attempt.requested_cursor)}</dd></div>
            <div><dt className="mono-label">Позиция после ответа</dt><dd className="mt-1 text-zinc-400">{cursorSummary(attempt.reported_cursor)}</dd></div>
          </dl>
          <div className="mt-3 grid grid-cols-2 gap-3 text-xs max-lg:grid-cols-1">
            <div><div className="mono-label">Сохранённый ответ</div><div className="mt-1 break-all font-mono text-zinc-400">{attempt.raw_response_ref ? 'сохранён в артефактах задачи' : 'не сохранён'}</div></div>
            <div><div className="mono-label">Повтор / ошибка</div><div className="mt-1 break-words text-zinc-400">{[attempt.failure_kind, attempt.retry_after, attempt.error].filter(Boolean).join(' / ') || '—'}</div></div>
          </div>
        </article>
      ))}
    </section>
  )
}
