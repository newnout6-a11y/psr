import type { MarketJobEvent } from '../types'
import { formatCount, formatTimestamp } from './shared'

const HIDDEN_EVENT_TYPES = new Set(['worker.state_changed', 'worker.heartbeat', 'worker.registered'])

function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

function eventTitle(event: MarketJobEvent): string {
  const payload = event.payload
  switch (event.type) {
    case 'job.state_changed':
      return 'Состояние запуска изменено'
    case 'job.phase_changed':
      return 'Этап выполнения изменён'
    case 'job.metrics':
      return 'Показатели сбора обновлены'
    case 'operation.queued':
      return 'Действие добавлено в очередь'
    case 'operation.started':
      return 'Действие запущено'
    case 'operation.completed':
      return 'Действие выполнено'
    case 'operation.failed':
      return 'Действие завершилось ошибкой'
    case 'operation.contract_violation':
      return 'Ответ не прошёл проверку источника'
    case 'shard.progress':
      return `Сегмент: +${formatCount(numberValue(payload.new_unique))} уникальных, ${formatCount(numberValue(payload.duplicate_count))} повторов`
    case 'transport.state_changed':
      return 'Состояние маршрута изменено'
    case 'checkpoint.saved':
      return 'Сохранён итоговый срез'
    case 'result.ready':
      return 'Результат готов'
    case 'warning':
      return 'Предупреждение запуска'
    default:
      return event.type
  }
}

function eventDetail(event: MarketJobEvent): string | null {
  const payload = event.payload
  if (event.type === 'job.state_changed' && typeof payload.state === 'string') return String(payload.state)
  if (event.type === 'warning' && typeof payload.code === 'string') return String(payload.code)
  if (typeof payload.message === 'string') return payload.message
  return null
}

export function EventTimeline({ events }: { events: MarketJobEvent[] }) {
  const visibleEvents = events.filter((event) => !HIDDEN_EVENT_TYPES.has(event.type)).slice(-100)
  return (
    <section className="factory-panel overflow-hidden">
      <div className="table-head flex items-center justify-between px-4 py-3"><h2 className="text-sm font-medium text-white">Ход запуска</h2><span className="mono-label">{visibleEvents.length}</span></div>
      <ol className="max-h-80 divide-y divide-surface-700/60 overflow-y-auto">{[...visibleEvents].reverse().map((event) => <li key={event.seq} className="grid grid-cols-[64px_1fr] gap-3 px-4 py-2 text-xs"><span className="font-mono text-zinc-600">#{event.seq}</span><div className="min-w-0"><div className="flex items-center justify-between gap-3"><span className="text-zinc-300">{eventTitle(event)}</span><time className="shrink-0 text-zinc-600">{formatTimestamp(event.emitted_at)}</time></div>{eventDetail(event) && <p className="mt-1 truncate font-mono text-zinc-500">{eventDetail(event)}</p>}</div></li>)}{!visibleEvents.length && <li className="px-4 py-8 text-center text-xs text-zinc-500">События появятся здесь. Состояние исполнителей отображается в таблице выше.</li>}</ol>
    </section>
  )
}
