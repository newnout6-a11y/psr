import { useApi } from '../hooks/useApi'
import { api, HealthData, type KworkVerificationStatus } from '../lib/api'
import { HeartPulse, AlertTriangle, CheckCircle2, XCircle, Activity, Bell, Shield, Zap, Clock, MessageCircle, RefreshCw } from 'lucide-react'
import { cn } from '../lib/utils'

export default function Health() {
  const { data, loading, refetch } = useApi(() => (api as any).getHealth(), [])
  const { data: connectsCheck } = useApi(() => (api as any).getConnectsCheck(), [])
  const { data: breakerData } = useApi(() => api.getBreaker(), [])
  const { data: alertsData } = useApi(() => (api as any).getAlerts(), [])
  const {
    data: verificationStatus,
    loading: verificationLoading,
    error: verificationError,
    refetch: refetchVerification,
  } = useApi(() => api.getKworkVerificationStatus(false), [])

  const h = data as HealthData | null
  const verification = verificationStatus as KworkVerificationStatus | undefined

  if (loading) return <div className="p-4 text-zinc-500">Загрузка...</div>
  if (!h) return <div className="p-4 text-zinc-500">Нет данных</div>

  const manualVerificationRequired = Boolean(h.manual_verification_required || verification?.manual_verification_required)
  const verificationOk = verification?.status === 'ok'
  const verificationWarn = verification?.status === 'api_flag_only'
  const verificationBad = verification?.status === 'manual_required' || verification?.status === 'error'

  return (
    <div className="space-y-4 p-6 max-lg:p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="page-kicker">kwork health</div>
          <h1 className="text-xl font-bold text-white flex items-center gap-2">
            <HeartPulse className="w-5 h-5" /> Статус аккаунта
          </h1>
        </div>
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

      <div className="grid grid-cols-4 gap-3 max-lg:grid-cols-2">
        <div className={cn('card p-4', manualVerificationRequired && 'border-red-500/50')}>
          <div className="flex items-center gap-2 mb-1">
            {manualVerificationRequired ? <XCircle className="w-4 h-4 text-red-400" /> : <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
            <span className="text-xs text-zinc-500">Капча</span>
          </div>
          <div className={cn('text-lg font-bold', manualVerificationRequired ? 'text-red-400' : 'text-emerald-400')}>
            {manualVerificationRequired ? 'Требуется' : 'OK'}
          </div>
        </div>

        {h.captcha_api_flag && !manualVerificationRequired && (
          <div className="card p-4 border-amber-500/30">
            <div className="text-xs text-zinc-500">Captcha API flag</div>
            <div className="mt-1 text-sm font-medium text-amber-300">API flag only</div>
            {h.captcha_check_error && <div className="mt-1 text-xs text-zinc-500">check error</div>}
          </div>
        )}

        <div className={cn('card p-4', h.success_rate < 80 && 'border-yellow-500/50')}>
          <div className="flex items-center gap-2 mb-1">
            <Activity className="w-4 h-4 text-zinc-400" />
            <span className="text-xs text-zinc-500">Success Rate</span>
          </div>
          <div className={cn('text-lg font-bold', h.success_rate < 70 ? 'text-red-400' : h.success_rate < 80 ? 'text-yellow-400' : 'text-emerald-400')}>
            {h.success_rate}%
          </div>
          <div className="text-xs text-zinc-500">{h.completed} done / {h.cancelled} cancelled</div>
        </div>

        <div className={cn('card p-4', h.busy_risk && 'border-yellow-500/50')}>
          <div className="flex items-center gap-2 mb-1">
            {h.busy_risk ? <AlertTriangle className="w-4 h-4 text-yellow-400" /> : <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
            <span className="text-xs text-zinc-500">Активные заказы</span>
          </div>
          <div className={cn('text-lg font-bold', h.busy_risk ? 'text-yellow-400' : 'text-white')}>
            {h.active_orders}
          </div>
          {h.busy_risk && <div className="text-xs text-yellow-400">Риск "Занят"!</div>}
        </div>

        <div className="card p-4">
          <div className="flex items-center gap-2 mb-1">
            <Bell className="w-4 h-4 text-zinc-400" />
            <span className="text-xs text-zinc-500">Уведомления</span>
          </div>
          <div className={cn('text-lg font-bold', h.unread_notifications > 0 ? 'text-amber-400' : 'text-white')}>
            {h.unread_notifications}
          </div>
          <div className="text-xs text-zinc-500">непрочитанных</div>
        </div>
      </div>

      <div className={cn(
        'card p-4',
        verificationOk && 'border-emerald-500/30',
        verificationWarn && 'border-amber-500/30',
        verificationBad && 'border-red-500/40',
      )}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-zinc-200 flex items-center gap-2">
              <Shield className={cn(
                'w-4 h-4',
                verificationOk ? 'text-emerald-400' : verificationWarn ? 'text-amber-400' : verificationBad ? 'text-red-400' : 'text-zinc-500',
              )} />
              Kwork web verification
            </h3>
            <div className={cn(
              'mt-2 text-sm font-medium',
              verificationOk ? 'text-emerald-300' : verificationWarn ? 'text-amber-300' : verificationBad ? 'text-red-300' : 'text-zinc-400',
            )}>
              {verificationLoading ? 'Checking...' : verification?.status || 'not checked'}
            </div>
            <div className="mt-1 text-xs text-zinc-500">
              {verificationError || verification?.detail || 'Checks Kwork web pages, not only getCaptchaStatus.'}
            </div>
          </div>
          <button onClick={refetchVerification} disabled={verificationLoading} className="btn btn-ghost shrink-0 py-1 text-xs">
            <RefreshCw className={cn('h-3.5 w-3.5', verificationLoading && 'animate-spin')} />
            Refresh
          </button>
        </div>
        <div className="mt-3 grid grid-cols-4 gap-2 text-xs max-lg:grid-cols-2">
          <div className="rounded-md border border-surface-700 bg-surface-950/40 px-2 py-1.5">
            <div className="text-zinc-500">cookies</div>
            <div className="text-zinc-200">{verification?.cookie_count ?? '—'}</div>
          </div>
          <div className="rounded-md border border-surface-700 bg-surface-950/40 px-2 py-1.5">
            <div className="text-zinc-500">web session</div>
            <div className={verification?.web_session_ok ? 'text-emerald-300' : 'text-zinc-400'}>
              {verification?.web_session_ok ? 'ok' : 'unknown'}
            </div>
          </div>
          <div className="rounded-md border border-surface-700 bg-surface-950/40 px-2 py-1.5">
            <div className="text-zinc-500">SmartCaptcha script</div>
            <div className={verification?.smartcaptcha_scripts_seen ? 'text-amber-300' : 'text-zinc-400'}>
              {verification?.smartcaptcha_scripts_seen ? 'seen' : 'not seen'}
            </div>
          </div>
          <div className="rounded-md border border-surface-700 bg-surface-950/40 px-2 py-1.5">
            <div className="text-zinc-500">API flag</div>
            <div className={verification?.captcha_status?.required ? 'text-amber-300' : 'text-zinc-400'}>
              {verification?.captcha_status?.required ? 'required' : verification?.captcha_status?.error ? 'error' : 'clear'}
            </div>
          </div>
        </div>
        {!!verification?.pages?.length && (
          <div className="mt-3 flex flex-wrap gap-1.5 text-[11px]">
            {verification.pages.map((page) => (
              <span
                key={page.path}
                className={cn(
                  'rounded border px-1.5 py-0.5',
                  page.evidence?.manual_required
                    ? 'border-red-500/40 text-red-300'
                    : page.status_code >= 200 && page.status_code < 400
                      ? 'border-emerald-500/30 text-emerald-300'
                      : 'border-zinc-700 text-zinc-500',
                )}
              >
                {page.path}: {page.status_code || 'err'}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Connects check + Breaker */}
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        {/* Connects guardrails */}
        <div className="card p-4">
          <h3 className="text-sm font-medium text-zinc-300 mb-3 flex items-center gap-2">
            <Shield className="w-4 h-4 text-brand-400" /> Connects / Sending
          </h3>
          <div className="space-y-2 text-xs">
            <div className="flex items-center justify-between">
              <span className="text-zinc-500">Can send</span>
              <span className={connectsCheck?.can_send ? 'text-emerald-400' : 'text-red-400'}>
                {connectsCheck ? (connectsCheck.can_send ? 'Да' : 'Заблокировано') : '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-zinc-500">Free amount</span>
              <span className="text-zinc-300">{connectsCheck?.free_amount ?? '—'}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-zinc-500">Warn threshold</span>
              <span className="text-amber-400">{connectsCheck?.warn_threshold ?? '—'}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-zinc-500">Block threshold</span>
              <span className="text-red-400">{connectsCheck?.block_threshold ?? '—'}</span>
            </div>
          </div>
        </div>

        {/* Circuit Breaker */}
        <div className="card p-4">
          <h3 className="text-sm font-medium text-zinc-300 mb-3 flex items-center gap-2">
            <Zap className="w-4 h-4 text-brand-400" /> Circuit Breaker
          </h3>
          {breakerData && breakerData.length > 0 ? (
            <div className="space-y-2 text-xs">
              {breakerData.map((b) => (
                <div key={b.key} className="flex items-center justify-between">
                  <div>
                    <span className="text-zinc-300">{b.key}</span>
                    {b.consecutive_failures > 0 && (
                      <span className="text-red-400 ml-2">{b.consecutive_failures} ошибок</span>
                    )}
                  </div>
                  <span className={
                    b.state === 'CLOSED' ? 'text-emerald-400' :
                    b.state === 'OPEN' ? 'text-red-400' : 'text-amber-400'
                  }>
                    {b.state}
                    {b.paused_seconds_left > 0 && ` (${b.paused_seconds_left}s)`}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-zinc-500 text-xs">Все breaker'ы закрыты (OK)</p>
          )}
        </div>
      </div>

      {/* Follow-up alerts */}
      {alertsData?.count > 0 && alertsData?.alerts && (
        <div className="card p-4">
          <h3 className="text-sm font-medium text-zinc-300 mb-3 flex items-center gap-2">
            <Clock className="w-4 h-4 text-amber-400" /> Follow-up алерты
          </h3>
          <div className="space-y-2">
            {alertsData.alerts.map((alert: any, i: number) => (
              <div
                key={i}
                className={cn(
                  'flex items-start gap-2 rounded-md border px-3 py-2 text-xs',
                  alert.severity === 'critical'
                    ? 'border-red-500/30 bg-red-500/10 text-red-300'
                    : alert.severity === 'warning'
                      ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                      : 'border-blue-500/30 bg-blue-500/10 text-blue-300'
                )}
              >
                {alert.type === 'slow_response' ? (
                  <MessageCircle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                ) : (
                  <Bell className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-medium">{alert.title}</div>
                  <div className="text-zinc-400">{alert.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {h.paused_kworks && h.paused_kworks.length > 0 && (
        <div className="card p-3 text-xs text-yellow-400">
          ⚠️ На паузе {h.paused_kworks.length} кворков: {h.paused_kworks.join(', ')}
        </div>
      )}
    </div>
  )
}
