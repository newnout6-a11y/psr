import { useState } from 'react'
import {
  AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Legend
} from 'recharts'
import { RefreshCw, TrendingUp } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, TimelineItem, StatusRow } from '../lib/api'
import { fmtTs, fmtNum, STATUS_LABEL } from '../lib/utils'

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

  const chartData = timeline ? timelineToChartData(timeline) : []
  const actions = timeline
    ? [...new Set(timeline.map((t) => t.action))].filter((a) => a !== 'day')
    : []

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
      <div className="grid grid-cols-6 gap-3 max-2xl:grid-cols-3 max-lg:grid-cols-2">
        <MetricCard label="Спарсено" value={overview?.parsed ?? 0} />
        <MetricCard label="В очереди" value={overview?.queued ?? 0} />
        <MetricCard label="Авто-отправлено" value={overview?.auto_sent ?? 0} />
        <MetricCard label="Ручная отправка" value={overview?.manual_sent ?? 0} />
        <MetricCard label="Черновики" value={overview?.draft ?? 0} />
        <MetricCard label="Ответы" value={overview?.responses ?? 0} />
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
    </div>
  )
}
