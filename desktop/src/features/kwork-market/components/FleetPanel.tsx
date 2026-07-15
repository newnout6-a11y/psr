import { useCallback, useEffect, useMemo, useState } from 'react'
import { Check, ChevronDown, Link2, RefreshCw, RotateCw, Search, UsersRound, Wifi } from 'lucide-react'

import { marketJobsApi } from '../api'
import type { MarketAccountPoolAccount, MarketAccountPoolBinding, MarketAccountPoolSnapshot } from '../types'
import { IconButton, formatCount, formatTimestamp } from './shared'

interface FleetPanelProps {
  jobId: string
}

function modeLabel(value: string | null | undefined): string {
  if (value === 'retained') return 'Закреплён'
  if (value === 'preferred_ip') return 'IP регистрации'
  if (value === 'preferred_slot') return 'Предпочтительный слот'
  if (value === 'fallback') return 'Резервный маршрут'
  return value || 'Не назначен'
}

function statusLabel(value: string | null | undefined): string {
  if (value === 'active') return 'Активен'
  if (value === 'released') return 'Освобождён'
  if (value === 'healthy') return 'Исправен'
  if (value === 'degraded') return 'Нестабилен'
  return value || '-'
}

export function FleetPanel({ jobId }: FleetPanelProps) {
  const [snapshot, setSnapshot] = useState<MarketAccountPoolSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accountQuery, setAccountQuery] = useState('')

  const loadFleet = useCallback(async (sync = false) => {
    setLoading(true)
    try {
      const result = sync ? await marketJobsApi.syncAccountPool() : await marketJobsApi.getFleet(jobId)
      setSnapshot(sync ? await marketJobsApi.getFleet(jobId) : result)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setLoading(false)
    }
  }, [jobId])

  useEffect(() => { void loadFleet() }, [loadFleet])

  const activeBindings = useMemo(() => snapshot?.bindings.filter((binding) => binding.state === 'active') ?? [], [snapshot])
  const visibleBindings = activeBindings.length ? activeBindings : snapshot?.bindings ?? []
  const filteredAccounts = useMemo(() => {
    const needle = accountQuery.trim().toLocaleLowerCase('ru-RU')
    if (!needle) return snapshot?.accounts ?? []
    return (snapshot?.accounts ?? []).filter((account) =>
      `${account.username} ${account.email} ${account.signup_ip ?? ''} ${account.registration_id}`
        .toLocaleLowerCase('ru-RU')
        .includes(needle),
    )
  }, [accountQuery, snapshot])

  async function runAction(action: string, task: () => Promise<unknown>) {
    setBusyAction(action)
    try {
      await task()
      await loadFleet()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusyAction(null)
    }
  }

  function toggleAccount(account: MarketAccountPoolAccount) {
    void runAction(`account:${account.registration_id}`, () => marketJobsApi.setAccountPoolAccount(account.registration_id, { enabled: !account.market_enabled }))
  }

  function rebindWorker(workerId: string) {
    void runAction(`worker:${workerId}`, () => marketJobsApi.rebindWorker(jobId, workerId))
  }

  const summary = snapshot?.summary
  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div className="flex min-w-0 items-center gap-3">
          <span className="market-icon-tile"><UsersRound className="h-4 w-4" /></span>
          <div className="min-w-0">
            <div className="market-section-label">Fleet</div>
            <h2 className="mt-1 truncate text-base font-semibold text-white">Аккаунты, workers и IP</h2>
          </div>
        </div>
        <div className="flex items-center gap-1">
          <IconButton title="Обновить fleet" disabled={loading || busyAction !== null} onClick={() => void loadFleet()}><RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} /></IconButton>
          <button type="button" disabled={loading || busyAction !== null} onClick={() => void loadFleet(true)} className="market-compact-action"><Wifi className="h-3.5 w-3.5" />Синхронизировать пул</button>
          <button type="button" disabled={loading || busyAction !== null} onClick={() => void runAction('reconcile', () => marketJobsApi.reconcileFleet(jobId))} className="market-compact-action"><RotateCw className="h-3.5 w-3.5" />Reconcile</button>
        </div>
      </div>

      {error && <div className="market-error-banner">{error}</div>}

      <div className="market-fleet-summary">
        <FleetMetric label="Команда" value={`${formatCount(summary?.accounts_selected ?? summary?.accounts_eligible ?? 0)} / ${formatCount(summary?.accounts_eligible ?? 0)}`} />
        <FleetMetric label="Активны" value={formatCount(summary?.accounts_active ?? 0)} />
        <FleetMetric label="Уникальные IP" value={`${formatCount(summary?.egress_ips_active ?? 0)} / ${formatCount(summary?.egress_ips_distinct ?? 0)}`} />
        <FleetMetric label="Маршруты" value={`${formatCount(summary?.routes_healthy ?? 0)} здоровых`} />
        <FleetMetric label="Ёмкость" value={formatCount(summary?.effective_capacity ?? 0)} />
      </div>

      <div className="market-table-toolbar">
        <div>
          <strong>{activeBindings.length ? 'Активные привязки' : 'История привязок'}</strong>
          <span>{visibleBindings.length} записей</span>
        </div>
        {!activeBindings.length && !!snapshot?.bindings.length && <span className="text-xs text-zinc-500">Запуск завершён, маршруты освобождены</span>}
      </div>
      <div className="max-h-[32rem] overflow-auto">
        <table className="market-data-table min-w-[980px]">
          <thead><tr><th>Worker</th><th>Аккаунт</th><th>Slot / IP</th><th>Назначение</th><th>Persona</th><th className="text-right">Действие</th></tr></thead>
          <tbody>
            {visibleBindings.map((binding) => (
              <BindingRow key={`${binding.worker_id}:${binding.registration_id}`} binding={binding} busy={busyAction !== null} onRebind={rebindWorker} />
            ))}
            {!visibleBindings.length && <tr><td colSpan={6}><div className="market-empty-state">Привязки появятся после запуска workers.</div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="market-table-footer">
        <span>Обновлено {formatTimestamp(new Date().toISOString())}</span>
        <span>Один worker использует один аккаунт и один подтверждённый IP</span>
      </div>

      {!!snapshot?.accounts.length && (
        <details className="market-disclosure border-t border-white/10">
          <summary>
            <span><span className="market-section-label">Пул аккаунтов</span><strong>Команда и доступные аккаунты</strong></span>
            <span>{snapshot.accounts.length} аккаунтов <ChevronDown className="h-4 w-4" /></span>
          </summary>
          <div className="market-table-toolbar border-t border-white/10">
            <span className="text-xs text-zinc-500">Включайте аккаунты для будущих запусков прямо здесь.</span>
            <label className="market-search-field">
              <Search className="h-3.5 w-3.5" />
              <input value={accountQuery} onChange={(event) => setAccountQuery(event.target.value)} placeholder="Найти аккаунт" />
            </label>
          </div>
          <div className="max-h-[30rem] overflow-auto">
            <table className="market-data-table min-w-[980px]">
              <thead><tr><th>Аккаунт</th><th>IP регистрации</th><th>Слот</th><th>Persona</th><th>Cookies</th><th>Команда</th><th className="text-right">Market</th></tr></thead>
              <tbody>{filteredAccounts.map((account) => <AccountRow key={account.registration_id} account={account} busy={busyAction !== null} onToggle={toggleAccount} />)}</tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  )
}

function FleetMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>
}

function BindingRow({ binding, busy, onRebind }: { binding: MarketAccountPoolBinding; busy: boolean; onRebind(workerId: string): void }) {
  const currentIp = binding.current_egress_ip || binding.egress_ip || '-'
  const preferred = binding.signup_ip || (binding.preferred_slot ? `slot ${binding.preferred_slot}` : '-')
  const active = binding.state === 'active'
  return (
    <tr>
      <td><div className="font-mono text-zinc-200">{binding.worker_id}</div><div className={`market-row-note ${active ? 'text-emerald-300' : ''}`}>{statusLabel(binding.state)}</div></td>
      <td><div className="font-mono text-zinc-200">{binding.username || binding.registration_id}</div><div className="market-row-note font-mono">{binding.registration_id}</div></td>
      <td><div className="font-mono text-zinc-300">{binding.slot ? `slot ${binding.slot}` : binding.transport_id || '-'}</div><div className="market-row-note font-mono">{currentIp}</div></td>
      <td><div className="text-zinc-300">{modeLabel(binding.binding_mode)}</div><div className="market-row-note">предпочтение: {preferred}</div></td>
      <td><div className="font-mono text-zinc-300">{binding.persona_id || '-'}</div><div className="market-row-note">{statusLabel(binding.route_health)}</div></td>
      <td className="text-right"><IconButton title="Перепривязать к свободному IP" disabled={busy || !active} onClick={() => onRebind(binding.worker_id)}><Link2 className="h-3.5 w-3.5" /></IconButton></td>
    </tr>
  )
}

function AccountRow({ account, busy, onToggle }: { account: MarketAccountPoolAccount; busy: boolean; onToggle(account: MarketAccountPoolAccount): void }) {
  return (
    <tr>
      <td><div className="font-mono text-zinc-200">{account.username}</div><div className="market-row-note">{account.email}</div></td>
      <td className="font-mono">{account.signup_ip || '-'}</td>
      <td>{account.preferred_slot ? `slot ${account.preferred_slot}` : '-'}</td>
      <td className="font-mono">{account.persona_id || '-'}</td>
      <td className="font-mono">{account.session_cookie_count}</td>
      <td><span className={`market-tone-label ${account.selected_for_job ? 'market-tone-positive' : 'market-tone-neutral'}`}>{account.selected_for_job ? 'В команде' : 'Не выбран'}</span></td>
      <td className="text-right"><button type="button" disabled={busy} aria-pressed={account.market_enabled} onClick={() => onToggle(account)} className={`market-inline-toggle ${account.market_enabled ? 'is-on' : ''}`} title={account.market_enabled ? 'Отключить аккаунт в пуле' : 'Включить аккаунт в пуле'}><Check className="h-3.5 w-3.5" />{account.market_enabled ? 'Включён' : 'Выключен'}</button></td>
    </tr>
  )
}
