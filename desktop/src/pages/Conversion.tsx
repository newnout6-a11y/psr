import { useState } from 'react'
import { RefreshCw, TrendingUp } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, type ConversionAnalytics, type ConversionRow } from '../lib/api'
import { fmtNum } from '../lib/utils'

const stages: Array<{ key: 'sent' | 'replied' | 'won'; label: string }> = [
  { key: 'sent', label: 'Отправлено откликов' },
  { key: 'replied', label: 'Ответили' },
  { key: 'won', label: 'Выиграно' },
]

function Breakdown({ title, rows, keyName }: { title: string; rows: ConversionRow[]; keyName: 'provider' | 'niche' | 'queue_bucket' | 'prompt_variant' }) {
  return <div className="card min-w-0 overflow-x-auto">
    <h2 className="mb-3 text-sm font-medium text-white">{title}</h2>
    {!rows.length ? <p className="text-xs text-zinc-500">Нет данных за выбранный период</p> : <table className="w-full text-xs">
      <thead><tr className="text-zinc-500"><th className="text-left pb-2">Группа</th><th className="text-right pb-2">Отправлено</th><th className="text-right pb-2">Ответили</th><th className="text-right pb-2">Выиграно</th><th className="text-right pb-2">Ответы, %</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={`${row[keyName] ?? 'unknown'}-${index}`} className="border-t border-white/5">
        <td className="py-2 text-zinc-300">{row[keyName] || 'Не указано'}</td><td className="text-right">{row.sent}</td><td className="text-right">{row.replied}</td><td className="text-right">{row.won}</td><td className="text-right">{row.reply_rate.toFixed(1)}</td>
      </tr>)}</tbody>
    </table>}
  </div>
}

export default function Conversion() {
  const [days, setDays] = useState(30)
  const { data, loading, error, refetch } = useApi(() => api.getConversion(days), [days])
  const current: ConversionAnalytics | undefined = !loading && !error && data?.days === days ? data : undefined

  return (
    <div className="space-y-6 p-6 max-lg:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="page-kicker">фидбек-петля</div>
          <h1 className="text-xl font-semibold text-white">Конверсия</h1>
        </div>
        <div className="flex items-center gap-3">
          <select value={days} onChange={(event) => setDays(Number(event.target.value))} className="input w-28 py-1.5">
            {[7, 14, 30, 60, 90].map((value) => <option key={value} value={value}>{value} дней</option>)}
          </select>
          <button onClick={refetch} className="btn btn-ghost py-1.5" aria-label="Обновить конверсию">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>
      {error && <div className="card border border-red-500/40 text-red-300 text-sm">Не удалось загрузить конверсию: {error}</div>}
      {loading && !data && <div className="text-sm text-zinc-500">Загрузка воронки…</div>}
      {current?.status === 'unavailable' && <div className="card text-amber-300 text-sm">База откликов или поля аналитики пока недоступны. Показатели нельзя считать достоверными.</div>}
      {current?.status === 'ready' && (
        <>
          <div className="grid grid-cols-5 gap-3 max-xl:grid-cols-3 max-md:grid-cols-2">
            {stages.map(({ key, label }) => (
              <div key={key} className="card scanline flex flex-col gap-1 min-w-0">
                <span className="mono-label">{label}</span>
                <span className="text-2xl font-semibold text-white">{fmtNum(current.summary[key])}</span>
              </div>
            ))}
          </div>
          <div className="card">
            <h2 className="mb-4 flex items-center gap-2 text-sm font-medium text-zinc-300"><TrendingUp className="w-4 h-4 text-brand-400" />Конверсия между этапами</h2>
            <div className="grid grid-cols-3 gap-4 max-lg:grid-cols-2">
              {([
                ['reply_rate', 'Ответили / отправлено'],
                ['win_rate', 'Выиграно / отправлено'],
              ] as const).map(([key, label]) => (
                <div key={key}>
                  <div className="text-xs text-zinc-500">{label}</div>
                  <div className="mt-1 text-lg font-semibold text-white">{current.summary[key].toFixed(1)}%</div>
                </div>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
            <Breakdown title="По провайдеру LLM" rows={current.by_provider} keyName="provider" />
            <Breakdown title="По поисковому запросу" rows={current.by_niche} keyName="niche" />
            <Breakdown title="По сохранённому числу конкурирующих предложений" rows={current.by_queue_position} keyName="queue_bucket" />
            <Breakdown title="По варианту промпта" rows={current.by_prompt_variant} keyName="prompt_variant" />
          </div>
          <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
            <div className="card"><h2 className="mb-3 text-sm font-medium text-white">Время первого ответа</h2>{current.by_response_time.length ? current.by_response_time.map((row) => <div key={row.response_bucket} className="flex justify-between gap-2 border-t border-white/5 py-2 text-xs"><span>{row.response_bucket}</span><span>{row.replies} ответов · {row.won} выиграно</span></div>) : <p className="text-xs text-zinc-500">Нет измеримых ответов</p>}</div>
            <div className="card"><h2 className="mb-3 text-sm font-medium text-white">Классификация ответов</h2>{current.classification_breakdown.length ? current.classification_breakdown.map((row) => <div key={row.classification} className="flex justify-between gap-2 border-t border-white/5 py-2 text-xs"><span>{row.classification}</span><span>{row.replies} ответов · {row.won} выиграно</span></div>) : <p className="text-xs text-zinc-500">Нет классифицированных ответов</p>}</div>
          </div>
          <p className="text-xs text-zinc-500">Основа: кандидаты с подтверждённой отправкой за {days} дней. Статус найма не исключает отклик из выборки; доход в рублях учитывает только зафиксированное значение по выигранным заказам. Число предложений может меняться после отправки.</p>
        </>
      )}
    </div>
  )
}
