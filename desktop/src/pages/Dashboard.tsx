import { useState } from 'react'
import {
  AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Legend
} from 'recharts'
import { RefreshCw, TrendingUp, ChevronDown, ChevronUp, XCircle } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, TimelineItem, StatusRow, SkippedCandidate } from '../lib/api'
import { fmtTs, fmtNum, STATUS_LABEL } from '../lib/utils'
import { cn } from '../lib/utils'

const REASON_LABELS: Record<string, string> = {
  'keyword filter': 'ключевые слова',
  'honeypot': 'honeypot',
  'nlp spam': 'NLP спам',
  'nlp irrelevant': 'NLP нерелевант',
  'already sent': 'уже отправлен',
  'already queued': 'уже в очереди',
  'already auto_sent': 'уже авто-отправлен',
  'already manual_sent': 'уже отправлен вручную',
  'already draft': 'черновик',
  'already sending': 'отправляется',
  'already scored': 'оценён',
  'already vetted': 'проветтирован',
  'already auto_ready': 'auto_ready',
  'already filtered': 'отфильтрован',
  'already parsed': 'распарсен',
  'client blacklisted': 'ЧС клиента',
  'cycle auto-send limit reached': 'лимит откликов',
  'outside_work_hours': 'вне рабочих часов',
}

function reasonLabel(reason: string): string {
  if (!reason) return '—'
  const lower = reason.toLowerCase()
  for (const [key, label] of Object.entries(REASON_LABELS)) {
    if (lower.includes(key)) return label
  }
  if (lower.startsWith('score ')) return 'AI-скоринг'
  if (lower.startsWith('vet score')) return 'веттинг'
  return reason.slice(0, 60)
}

function reasonColor(reason: string): string {
  const lower = (reason || '').toLowerCase()
  if (lower.includes('blacklist')) return 'text-red-400'
  if (lower.includes('keyword') || lower.includes('honeypot')) return 'text-orange-400'
  if (lower.includes('nlp') || lower.includes('spam')) return 'text-yellow-400'
  if (lower.startsWith('score') || lower.includes('ai')) return 'text-blue-400'
  if (lower.includes('vet')) return 'text-purple-400'
  if (lower.includes('already')) return 'text-zinc-400'
  if (lower.includes('limit') || lower.includes('hours')) return 'text-amber-400'
  return 'text-zinc-400'
}

function SkippedPanel({ items }: { items: SkippedCandidate[] }) {
  const [expanded, setExpanded] = useState(true)
  const [reasonFilter, setReasonFilter] = useState<string>('')

  if (!items || items.length === 0) return null

  const byReason: Record<string, number> = {}
  for (const e of items) {
    const label = reasonLabel(e.decision_reason)
    byReason[label] = (byReason[label] || 0) + 1
  }

  const filtered = reasonFilter ? items.filter(e => reasonLabel(e.decision_reason) === reasonFilter) : items
  const reasons = Object.keys(byReason).sort((a, b) => byReason[b] - byReason[a])

  return (
    <div className="card">
      <button
        onClick={() => setExpanded(v => !v)}
        className="flex w-full items-center justify-between"
      >
        <div className="flex items-center gap-2">
          <XCircle className="h-4 w-4 text-zinc-500" />
          <h2 className="text-sm font-medium text-zinc-300">Пропущенные заказы</h2>
          <span className="badge border-white/10 bg-white/[0.03] text-stone-300">{items.length}</span>
        </div>
        {expanded ? <ChevronUp className="h-4 w-4 text-zinc-500" /> : <ChevronDown className="h-4 w-4 text-zinc-500" />}
      </button>

      {expanded && (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap gap-1.5">
            <button
              onClick={() => setReasonFilter('')}
              className={cn(
                'rounded-md border px-2 py-0.5 text-xs transition-colors',
                !reasonFilter ? 'border-brand-500/40 bg-brand-600/15 text-white' : 'border-surface-600 text-zinc-400 hover:text-white'
              )}
            >
              все ({items.length})
            </button>
            {reasons.map(r => (
              <button
                key={r}
                onClick={() => setReasonFilter(r)}
                className={cn(
                  'rounded-md border px-2 py-0.5 text-xs transition-colors',
                  reasonFilter === r ? 'border-brand-500/40 bg-brand-600/15 text-white' : 'border-surface-600 text-zinc-400 hover:text-white'
                )}
              >
                {r} ({byReason[r]})
              </button>
            ))}
          </div>

          <div className="max-h-80 overflow-y-auto space-y-1.5">
            {filtered.map((entry) => (
              <div
                key={entry.candidate_id}
                className="rounded-md border border-white/5 bg-black/20 px-3 py-2 text-xs"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className={cn('font-medium', reasonColor(entry.decision_reason))}>
                    {reasonLabel(entry.decision_reason)}
                  </span>
                  <div className="flex items-center gap-2 text-zinc-500">
                    {entry.ai_score != null && <span>AI: {entry.ai_score}</span>}
                    {entry.budget && <span>{entry.budget}₽</span>}
                  </div>
                </div>
                <div className="mt-0.5 text-zinc-300 truncate">{entry.title || '—'}</div>
                <div className="mt-0.5 text-zinc-500">
                  id={entry.project_id.slice(0, 16)} · {entry.platform}
                  {entry.search_query && ` · "${entry.search_query}"`}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

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

export default function Dashboard() {
  const [days, setDays] = useState(14)

  const { data: overview, loading: ovLoading, refetch: refetchOv } = useApi(
    () => api.getOverview(days), [days]
  )
  const { data: timeline } = useApi(() => api.getTimeline(days), [days])
  const { data: statusBreakdown } = useApi(() => api.getStatusBreakdown(days), [days])
  const { data: parseStats } = useApi(() => api.getParseStats(days), [days])
  const { data: activity } = useApi(() => api.getActivity(), [days])
  const { data: statusData } = useApi(() => api.getStatus(), [])
  const { data: skippedData, refetch: refetchSkipped } = useApi(() => (api as any).getSkipped(100), [])

  const chartData = timeline ? timelineToChartData(timeline) : []
  const actions = timeline
    ? [...new Set(timeline.map((t) => t.action))].filter((a) => a !== 'day')
    : []

  const skippedItems: SkippedCandidate[] = skippedData?.items ?? []
  const skippedCount = skippedItems.length

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

      {/* Skipped orders panel */}
      {skippedCount > 0 && <SkippedPanel items={skippedItems} />}

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
    </div>
  )
}
