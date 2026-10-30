import { useEffect, useRef, useState } from 'react'
import {
  AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Legend
} from 'recharts'
import { RefreshCw, TrendingUp, AlertTriangle, Bell } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, API_BASE, TimelineItem, StatusRow } from '../lib/api'
import { fmtTs, fmtNum, STATUS_LABEL } from '../lib/utils'
import { cn } from '../lib/utils'

function MetricCard({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  const display = typeof value === 'number' ? fmtNum(value) : String(value)
  return (
    <div className="card scanline flex flex-col gap-1 min-w-0">
      <span className="mono-label truncate">{label}</span>
      <span className="text-2xl font-semibold text-white">{display}</span>
      {sub && <span className="text-xs text-zinc-500">{sub}</span>}
    </div>
  )
}

function timelineToChartData(items: TimelineItem[]) {
  const byDay: Record<string, Record<string, number | string>> = {}
  for (const item of items) {
    if (!byDay[item.day]) byDay[item.day] = { day: item.day }
    byDay[item.day][item.action] = item.total
  }
  return Object.values(byDay).sort((a, b) => String(a.day).localeCompare(String(b.day)))
}

const STATUS_COLORS: Record<string, string> = {
  queued: '#facc15', auto_ready: '#34d399', auto_sent: '#10b981',
  manual_sent: '#6ee7b7', skipped: '#52525b', error: '#f87171',
  parsed: '#a8a29e', filtered: '#fb923c', scored: '#fdba74', vetted: '#fed7aa',
}

const ACTIVITY_LABELS: Record<string, string> = {
  parsed: 'последний парсинг',
  queued: 'последняя очередь',
  auto_sent: 'последняя авто-отправка',
  manual_sent: 'последняя ручная отправка',
  draft: 'последний черновик',
  responses: 'последний ответ',
}

interface PersistedCandidateRow {
  candidate_id: number
  project_id: string
  platform: string
  title: string
  status: string
  updated_at: string
}

interface CandidateDiscoverySnapshot {
  status: 'ready' | 'unavailable'
  reason: string | null
  latest_id: number
  next_after_id: number
  has_more: boolean
  total: number
  by_platform: Array<{ platform: string; total: number }>
  recent: PersistedCandidateRow[]
  items: PersistedCandidateRow[]
}

interface DurableCandidateEvent {
  seq: number
  candidate_id: number
  platform: string
  project_id: string
  event_type: string
  candidate_status: string | null
  delivery_status: string | null
  occurred_at: string
}

interface CandidateEventsResponse {
  status: 'ready' | 'unavailable' | 'reset_required'
  reason: string | null
  stream_id: string | null
  latest_seq: number
  next_after_seq: number
  has_more: boolean
  recent: DurableCandidateEvent[]
  items: DurableCandidateEvent[]
  outbox_status_counts: Record<string, number>
}

type CandidateCursor = { stream_id: string; after_seq: number }
const CANDIDATE_CURSOR_KEY = 'psr.dashboard.candidate-events.cursor.v1'

function readCandidateCursor(): CandidateCursor | null {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(CANDIDATE_CURSOR_KEY) || 'null')
    if (!saved || typeof saved !== 'object') return null
    if (!('stream_id' in saved) || typeof saved.stream_id !== 'string') return null
    if (!('after_seq' in saved) || typeof saved.after_seq !== 'number' ||
      !Number.isSafeInteger(saved.after_seq) || saved.after_seq < 0) return null
    return { stream_id: saved.stream_id, after_seq: saved.after_seq }
  } catch {
    return null
  }
}

function saveCandidateCursor(cursor: CandidateCursor | null) {
  try {
    if (cursor) localStorage.setItem(CANDIDATE_CURSOR_KEY, JSON.stringify(cursor))
    else localStorage.removeItem(CANDIDATE_CURSOR_KEY)
  } catch {
    // REST replay continues for the active window even when storage is disabled.
  }
}

function isDurableCandidateEvent(value: unknown): value is DurableCandidateEvent {
  if (!value || typeof value !== 'object') return false
  return 'seq' in value && typeof value.seq === 'number' && Number.isSafeInteger(value.seq) &&
    'candidate_id' in value && typeof value.candidate_id === 'number' &&
    'platform' in value && typeof value.platform === 'string' &&
    'project_id' in value && typeof value.project_id === 'string' &&
    'event_type' in value && typeof value.event_type === 'string' &&
    'candidate_status' in value && (value.candidate_status === null || typeof value.candidate_status === 'string') &&
    'delivery_status' in value && (value.delivery_status === null || typeof value.delivery_status === 'string') &&
    'occurred_at' in value && typeof value.occurred_at === 'string'
}

function isCandidateEventsResponse(value: unknown): value is CandidateEventsResponse {
  if (!value || typeof value !== 'object') return false
  return 'status' in value && ['ready', 'unavailable', 'reset_required'].includes(String(value.status)) &&
    'stream_id' in value && (value.stream_id === null || typeof value.stream_id === 'string') &&
    'next_after_seq' in value && typeof value.next_after_seq === 'number' &&
    'latest_seq' in value && typeof value.latest_seq === 'number' &&
    'has_more' in value && typeof value.has_more === 'boolean' &&
    'items' in value && Array.isArray(value.items) && value.items.every(isDurableCandidateEvent) &&
    'recent' in value && Array.isArray(value.recent) && value.recent.every(isDurableCandidateEvent) &&
    'outbox_status_counts' in value && typeof value.outbox_status_counts === 'object' &&
    value.outbox_status_counts !== null && !Array.isArray(value.outbox_status_counts)
}

function candidateEventLabel(event: DurableCandidateEvent): string {
  const labels: Record<string, string> = {
    'candidate.discovered': 'Обнаружено и сохранено (без оценки)',
    'candidate.status_changed': 'Обновлён статус кандидата',
    'notification.pending': 'Уведомление ожидает отправки',
    'notification.leased': 'Уведомление обрабатывается',
    'notification.retry': 'Запланирована повторная попытка уведомления',
    'notification.provider_accepted': 'Провайдер принял отправку',
    'notification.failed': 'Доставка не подтверждена',
    'notification.dead': 'Попытки доставки исчерпаны',
    'notification.cancelled': 'Уведомление отменено',
  }
  return labels[event.event_type] || event.event_type
}

const DELIVERY_LABELS: Record<string, string> = {
  pending: 'Ожидают отправки',
  leased: 'Обрабатываются',
  delivered: 'Приняты провайдером',
  dead: 'Попытки исчерпаны',
  cancelled: 'Отменены',
}

function isDiscoverySnapshot(value: unknown): value is CandidateDiscoverySnapshot {
  return typeof value === 'object' && value !== null &&
    'status' in value && (value.status === 'ready' || value.status === 'unavailable') &&
    'next_after_id' in value && typeof value.next_after_id === 'number' &&
    'latest_id' in value && typeof value.latest_id === 'number' &&
    'total' in value && typeof value.total === 'number' &&
    'by_platform' in value && Array.isArray(value.by_platform) &&
    'recent' in value && Array.isArray(value.recent) &&
    'items' in value && Array.isArray(value.items) &&
    'has_more' in value && typeof value.has_more === 'boolean'
}

export default function Dashboard() {
  const [days, setDays] = useState(14)
  const [candidateEvents, setCandidateEvents] = useState<CandidateEventsResponse | null>(null)
  const [candidateEventError, setCandidateEventError] = useState<string | null>(null)
  const [candidateEventsChecked, setCandidateEventsChecked] = useState<number | null>(null)
  const [candidateEventRows, setCandidateEventRows] = useState<DurableCandidateEvent[]>([])
  const [candidateEventResetReason, setCandidateEventResetReason] = useState<string | null>(null)
  const candidateEventsCursor = useRef<CandidateCursor | null>(null)
  const candidateReplayPausedRef = useRef(false)
  const pollCandidateEventsNowRef = useRef<() => void>(() => {})
  const resetCandidateEventsRef = useRef<() => void>(() => {})
  const [candidateFeed, setCandidateFeed] = useState<CandidateDiscoverySnapshot | null>(null)
  const [candidateFeedError, setCandidateFeedError] = useState<string | null>(null)
  const [candidateFeedChecked, setCandidateFeedChecked] = useState<number | null>(null)
  const [newCandidateRows, setNewCandidateRows] = useState<PersistedCandidateRow[]>([])
  const [newCandidateTotal, setNewCandidateTotal] = useState(0)
  const discoveryCursor = useRef<number | null>(null)
  const pollNowRef = useRef<() => void>(() => {})

  useEffect(() => {
    let active = true
    let inFlight = false
    candidateEventsCursor.current = readCandidateCursor()
    const controller = new AbortController()
    const check = async () => {
      if (!active || inFlight || candidateReplayPausedRef.current) return
      inFlight = true
      try {
        for (let page = 0; page < 5 && active; page += 1) {
          const cursor = candidateEventsCursor.current
          const params = new URLSearchParams({ limit: '200' })
          if (cursor) {
            params.set('stream_id', cursor.stream_id)
            params.set('after_seq', String(cursor.after_seq))
          }
          const response = await fetch(`${API_BASE}/api/dashboard/candidate-events?${params}`, { signal: controller.signal })
          if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText || 'Ошибка API'}`)
          const data: unknown = await response.json()
          if (!isCandidateEventsResponse(data)) throw new Error('Некорректная структура журнала событий')
          if (!active) return
          setCandidateEvents(data)
          setCandidateEventsChecked(Date.now())
          setCandidateEventError(null)
          if (data.status === 'unavailable') break
          if (data.status === 'reset_required') {
            candidateReplayPausedRef.current = true
            setCandidateEventResetReason(data.reason || 'Нарушена последовательность журнала событий')
            break
          }
          if (!data.stream_id) throw new Error('Отсутствует идентификатор журнала событий')
          if (cursor && data.stream_id !== cursor.stream_id) {
            candidateReplayPausedRef.current = true
            setCandidateEventResetReason('Файл базы заменён. Для воспроизведения событий требуется новая точка отсчёта.')
            break
          }
          if (cursor && data.items.length) {
            setCandidateEventRows((existing) => [
              ...data.items.slice().reverse(),
              ...existing,
            ].slice(0, 30))
          } else if (!cursor) {
            // Historical snapshot is visible, but never counted as newly
            // discovered or newly delivered notifications.
            setCandidateEventRows(data.recent.slice().reverse())
          } else if (data.recent.length) {
            setCandidateEventRows((existing) => existing.length ? existing : data.recent.slice().reverse())
          }
          const next = { stream_id: data.stream_id, after_seq: data.next_after_seq }
          candidateEventsCursor.current = next
          saveCandidateCursor(next)
          if (!data.has_more || next.after_seq === cursor?.after_seq) break
        }
      } catch (reason) {
        if (active && !controller.signal.aborted) {
          setCandidateEventError(reason instanceof Error ? reason.message : String(reason))
        }
      } finally {
        inFlight = false
      }
    }
    pollCandidateEventsNowRef.current = () => void check()
    resetCandidateEventsRef.current = () => {
      candidateEventsCursor.current = null
      candidateReplayPausedRef.current = false
      saveCandidateCursor(null)
      setCandidateEventResetReason(null)
      setCandidateEventRows([])
      // A subsequent check will establish a new baseline. Explicitly
      // acknowledging a DB reset avoids silently interpreting old IDs.
      void check()
    }
    void check()
    const timer = window.setInterval(() => void check(), 10_000)
    const onFocus = () => void check()
    window.addEventListener('focus', onFocus)
    return () => {
      active = false
      controller.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      pollCandidateEventsNowRef.current = () => {}
      resetCandidateEventsRef.current = () => {}
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let inFlight = false
    const poll = async () => {
      if (inFlight || controller.signal.aborted) return
      inFlight = true
      try {
        // Cursor is candidate_id, not updated_at or remote project creation
        // time; reprocessing an existing candidate must not count as a new one.
        for (let page = 0; page < 5; page += 1) {
          const currentCursor = discoveryCursor.current
          const params = new URLSearchParams({ limit: '200' })
          if (currentCursor !== null) params.set('after_id', String(currentCursor))
          const response = await fetch(`${API_BASE}/api/dashboard/candidate-discovery?${params}`, { signal: controller.signal })
          if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText || 'Ошибка API'}`)
          const payload: unknown = await response.json()
          if (!isDiscoverySnapshot(payload)) throw new Error('Неожиданный формат ответа наблюдения кандидатов')
          if (controller.signal.aborted) return
          setCandidateFeed(payload)
          setCandidateFeedChecked(Date.now())
          setCandidateFeedError(null)
          if (payload.status !== 'ready') return
          if (currentCursor !== null && payload.items.length) {
            setNewCandidateTotal((value) => value + payload.items.length)
            setNewCandidateRows((existing) => [...payload.items.slice().reverse(), ...existing].slice(0, 8))
          }
          discoveryCursor.current = payload.next_after_id
          if (!payload.has_more || payload.next_after_id === currentCursor) break
        }
      } catch (reason) {
        if (!controller.signal.aborted) setCandidateFeedError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        inFlight = false
      }
    }
    pollNowRef.current = () => { void poll() }
    void poll()
    const timer = window.setInterval(() => void poll(), 15_000)
    const onFocus = () => void poll()
    window.addEventListener('focus', onFocus)
    return () => {
      controller.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      pollNowRef.current = () => {}
    }
  }, [])

  const { data: overview, loading: ovLoading, refetch: refetchOv } = useApi(
    () => api.getOverview(days), [days]
  )
  const { data: timeline } = useApi(() => api.getTimeline(days), [days])
  const { data: statusBreakdown } = useApi(() => api.getStatusBreakdown(days), [days])
  const { data: parseStats } = useApi(() => api.getParseStats(days), [days])
  const { data: activity } = useApi(() => api.getActivity(), [days])
  const { data: statusData } = useApi(() => api.getStatus(), [])
  const { data: funnelData } = useApi(() => api.getFunnel(days), [days])
  const { data: queryMemory } = useApi(() => api.getQueryMemory(10), [days])
  const { data: breakerData } = useApi(() => api.getBreaker(), [])
  const { data: alertsData } = useApi(() => api.getAlerts(), [])

  const chartData = timeline ? timelineToChartData(timeline) : []
  const actions = timeline
    ? [...new Set(timeline.map((t) => t.action))].filter((a) => a !== 'day')
    : []

  const skippedCount: number = statusData?.last_cycle_stats?.skipped ?? 0

  return (
    <div className="space-y-6 p-6 max-lg:p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="page-kicker">обзор</div>
          <h1 className="text-xl font-semibold text-white">Панель</h1>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="input w-28 py-1.5"
          >
            {[7, 14, 30, 60].map((d) => (
              <option key={d} value={d}>{d} дней</option>
            ))}
          </select>
          <button onClick={refetchOv} className="btn btn-ghost py-1.5">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Alerts banner */}
      {(alertsData?.count ?? 0) > 0 && alertsData?.alerts && (
        <div className="space-y-1.5">
          {alertsData.alerts.map((alert, i) => (
            <div
              key={i}
              className={cn(
                'flex items-center gap-2 rounded-md border px-3 py-2 text-xs',
                alert.severity === 'critical'
                  ? 'border-red-500/30 bg-red-500/10 text-red-300'
                  : alert.severity === 'warning'
                    ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                    : 'border-blue-500/30 bg-blue-500/10 text-blue-300'
              )}
            >
              {alert.severity === 'critical' ? (
                <AlertTriangle className="h-4 w-4 shrink-0" />
              ) : alert.type === 'pending_review' ? (
                <Bell className="h-4 w-4 shrink-0" />
              ) : (
                <AlertTriangle className="h-4 w-4 shrink-0" />
              )}
              <span className="font-medium">{alert.title}</span>
              <span className="text-zinc-500">·</span>
              <span>{alert.detail}</span>
            </div>
          ))}
        </div>
      )}

      <section className="card" aria-label="Журнал кандидатов и уведомлений">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-medium text-zinc-200">Журнал состояний кандидатов и доставки</h2>
            <p className="mt-1 text-xs text-zinc-500">Сохранённые события Kwork / Freelance.ru / HH.ru; независимо от Buyer Search. Это REST-воспроизведение, не push-уведомления.</p>
          </div>
          <button type="button" className="btn btn-ghost py-1 text-xs" onClick={() => pollCandidateEventsNowRef.current()} disabled={Boolean(candidateEventResetReason)}>
            <RefreshCw className="h-3.5 w-3.5" /> Проверить журнал
          </button>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-zinc-400">
          <span>{candidateEventResetReason ? 'Нужна новая точка отсчёта'
            : candidateEvents?.status === 'ready' ? 'Журнал доступен'
            : candidateEvents?.status === 'unavailable' ? 'Журнал пока недоступен'
              : candidateEvents?.status === 'reset_required' ? 'Нужна новая точка отсчёта'
                : 'Подключение…'}</span>
          <span>Последний seq: {candidateEvents?.latest_seq ?? '—'}</span>
          <span>Проверено: {candidateEventsChecked ? new Date(candidateEventsChecked).toLocaleTimeString('ru-RU') : '—'}</span>
          <span>REST-опрос каждые 10 с — не задержка обнаружения</span>
          {candidateEvents?.has_more && <span className="text-amber-300">Есть ещё сохранённые события для воспроизведения</span>}
        </div>
        {candidateEvents?.status === 'ready' && (
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-zinc-300">
            {Object.entries(DELIVERY_LABELS).map(([key, label]) => <span key={key}>
              {label}: <strong>{candidateEvents.outbox_status_counts[key] ?? 0}</strong>
            </span>)}
          </div>
        )}
        {candidateEvents?.status === 'unavailable' && <p className="mt-2 text-xs text-amber-300">{candidateEvents.reason || 'Схема outbox ещё не создана'}. Данные не заменяются выдуманными счётчиками.</p>}
        {candidateEventError && <p className="mt-2 text-xs text-red-300" role="alert">
          Ошибка чтения событий: {candidateEventError}. Проверьте локальный FastAPI.
        </p>}
        {candidateEventResetReason && <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-amber-300" role="alert">
          <span>{candidateEventResetReason} История до сброса не подтверждена.</span>
          <button type="button" className="btn btn-ghost py-1 text-xs" onClick={() => resetCandidateEventsRef.current()}>
            Подтвердить сброс и начать с текущих событий
          </button>
        </div>}
        <div className="mt-3 space-y-1">
          {candidateEventRows.length ? candidateEventRows.map((event) => (
            <div key={event.seq} className="flex flex-wrap items-center justify-between gap-2 border-t border-white/5 py-1.5 text-xs">
              <span className="min-w-0 text-zinc-300">
                #{event.seq} · {event.platform} · кандидат #{event.candidate_id} · {candidateEventLabel(event)}
              </span>
              <span className="text-zinc-500">
                {event.candidate_status ? `Статус: ${STATUS_LABEL[event.candidate_status] ?? event.candidate_status}` : ''}
                {event.delivery_status ? ` · доставка: ${event.delivery_status === 'provider_accepted' ? 'принято провайдером' : event.delivery_status}` : ''}
                {' · '}{event.occurred_at ? fmtTs(event.occurred_at) : 'время не указано'}
              </span>
            </div>
          )) : <p className="text-xs text-zinc-500">
            {candidateEvents?.status === 'ready'
              ? 'Событий пока нет. Это не означает, что сервисы обнаружения выключены.'
              : 'Нет подтверждённых событий для отображения.'}
          </p>}
        </div>
        <p className="mt-2 text-xs text-zinc-500">
          «parsed» — лишь первичное сохранение, не подтверждение релевантности.
          «Принято провайдером» не означает, что получатель прочитал сообщение.
          Исторические строки при первой загрузке не считаются новыми уведомлениями.
        </p>
      </section>

      {/* Read-only observations of orchestrator DB insertions. This is not a
          push notification, a buyer-search event, or end-to-end latency. */}
      <section className="card" aria-label="Новые сохранённые кандидаты">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-medium text-zinc-200">Кандидаты оркестратора · сохранённые записи</h2>
            <p className="mt-1 text-xs text-zinc-500">Kwork / Freelance.ru / HH.ru — только то, что уже записано в общую БД кандидатов. Buyer Search хранится отдельно.</p>
          </div>
          <button className="btn btn-ghost py-1 text-xs" type="button" onClick={() => pollNowRef.current()}><RefreshCw className="h-3.5 w-3.5" />Проверить БД</button>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs">
          <span className="text-zinc-300">Состояние: {candidateFeed?.status === 'ready' ? 'SQLite доступна' : candidateFeed?.status === 'unavailable' ? 'База недоступна' : 'Ожидание первой проверки'}</span>
          {candidateFeed?.status === 'ready' && <span className="text-zinc-300">Всего записей: {candidateFeed.total}</span>}
          <span className="text-zinc-300">Новых ID с открытия экрана: {newCandidateTotal}</span>
          <span className="text-zinc-500">Последняя проверка: {candidateFeedChecked ? new Date(candidateFeedChecked).toLocaleTimeString('ru-RU') : '—'}</span>
          <span className="text-zinc-500">Опрос раз в 15 с; это не задержка обнаружения на площадке</span>
        </div>
        {candidateFeed?.status === 'ready' && <p className="mt-2 text-xs text-zinc-400">Записано по платформам (за всё время): {candidateFeed.by_platform.length ? candidateFeed.by_platform.map((row) => `${row.platform}: ${row.total}`).join(' · ') : 'пока нет кандидатов'}</p>}
        {candidateFeed?.status === 'unavailable' && <p role="status" className="mt-2 text-xs text-amber-300">{candidateFeed.reason || 'База кандидатов недоступна'}.</p>}
        {candidateFeedError && <p role="alert" className="mt-2 text-xs text-red-300">Не удалось проверить сохранённые кандидаты: {candidateFeedError}. Проверьте FastAPI и повторите запрос.</p>}
        {newCandidateRows.length > 0 ? (
          <div className="mt-3 space-y-1">
            <p className="text-xs text-emerald-300">В этой сессии появились новые записи (не означает готовность к отклику или доставку уведомлений):</p>
            {newCandidateRows.map((row) => <div key={row.candidate_id} className="flex flex-wrap justify-between gap-2 border-t border-white/5 pt-1.5 text-xs">
              <span className="min-w-0 text-zinc-300">#{row.candidate_id} · {row.platform} · {row.title || row.project_id}</span>
              <span className="text-zinc-500">{STATUS_LABEL[row.status] ?? row.status}</span>
            </div>)}
          </div>
        ) : candidateFeed?.status === 'ready' && (
          <div className="mt-3 text-xs text-zinc-500">
            Новых ID после открытия экрана пока нет. Последние сохранённые: {candidateFeed.recent.length
              ? candidateFeed.recent.map((row) => `${row.platform} #${row.candidate_id}`).join(' · ')
              : 'записей нет'}.
          </div>
        )}
      </section>

      {/* Metric cards */}
      <div className="grid grid-cols-7 gap-3 max-2xl:grid-cols-4 max-lg:grid-cols-2">
        <MetricCard label="Спарсено" value={overview?.parsed ?? 0} />
        <MetricCard label="В очереди" value={overview?.queued ?? 0} />
        <MetricCard label="Авто-отправлено" value={overview?.auto_sent ?? 0} />
        <MetricCard label="Ручная отправка" value={overview?.manual_sent ?? 0} />
        <MetricCard label="Черновики" value={overview?.draft ?? 0} />
        <MetricCard label="Ответы" value={overview?.responses ?? 0} />
        <MetricCard label="Пропущено" value={skippedCount} sub="за последний цикл" />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-3 gap-4 max-xl:grid-cols-1">
        {/* Timeline */}
        <div className="card col-span-2 max-xl:col-span-1">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> Таймлайн решений
          </h2>
          {chartData.length === 0 ? (
            <p className="text-zinc-500 text-sm text-center py-8">Нет данных</p>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                <XAxis dataKey="day" tick={{ fill: '#71717a', fontSize: 11 }} />
                <YAxis tick={{ fill: '#71717a', fontSize: 11 }} />
                <Tooltip
                  contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8 }}
                  labelStyle={{ color: '#e4e4e7' }}
                />
                <Legend wrapperStyle={{ fontSize: 11, color: '#a1a1aa' }} />
                {actions.slice(0, 6).map((action) => (
                  <Area
                    key={action}
                    type="monotone"
                    dataKey={action}
                    stackId="1"
                    name={STATUS_LABEL[action] ?? action}
                    stroke={STATUS_COLORS[action] ?? '#fb923c'}
                    fill={STATUS_COLORS[action] ?? '#fb923c'}
                    fillOpacity={0.3}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Status breakdown */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4">Статусы</h2>
          {!statusBreakdown || statusBreakdown.length === 0 ? (
            <p className="text-zinc-500 text-sm text-center py-8">Нет данных</p>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={statusBreakdown} layout="vertical">
                <XAxis type="number" tick={{ fill: '#71717a', fontSize: 11 }} />
                <YAxis
                  dataKey="status"
                  type="category"
                  tick={{ fill: '#a8a29e', fontSize: 11 }}
                  tickFormatter={(value) => STATUS_LABEL[String(value)] ?? String(value)}
                  width={88}
                />
                <Tooltip
                  contentStyle={{ background: '#18181b', border: '1px solid #3f3f46', borderRadius: 8 }}
                  labelStyle={{ color: '#e4e4e7' }}
                />
                <Bar dataKey="total" radius={[0, 4, 4, 0]}>
                  {statusBreakdown.map((row: StatusRow) => (
                    <Cell key={row.status} fill={STATUS_COLORS[row.status] ?? '#fb923c'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      {/* Parse stats + Activity */}
      <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
        {/* Parse stats */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-3">Парсинг по платформам</h2>
          {!parseStats || parseStats.length === 0 ? (
            <p className="text-zinc-500 text-sm">Нет данных</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-zinc-500 border-b border-surface-700">
                  <th className="text-left pb-2">Платформа</th>
                  <th className="text-right pb-2">Запуски</th>
                  <th className="text-right pb-2">OK</th>
                  <th className="text-right pb-2">Ошибки</th>
                  <th className="text-right pb-2">Проекты</th>
                  <th className="text-right pb-2">Средн. мс</th>
                </tr>
              </thead>
              <tbody>
                {parseStats.map((row) => (
                  <tr key={row.platform} className="border-b border-surface-700/50 text-zinc-300">
                    <td className="py-1.5 font-medium">{row.platform}</td>
                    <td className="text-right">{row.runs}</td>
                    <td className="text-right text-emerald-400">{row.ok}</td>
                    <td className="text-right text-red-400">{row.err}</td>
                    <td className="text-right">{row.projects}</td>
                    <td className="text-right text-zinc-500">{row.avg_ms}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Activity */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-3">Последняя активность</h2>
          <div className="space-y-2">
            {activity && Object.entries(activity).map(([key, val]) => (
              <div key={key} className="flex items-center justify-between text-xs">
                <span className="text-zinc-500">{ACTIVITY_LABELS[key.replace('last_', '')] ?? key.replace('last_', '').replace('_', ' ')}</span>
                <span className="text-zinc-300">{fmtTs(val)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Funnel + Query Memory + Breaker */}
      <div className="grid grid-cols-3 gap-4 max-xl:grid-cols-1">
        {/* Conversion Funnel */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-3">Конверсия</h2>
          {funnelData && (funnelData as any).sent > 0 ? (
            <div className="space-y-2">
              {[
                ['Отправлено', (funnelData as any).sent, 100],
                ['Ответы', (funnelData as any).responded, (funnelData as any).response_rate],
                ['Наняли', (funnelData as any).hired, (funnelData as any).hire_rate],
                ['Завершено', (funnelData as any).completed, (funnelData as any).completion_rate],
                ['Оплачено', (funnelData as any).paid, (funnelData as any).payment_rate],
              ].map(([label, val, rate]) => (
                <div key={label as string} className="flex items-center justify-between text-xs">
                  <span className="text-zinc-400">{label}</span>
                  <div className="flex items-center gap-2">
                    <span className="text-zinc-200 font-medium">{val}</span>
                    <span className="text-zinc-600 w-12 text-right">{(rate as number).toFixed(0)}%</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-zinc-500 text-sm text-center py-6">Нет данных</p>
          )}
        </div>

        {/* Query Memory */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-3">Эффективность запросов</h2>
          {queryMemory && queryMemory.length > 0 ? (
            <div className="space-y-1.5 max-h-48 overflow-y-auto">
              {queryMemory.map((q, i) => (
                <div key={i} className="flex items-center justify-between text-xs">
                  <span className="text-zinc-400 truncate max-w-32">{q.query_text}</span>
                  <div className="flex items-center gap-2 text-zinc-500">
                    <span title="shortlisted">{q.shortlisted_count}</span>
                    <span className="text-emerald-400" title="sent">{q.sent_count}</span>
                    <span className="text-blue-400" title="responded">{q.responded_count}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-zinc-500 text-sm text-center py-6">Нет данных</p>
          )}
        </div>

        {/* Circuit Breaker */}
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-3">Circuit Breaker</h2>
          {breakerData && breakerData.length > 0 ? (
            <div className="space-y-2">
              {breakerData.map((b) => (
                <div key={b.key} className="flex items-center justify-between text-xs">
                  <div>
                    <span className="text-zinc-300">{b.key}</span>
                    {b.consecutive_failures > 0 && (
                      <span className="text-red-400 ml-2">{b.consecutive_failures} ошибок</span>
                    )}
                  </div>
                  <span className={
                    b.state === 'CLOSED' ? 'text-emerald-400' :
                    b.state === 'OPEN' ? 'text-red-400' :
                    'text-amber-400'
                  }>
                    {b.state}
                    {b.paused_seconds_left > 0 && ` (${b.paused_seconds_left}s)`}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-zinc-500 text-sm text-center py-6">Нет данных</p>
          )}
        </div>
      </div>
    </div>
  )
}
