import type { ComponentProps, ReactNode } from 'react'
import { Circle } from 'lucide-react'

import { cn } from '../../../lib/utils'
import type {
  MarketJobPhase,
  MarketJobState,
  MarketOperation,
  MarketOperationState,
  MarketStreamState,
  MarketTransportHealth,
  MarketWorkerDesiredState,
  MarketWorkerState,
} from '../types'

const STATE_COLORS: Record<MarketJobState, string> = {
  preparing: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  mapping: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  planning: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300',
  running: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  pausing: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
  paused: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
  completing: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  enriching: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  analyzing: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300',
  finalizing: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  completed: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  stopping: 'border-red-500/30 bg-red-500/10 text-red-300',
  stopped: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  blocked: 'border-red-500/30 bg-red-500/10 text-red-300',
  failed: 'border-red-500/30 bg-red-500/10 text-red-300',
}

const STREAM_COLORS: Record<MarketStreamState, string> = {
  connecting: 'text-amber-300',
  live: 'text-emerald-300',
  disconnected: 'text-zinc-400',
  resyncing: 'text-sky-300',
  stale: 'text-red-300',
}

const STATE_LABELS: Record<MarketJobState, string> = {
  preparing: 'подготовка',
  mapping: 'настройка категорий',
  planning: 'планирование',
  running: 'сбор',
  pausing: 'приостановка',
  paused: 'приостановлена',
  completing: 'завершение сбора',
  enriching: 'обогащение',
  analyzing: 'анализ',
  finalizing: 'подведение итогов',
  completed: 'завершена',
  stopping: 'остановка',
  stopped: 'остановлена',
  blocked: 'заблокирована',
  failed: 'ошибка',
}

const STREAM_LABELS: Record<MarketStreamState, string> = {
  connecting: 'подключение',
  live: 'на связи',
  disconnected: 'нет связи',
  resyncing: 'синхронизация',
  stale: 'устарело',
}

const PHASE_LABELS: Record<MarketJobPhase, string> = {
  prepare: 'подготовка',
  map: 'настройка области',
  plan: 'планирование',
  collect: 'сбор',
  enrich: 'обогащение',
  analyze: 'анализ',
  export: 'сохранение результата',
}

const OPERATION_KIND_LABELS: Record<MarketOperation['kind'], string> = {
  map_scope: 'Подготовка области',
  resolve_alias: 'Проверка рубрики',
  fetch_batch: 'Загрузка порции карточек',
  enrich_listing: 'Обогащение карточек',
  analyze_snapshot: 'Подготовка анализа',
  export_snapshot: 'Сохранение результата',
}

const OPERATION_STATE_LABELS: Record<MarketOperationState, string> = {
  queued: 'в очереди',
  leased: 'назначено',
  running: 'выполняется',
  succeeded: 'выполнено',
  retry_wait: 'ожидает повтора',
  failed: 'ошибка',
  cancelled: 'отменено',
  contract_violation: 'ответ не прошёл проверку',
  blocked: 'заблокировано',
}

const WORKER_STATE_LABELS: Record<MarketWorkerState, string> = {
  starting: 'запускается',
  connecting: 'подключается',
  idle: 'ожидает работу',
  leasing: 'получает задачу',
  busy: 'работает',
  cooldown: 'пауза',
  backoff: 'повтор позже',
  blocked: 'заблокирован',
  draining: 'завершает текущую задачу',
  stopped: 'остановлен',
  crashed: 'аварийно завершён',
}

const WORKER_DESIRED_STATE_LABELS: Record<MarketWorkerDesiredState, string> = {
  running: 'работать',
  draining: 'завершить',
  disabled: 'выключен',
  stopped: 'остановлен',
}

const TRANSPORT_HEALTH_LABELS: Record<MarketTransportHealth, string> = {
  unknown: 'не проверен',
  starting: 'запускается',
  healthy: 'исправен',
  degraded: 'нестабилен',
  quarantined: 'в изоляции',
  stopped: 'остановлен',
}

export function formatCount(value: unknown): string {
  const number = typeof value === 'number' ? value : Number(value ?? 0)
  return Number.isFinite(number) ? new Intl.NumberFormat('ru-RU').format(number) : '0'
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '-'
  const timestamp = Date.parse(value)
  if (Number.isNaN(timestamp)) return value
  return new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    day: '2-digit',
    month: 'short',
  }).format(timestamp)
}

export function formatTransportProxyRoute(value: string | null | undefined): string {
  const route = value?.trim()
  if (!route) return 'прямое подключение'
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(route)) {
    try {
      const parsed = new URL(route)
      return `${parsed.protocol}//${parsed.hostname}${parsed.port ? `:${parsed.port}` : ''}`
    } catch {
      // The fallback below intentionally removes userinfo and query fragments.
    }
  }
  return route.replace(/^.*@/, '').split(/[?#]/, 1)[0] || 'прямое подключение'
}

export function phaseLabel(value: MarketJobPhase): string {
  return PHASE_LABELS[value]
}

export function operationKindLabel(value: MarketOperation['kind']): string {
  return OPERATION_KIND_LABELS[value]
}

export function operationStateLabel(value: MarketOperationState | string): string {
  return OPERATION_STATE_LABELS[value as MarketOperationState] ?? value
}

export function operationLeaseLabel(operation: MarketOperation): string {
  if (operation.lease_owner) return operation.lease_owner
  if (['succeeded', 'failed', 'cancelled', 'blocked', 'contract_violation'].includes(operation.state)) return 'освобождено'
  if (operation.state === 'queued' || operation.state === 'retry_wait') return 'ожидает исполнителя'
  return formatTimestamp(operation.lease_deadline)
}

export function workerStateLabel(value: MarketWorkerState): string {
  return WORKER_STATE_LABELS[value]
}

export function workerDesiredStateLabel(value: MarketWorkerDesiredState): string {
  return WORKER_DESIRED_STATE_LABELS[value]
}

export function transportHealthLabel(value: MarketTransportHealth): string {
  return TRANSPORT_HEALTH_LABELS[value]
}

export function MarketStateBadge({ state }: { state: MarketJobState }) {
  return <span className={cn('badge whitespace-nowrap', STATE_COLORS[state])}>{STATE_LABELS[state]}</span>
}

export function StreamState({ state }: { state: MarketStreamState }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-xs', STREAM_COLORS[state])}>
      <Circle className="h-2.5 w-2.5 fill-current" />
      {STREAM_LABELS[state]}
    </span>
  )
}

export function IconButton({
  title,
  children,
  className,
  ...props
}: Omit<ComponentProps<'button'>, 'children'> & { title: string; children: ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      className={cn('btn btn-ghost h-8 w-8 justify-center px-0', className)}
      {...props}
    >
      {children}
    </button>
  )
}
