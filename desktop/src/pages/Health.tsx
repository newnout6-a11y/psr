import { useApi } from '../hooks/useApi'
import { api, HealthData } from '../lib/api'
import { HeartPulse, AlertTriangle, CheckCircle2, XCircle, Activity } from 'lucide-react'

export default function Health() {
  const { data, loading, refetch } = useApi(() => (api as any).getHealth(), [])

  const h = data as HealthData | null

  if (loading) return <div className="p-4 text-zinc-500">Загрузка...</div>
  if (!h) return <div className="p-4 text-zinc-500">Нет данных</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-white flex items-center gap-2">
          <HeartPulse className="w-5 h-5" /> Статус аккаунта
        </h1>
        <button onClick={refetch} className="btn btn-ghost text-xs">Обновить</button>
      </div>

      <div className="card p-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <div className="text-xs text-zinc-500">Пользователь</div>
            <div className="text-white font-medium">{h.username || '—'}</div>
          </div>
          <div>
            <div className="text-xs text-zinc-500">Уровень</div>
            <div className="text-white font-medium">{h.level || '—'}</div>
          </div>
          <div>
            <div className="text-xs text-zinc-500">Рейтинг</div>
            <div className="text-white font-medium">{h.rating} ({h.reviews_count} отзывов)</div>
          </div>
          <div>
            <div className="text-xs text-zinc-500">Connects</div>
            <div className="text-white font-medium">{h.connects_free} свободно / {h.connects_total} всего</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <div className={`card p-4 ${h.captcha_required ? 'border-red-500/50' : ''}`}>
          <div className="flex items-center gap-2 mb-1">
            {h.captcha_required ? <XCircle className="w-4 h-4 text-red-400" /> : <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
            <span className="text-xs text-zinc-500">Капча</span>
          </div>
          <div className={`text-lg font-bold ${h.captcha_required ? 'text-red-400' : 'text-emerald-400'}`}>
            {h.captcha_required ? 'Требуется' : 'OK'}
          </div>
        </div>

        <div className={`card p-4 ${h.success_rate < 80 ? 'border-yellow-500/50' : ''}`}>
          <div className="flex items-center gap-2 mb-1">
            <Activity className="w-4 h-4 text-zinc-400" />
            <span className="text-xs text-zinc-500">Success Rate</span>
          </div>
          <div className={`text-lg font-bold ${h.success_rate < 70 ? 'text-red-400' : h.success_rate < 80 ? 'text-yellow-400' : 'text-emerald-400'}`}>
            {h.success_rate}%
          </div>
          <div className="text-xs text-zinc-500">{h.completed} done / {h.cancelled} cancelled</div>
        </div>

        <div className={`card p-4 ${h.busy_risk ? 'border-yellow-500/50' : ''}`}>
          <div className="flex items-center gap-2 mb-1">
            {h.busy_risk ? <AlertTriangle className="w-4 h-4 text-yellow-400" /> : <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
            <span className="text-xs text-zinc-500">Активные заказы</span>
          </div>
          <div className={`text-lg font-bold ${h.busy_risk ? 'text-yellow-400' : 'text-white'}`}>
            {h.active_orders}
          </div>
          {h.busy_risk && <div className="text-xs text-yellow-400">Риск "Занят"!</div>}
        </div>
      </div>

      {h.paused_kworks && h.paused_kworks.length > 0 && (
        <div className="card p-3 text-xs text-yellow-400">
          ⚠️ На паузе {h.paused_kworks.length} кворков (авто-пауза при перегрузке)
        </div>
      )}
    </div>
  )
}
