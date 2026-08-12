import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CheckSquare, RefreshCw, Square, Users, Wifi } from 'lucide-react'

import { marketJobsApi } from '../api'
import type { MarketAccountPoolAccount, MarketAccountPoolBinding, MarketAccountPoolSnapshot } from '../types'
import { formatCount } from './shared'

interface AccountTeamPickerProps {
  selectedIds: string[]
  onSelectedIdsChange(ids: string[]): void
  onCapacityChange?(capacity: number): void
}

function isActiveBinding(binding: MarketAccountPoolBinding): boolean {
  return binding.state === 'active'
}

function accountAvailability(account: MarketAccountPoolAccount, binding?: MarketAccountPoolBinding): string {
  if (binding && isActiveBinding(binding)) return `занят ${binding.worker_id}`
  if (!account.market_enabled) return 'отключён в пуле'
  if (account.status !== 'activated') return account.status || 'не активирован'
  if (account.session_cookie_count < 1) return 'нет cookies'
  return 'готов к назначению'
}

function isEligible(account: MarketAccountPoolAccount, binding?: MarketAccountPoolBinding): boolean {
  return account.market_enabled && account.status === 'activated' && account.session_cookie_count > 0 && !binding
}

function StatusPill({ value, active }: { value: string; active: boolean }) {
  return (
    <span className={active ? 'badge border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'badge border-zinc-500/30 bg-zinc-500/10 text-zinc-400'}>
      {value}
    </span>
  )
}

function AccountTeamRow({
  account,
  binding,
  selected,
  sharedSignupIpCount,
  onToggle,
}: {
  account: MarketAccountPoolAccount
  binding?: MarketAccountPoolBinding
  selected: boolean
  sharedSignupIpCount: number
  onToggle(registrationId: string): void
}) {
  const eligible = isEligible(account, binding)
  const availability = accountAvailability(account, binding)
  const route = binding?.current_egress_ip || binding?.egress_ip || account.signup_ip || '-'
  const slot = binding?.slot ?? account.preferred_slot ?? account.registration_slot

  return (
    <tr className={selected ? 'border-t border-emerald-500/20 bg-emerald-500/[0.035]' : 'border-t border-surface-700/60 hover:bg-surface-800/45'}>
      <td className="px-3 py-2 text-center">
        <input
          type="checkbox"
          aria-label={`Использовать аккаунт ${account.username}`}
          checked={selected}
          disabled={!eligible}
          onChange={() => onToggle(account.registration_id)}
          className="h-4 w-4 accent-emerald-500 disabled:cursor-not-allowed disabled:opacity-35"
        />
      </td>
      <td className="px-3 py-2">
        <div className="font-mono text-sm text-zinc-200">{account.username || account.registration_id}</div>
        <div className="mt-0.5 truncate text-[11px] text-zinc-500">{account.email || account.registration_id}</div>
      </td>
      <td className="px-3 py-2">
        <StatusPill value={account.status === 'activated' ? 'активирован' : account.status || 'неизвестно'} active={account.status === 'activated'} />
        <div className="mt-1 text-[11px] text-zinc-500">cookies: {account.session_cookie_count}</div>
      </td>
      <td className="px-3 py-2">
        <div className="font-mono text-zinc-300">{route}</div>
        <div className="mt-0.5 text-[11px] text-zinc-500">{slot ? `${binding ? 'сейчас' : 'регистрация'}: slot ${slot}` : 'слот регистрации не сохранён'}</div>
        {!binding && account.signup_ip && sharedSignupIpCount > 1 && (
          <div className="mt-1 text-[11px] text-amber-300">общий IP регистрации: {sharedSignupIpCount} аккаунта</div>
        )}
      </td>
      <td className="px-3 py-2">
        <div className="font-mono text-[11px] text-zinc-400">{account.persona_id || '-'}</div>
        <div className="mt-1"><StatusPill value={availability} active={eligible || selected} /></div>
      </td>
    </tr>
  )
}

export function AccountTeamPicker({ selectedIds, onSelectedIdsChange, onCapacityChange }: AccountTeamPickerProps) {
  const [snapshot, setSnapshot] = useState<MarketAccountPoolSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const refreshInFlight = useRef(false)

  const loadPool = useCallback(async (refreshRoutes = true, background = false) => {
    if (refreshInFlight.current) return
    refreshInFlight.current = true
    if (background) setRefreshing(true)
    else setLoading(true)
    try {
      const result = refreshRoutes
        ? await marketJobsApi.syncAccountPool()
        : await marketJobsApi.getAccountPool()
      setSnapshot(result)
      setLastUpdatedAt(Date.now())
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (background) setRefreshing(false)
      else setLoading(false)
      refreshInFlight.current = false
    }
  }, [])

  useEffect(() => {
    void loadPool(true)
    const refreshVisiblePool = () => {
      if (document.visibilityState === 'visible') void loadPool(true, true)
    }
    const timer = window.setInterval(refreshVisiblePool, 15_000)
    document.addEventListener('visibilitychange', refreshVisiblePool)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshVisiblePool)
    }
  }, [loadPool])

  const activeBindings = useMemo(() => {
    const bindings = new Map<string, MarketAccountPoolBinding>()
    for (const binding of snapshot?.bindings ?? []) {
      if (isActiveBinding(binding)) bindings.set(binding.registration_id, binding)
    }
    return bindings
  }, [snapshot?.bindings])

  const eligibleAccounts = useMemo(
    () => (snapshot?.accounts ?? []).filter((account) => isEligible(account, activeBindings.get(account.registration_id))),
    [activeBindings, snapshot?.accounts],
  )
  const selectedEligibleIds = useMemo(
    () => selectedIds.filter((id) => eligibleAccounts.some((account) => account.registration_id === id)),
    [eligibleAccounts, selectedIds],
  )
  const selectedSet = useMemo(() => new Set(selectedEligibleIds), [selectedEligibleIds])
  const allEligibleSelected = eligibleAccounts.length > 0 && selectedEligibleIds.length === eligibleAccounts.length
  const capacity = snapshot?.summary.effective_capacity ?? 0
  const reserveCount = Math.max(selectedEligibleIds.length - capacity, 0)
  const runnableCount = Math.min(selectedEligibleIds.length, capacity)
  const routesNeedVerification = Boolean(snapshot && eligibleAccounts.length && snapshot.summary.routes_healthy === 0)
  const signupIpCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const account of snapshot?.accounts ?? []) {
      if (account.signup_ip) counts.set(account.signup_ip, (counts.get(account.signup_ip) ?? 0) + 1)
    }
    return counts
  }, [snapshot?.accounts])

  useEffect(() => {
    if (selectedEligibleIds.length === selectedIds.length) return
    onSelectedIdsChange(selectedEligibleIds)
  }, [onSelectedIdsChange, selectedEligibleIds, selectedIds.length])

  useEffect(() => {
    onCapacityChange?.(capacity)
  }, [capacity, onCapacityChange])

  function toggleAccount(registrationId: string) {
    const next = selectedSet.has(registrationId)
      ? selectedEligibleIds.filter((id) => id !== registrationId)
      : [...selectedEligibleIds, registrationId]
    onSelectedIdsChange(next)
  }

  function toggleAll() {
    onSelectedIdsChange(allEligibleSelected ? [] : eligibleAccounts.map((account) => account.registration_id))
  }

  return (
    <section className="mt-5 border-t border-surface-700/60 pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2"><Users className="h-4 w-4 text-sky-300" /><h3 className="text-sm font-medium text-white">Команда аккаунтов</h3></div>
          <p className="mt-1 text-xs text-zinc-500">
            Локальная готовность по cookies и живые VPNTE. Сессия Kwork проверяется воркером после назначения.
            {lastUpdatedAt && <span className="ml-2">Обновлено {new Date(lastUpdatedAt).toLocaleTimeString('ru-RU')}.</span>}
          </p>
        </div>
        <button type="button" title="Сверить живые VPNTE" aria-label="Сверить живые VPNTE" className="btn btn-ghost h-8 w-8 justify-center px-0" disabled={loading || refreshing} onClick={() => void loadPool(true)}>
          <RefreshCw className={refreshing ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
        </button>
      </div>

      <div className="mt-3 grid grid-cols-7 gap-px overflow-hidden border border-surface-700/60 bg-surface-700/60 max-xl:grid-cols-4 max-lg:grid-cols-2">
        {[
          ['Выбрано', formatCount(selectedEligibleIds.length)],
          ['Запустится сейчас', formatCount(runnableCount)],
          ['Резерв', formatCount(reserveCount)],
          ['Кандидаты', formatCount(eligibleAccounts.length)],
          ['Пул профилей', formatCount(snapshot?.summary.provider_profiles_total ?? 0)],
          ['VPNTE сейчас', formatCount(snapshot?.summary.routes_healthy ?? 0)],
          ['Уникальные IP', formatCount(snapshot?.summary.egress_ips_distinct ?? 0)],
        ].map(([label, value]) => <div key={label} className="bg-surface-900/90 px-3 py-2"><div className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</div><div className="mt-1 font-mono text-sm text-zinc-200">{value}</div></div>)}
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <div className={reserveCount ? 'text-xs text-amber-300' : 'text-xs text-zinc-500'}>
          {routesNeedVerification
            ? 'Сейчас нет ни одного живого VPNTE-маршрута. Аккаунты не запустятся.'
            : reserveCount
            ? `Одновременно пойдут ${runnableCount}; ещё ${reserveCount} останется в резерве до освобождения уникального IP.`
            : `Локально готовы ${eligibleAccounts.length} аккаунтов; сейчас можно запустить до ${capacity} по числу уникальных VPNTE IP.`}
        </div>
        <button type="button" className="btn btn-ghost h-8 px-2 text-xs" disabled={loading || !eligibleAccounts.length} onClick={toggleAll}>
          {allEligibleSelected ? <Square className="h-3.5 w-3.5" /> : <CheckSquare className="h-3.5 w-3.5" />}
          {allEligibleSelected ? 'Снять выбор' : 'Выбрать кандидатов'}
        </button>
      </div>

      {error && <div className="mt-3 flex items-center gap-2 border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200"><Wifi className="h-3.5 w-3.5 shrink-0" />Не удалось загрузить пул: {error}</div>}
      <div className="mt-3 overflow-x-auto border border-surface-700/60">
        <table className="w-full min-w-[860px] text-left text-xs">
          <thead className="bg-surface-900 text-zinc-500"><tr><th className="w-11 px-3 py-2 text-center font-medium">Выбор</th><th className="px-3 py-2 font-medium">Аккаунт</th><th className="px-3 py-2 font-medium">Локальное состояние</th><th className="px-3 py-2 font-medium">IP регистрации / slot</th><th className="px-3 py-2 font-medium">Persona / назначение</th></tr></thead>
          <tbody>
            {!loading && (snapshot?.accounts ?? []).map((account) => <AccountTeamRow key={account.registration_id} account={account} binding={activeBindings.get(account.registration_id)} selected={selectedSet.has(account.registration_id)} sharedSignupIpCount={account.signup_ip ? signupIpCounts.get(account.signup_ip) ?? 0 : 0} onToggle={toggleAccount} />)}
            {loading && <tr><td colSpan={5} className="px-3 py-7 text-center text-zinc-500">Загружаем сохранённые аккаунты...</td></tr>}
            {!loading && !error && !(snapshot?.accounts.length) && <tr><td colSpan={5} className="px-3 py-7 text-center text-zinc-500">В пуле ещё нет зарегистрированных аккаунтов.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
