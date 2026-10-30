import { useState } from 'react'
import { RefreshCw, TrendingUp } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, type FunnelData } from '../lib/api'
import { fmtNum } from '../lib/utils'

const stages: Array<{ key: keyof Pick<FunnelData, 'sent' | 'responded' | 'hired' | 'completed' | 'paid'>; label: string }> = [
  { key: 'sent', label: 'Отправлено откликов' },
  { key: 'responded', label: 'Получено ответов' },
  { key: 'hired', label: 'Нанято' },
  { key: 'completed', label: 'Выполнено' },
  { key: 'paid', label: 'Оплачено' },
]

export default function Conversion() {
  const [days, setDays] = useState(30)
  const { data, loading, error, refetch } = useApi(() => api.getFunnel(days), [days])

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
      {data && (
        <>
          <div className="grid grid-cols-5 gap-3 max-xl:grid-cols-3 max-md:grid-cols-2">
            {stages.map(({ key, label }) => (
              <div key={key} className="card scanline flex flex-col gap-1 min-w-0">
                <span className="mono-label">{label}</span>
                <span className="text-2xl font-semibold text-white">{fmtNum(data[key])}</span>
              </div>
            ))}
          </div>
          <div className="card">
            <h2 className="mb-4 flex items-center gap-2 text-sm font-medium text-zinc-300"><TrendingUp className="w-4 h-4 text-brand-400" />Конверсия между этапами</h2>
            <div className="grid grid-cols-4 gap-4 max-lg:grid-cols-2">
              {([
                ['response_rate', 'Ответили / отправлено'],
                ['hire_rate', 'Нанято / ответили'],
                ['completion_rate', 'Выполнено / нанято'],
                ['payment_rate', 'Оплачено / выполнено'],
              ] as const).map(([key, label]) => (
                <div key={key}>
                  <div className="text-xs text-zinc-500">{label}</div>
                  <div className="mt-1 text-lg font-semibold text-white">{data[key].toFixed(1)}%</div>
                </div>
              ))}
            </div>
          </div>
          <p className="text-xs text-zinc-500">API предоставляет общую воронку по выбранному периоду. Разбивка по провайдерам, нишам, очереди, времени ответа и A/B-вариантам в этом endpoint недоступна.</p>
        </>
      )}
    </div>
  )
}
