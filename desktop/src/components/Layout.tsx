import { useState, useEffect, useCallback, type CSSProperties } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import {
  LayoutDashboard, ListChecks, Settings, ScrollText,
  Search, Play, Square, ChevronDown, Globe2,
  AlertCircle, CheckCircle2, Loader2, Asterisk, Monitor, ShieldCheck,
  Activity
} from 'lucide-react'
import { cn } from '../lib/utils'
import { api } from '../lib/api'
import { useWebSocket } from '../hooks/useWebSocket'

type PlatformId = 'kwork' | 'freelance_ru' | 'hh_ru'

interface OrchestratorStatus {
  cycle_running: boolean
  execution_mode: string
  paused_platforms: string[]
  last_cycle_stats: Record<string, number> | null
  last_error: string | null
  runtime_config?: {
    platforms?: string[]
    telegram_configured?: boolean
  }
}

interface RunConfig {
  platforms: PlatformId[]
  search_brief: string
  query_count: number
  pages_to_parse: number
  max_projects_per_cycle: number
  max_parse_seconds: number
  top_projects: number
  limit: number
  browser_headless: boolean
  telegram_enabled: boolean
  osint_enabled: boolean
  probiv_enabled: boolean
  session_hub_required: boolean
}

const NAV = [
  { to: '/dashboard', icon: LayoutDashboard, label: 'Панель' },
  { to: '/queue',     icon: ListChecks,      label: 'Очередь' },
  { to: '/settings',  icon: Settings,        label: 'Настройки' },
  { to: '/logs',      icon: ScrollText,      label: 'Логи' },
  { to: '/osint',     icon: Search,          label: 'Сигналы' },
]

const MODES = ['auto', 'semi_auto', 'manual', 'paused']

const PLATFORM_OPTIONS: { id: PlatformId; label: string }[] = [
  { id: 'kwork', label: 'Kwork' },
  { id: 'freelance_ru', label: 'FL.ru' },
  { id: 'hh_ru', label: 'HH' },
]

const DEFAULT_RUN_CONFIG: RunConfig = {
  platforms: ['kwork'],
  search_brief: 'мелкие заказы на автоматизацию, ботов, парсеры, скрипты, небольшие сайты. бюджет до 10к. не на постоянку',
  query_count: 8,
  pages_to_parse: 50,
  max_projects_per_cycle: 500,
  max_parse_seconds: 90,
  top_projects: 0,
  limit: 1,
  browser_headless: false,
  telegram_enabled: true,
  osint_enabled: false,
  probiv_enabled: false,
  session_hub_required: true,
}

const RUN_CONFIG_STORAGE_KEY = 'psr.runConfig.v1'

function isPlatformId(value: unknown): value is PlatformId {
  return value === 'kwork' || value === 'freelance_ru' || value === 'hh_ru'
}

function clampNumber(raw: string, min: number, max: number) {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return min
  return Math.max(min, Math.min(max, Math.trunc(parsed)))
}

function clampConfigNumber(value: unknown, fallback: number, min: number, max: number) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.min(max, Math.trunc(parsed)))
}

function readStoredRunConfig(): RunConfig {
  try {
    const raw = window.localStorage.getItem(RUN_CONFIG_STORAGE_KEY)
    if (!raw) return DEFAULT_RUN_CONFIG
    const saved = JSON.parse(raw) as Partial<RunConfig>
    const platforms = Array.isArray(saved.platforms)
      ? saved.platforms.filter(isPlatformId)
      : DEFAULT_RUN_CONFIG.platforms

    return {
      ...DEFAULT_RUN_CONFIG,
      platforms: platforms.length ? platforms : DEFAULT_RUN_CONFIG.platforms,
      search_brief: typeof saved.search_brief === 'string'
        ? saved.search_brief
        : DEFAULT_RUN_CONFIG.search_brief,
      query_count: clampConfigNumber(saved.query_count, DEFAULT_RUN_CONFIG.query_count, 1, 50),
      pages_to_parse: clampConfigNumber(saved.pages_to_parse, DEFAULT_RUN_CONFIG.pages_to_parse, 1, 100),
      max_projects_per_cycle: clampConfigNumber(
        saved.max_projects_per_cycle,
        DEFAULT_RUN_CONFIG.max_projects_per_cycle,
        20,
        2000
      ),
      max_parse_seconds: clampConfigNumber(saved.max_parse_seconds, DEFAULT_RUN_CONFIG.max_parse_seconds, 10, 600),
      top_projects: clampConfigNumber(saved.top_projects, DEFAULT_RUN_CONFIG.top_projects, 0, 50),
      limit: clampConfigNumber(saved.limit, DEFAULT_RUN_CONFIG.limit, 1, 20),
      browser_headless: typeof saved.browser_headless === 'boolean'
        ? saved.browser_headless
        : DEFAULT_RUN_CONFIG.browser_headless,
      telegram_enabled: typeof saved.telegram_enabled === 'boolean'
        ? saved.telegram_enabled
        : DEFAULT_RUN_CONFIG.telegram_enabled,
      osint_enabled: typeof saved.osint_enabled === 'boolean'
        ? saved.osint_enabled
        : DEFAULT_RUN_CONFIG.osint_enabled,
      probiv_enabled: typeof saved.probiv_enabled === 'boolean'
        ? saved.probiv_enabled
        : DEFAULT_RUN_CONFIG.probiv_enabled,
      session_hub_required: typeof saved.session_hub_required === 'boolean'
        ? saved.session_hub_required
        : DEFAULT_RUN_CONFIG.session_hub_required,
    }
  } catch {
    return DEFAULT_RUN_CONFIG
  }
}

function storeRunConfig(config: RunConfig) {
  try {
    window.localStorage.setItem(RUN_CONFIG_STORAGE_KEY, JSON.stringify(config))
  } catch {}
}

function NumberField({
  label,
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  disabled: boolean
  onChange: (value: number) => void
}) {
  return (
    <label className="block">
      <span className="block text-[11px] text-zinc-500 mb-1">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(clampNumber(e.target.value, min, max))}
        className="input h-8 px-2 py-1 text-xs"
      />
    </label>
  )
}

function ToggleButton({
  label,
  checked,
  disabled,
  onChange,
  activeText = 'вкл',
  inactiveText = 'выкл',
}: {
  label: string
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
  activeText?: string
  inactiveText?: string
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'flex items-center justify-between rounded-md border px-2.5 py-1.5 text-xs transition-colors',
        checked
          ? 'border-brand-500/40 bg-brand-600/15 text-stone-50'
          : 'border-surface-600 bg-surface-800 text-zinc-400 hover:text-white hover:bg-surface-700',
        disabled && 'opacity-50 cursor-not-allowed'
      )}
    >
      <span>{label}</span>
      <span className={checked ? 'text-brand-300' : 'text-zinc-500'}>
        {checked ? activeText : inactiveText}
      </span>
    </button>
  )
}

export default function Layout() {
  const [status, setStatus] = useState<OrchestratorStatus>({
    cycle_running: false,
    execution_mode: 'semi_auto',
    paused_platforms: [],
    last_cycle_stats: null,
    last_error: null,
  })
  const [modeOpen, setModeOpen] = useState(false)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [dryRun, setDryRun] = useState(true)
  const [runConfig, setRunConfig] = useState<RunConfig>(() => readStoredRunConfig())
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null)

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.getStatus()
      setStatus(s)
    } catch {}
  }, [])

  useEffect(() => {
    refreshStatus()
    const id = setInterval(refreshStatus, 10_000)
    return () => clearInterval(id)
  }, [refreshStatus])

  useEffect(() => {
    storeRunConfig(runConfig)
  }, [runConfig])

  useWebSocket('ws://127.0.0.1:7788/ws/status', (msg) => {
    if (msg.type === 'status') {
      setStatus((prev) => ({ ...prev, ...(msg as unknown as OrchestratorStatus) }))
    }
  })

  function showToast(msg: string, ok: boolean) {
    setToast({ msg, ok })
    setTimeout(() => setToast(null), 3500)
  }

  async function handleStart() {
    setStarting(true)
    try {
      await api.startCycle({
        dry_run: dryRun,
        limit: runConfig.limit,
        platforms: runConfig.platforms,
        discovery_mode: 'wide',
        pages_to_parse: runConfig.pages_to_parse,
        max_pages_per_query: runConfig.pages_to_parse,
        max_projects_per_cycle: runConfig.max_projects_per_cycle,
        max_parse_seconds: runConfig.max_parse_seconds,
        query_count: runConfig.query_count,
        top_projects: runConfig.top_projects,
        ai_score_mode: 'fast_full',
        ai_score_batch_size: 20,
        ai_score_max_candidates: 300,
        search_brief: runConfig.search_brief,
        browser_headless: runConfig.browser_headless,
        telegram_enabled: runConfig.telegram_enabled,
        osint_enabled: runConfig.osint_enabled,
        probiv_enabled: runConfig.probiv_enabled,
        session_hub_required: runConfig.session_hub_required,
      })
      showToast(dryRun ? 'Dry-run цикл запущен' : 'Реальный цикл запущен', true)
      setStatus((p) => ({ ...p, cycle_running: true }))
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Ошибка', false)
    } finally {
      setStarting(false)
    }
  }

  async function handleStop() {
    setStopping(true)
    try {
      await api.stopCycle()
      showToast('Цикл остановлен', true)
      setStatus((p) => ({ ...p, cycle_running: false }))
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Ошибка', false)
    } finally {
      setStopping(false)
    }
  }

  async function handleSetMode(mode: string) {
    setModeOpen(false)
    try {
      await api.setMode(mode)
      setStatus((p) => ({ ...p, execution_mode: mode }))
      showToast(`Режим: ${mode}`, true)
    } catch (e) {
      showToast(e instanceof Error ? e.message : 'Ошибка', false)
    }
  }

  const modeLabel: Record<string, string> = {
    auto: 'Авто', semi_auto: 'Полуавто', manual: 'Ручной', paused: 'Пауза'
  }

  function updateConfig<K extends keyof RunConfig>(key: K, value: RunConfig[K]) {
    setRunConfig((prev) => ({ ...prev, [key]: value }))
  }

  function togglePlatform(platform: PlatformId) {
    setRunConfig((prev) => {
      const hasPlatform = prev.platforms.includes(platform)
      if (hasPlatform && prev.platforms.length === 1) return prev
      const platforms = hasPlatform
        ? prev.platforms.filter((item) => item !== platform)
        : [...prev.platforms, platform]
      return { ...prev, platforms }
    })
  }

  const disabledControls = status.cycle_running || starting
  const telegramConfigured = status.runtime_config?.telegram_configured !== false
  const lastStats = status.last_cycle_stats ?? {}

  return (
    <div className="app-shell flex h-screen w-screen overflow-hidden bg-surface-900 max-lg:flex-col">
      {/* Sidebar */}
      <aside className="chrome-rail flex w-80 shrink-0 flex-col max-lg:h-[48vh] max-lg:w-full max-lg:border-b max-lg:border-r-0">
        {/* Logo area — also acts as drag region */}
        <div
          className="flex items-center gap-3 px-4 h-14 border-b border-white/10 select-none"
          style={{ WebkitAppRegion: 'drag' } as CSSProperties}
        >
          <span className="factory-mark">
            <Asterisk className="w-5 h-5" />
          </span>
          <div className="min-w-0">
            <div className="font-semibold text-sm tracking-wide text-white">PSR Desktop</div>
            <div className="mono-label leading-none">агент откликов</div>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {/* Navigation */}
          <nav className="px-2 py-3 space-y-1">
            {NAV.map(({ to, icon: Icon, label }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
                    isActive
                      ? 'bg-stone-100 text-black font-medium'
                      : 'text-zinc-400 hover:text-white hover:bg-surface-700'
                  )
                }
              >
                <Icon className="w-4 h-4 shrink-0" />
                {label}
              </NavLink>
            ))}
          </nav>

          {/* Control Panel */}
          <div className="p-3 border-t border-white/10 space-y-3">
            <div className="flex items-center justify-between">
              <span className="page-kicker">управление</span>
              <span className="mono-label">{dryRun ? 'тестовый' : 'боевой'}</span>
            </div>
            {/* Start / Stop */}
            {status.cycle_running ? (
              <button
                onClick={handleStop}
                disabled={stopping}
                className="btn btn-danger w-full justify-center"
              >
                {stopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Square className="w-3.5 h-3.5" />}
                Остановить
              </button>
            ) : (
              <button
                onClick={handleStart}
                disabled={starting || runConfig.platforms.length === 0}
                className="btn btn-primary w-full justify-center"
              >
                {starting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                Запустить цикл
              </button>
            )}

            {/* Mode selector */}
            <div className="relative">
              <button
                onClick={() => setModeOpen((v) => !v)}
              className="btn btn-ghost w-full justify-between text-xs"
              >
                <span className="text-zinc-400">Режим:</span>
                <span className="text-white font-medium">{modeLabel[status.execution_mode] ?? status.execution_mode}</span>
                <ChevronDown className={cn('w-3.5 h-3.5 transition-transform', modeOpen && 'rotate-180')} />
              </button>
              {modeOpen && (
                <div className="absolute top-full left-0 right-0 mt-1 bg-surface-700 border border-surface-600 rounded-lg shadow-xl z-50 overflow-hidden">
                  {MODES.map((m) => (
                    <button
                      key={m}
                      onClick={() => handleSetMode(m)}
                      className={cn(
                        'w-full text-left px-3 py-1.5 text-xs hover:bg-surface-600 transition-colors',
                        m === status.execution_mode ? 'text-brand-400 font-medium' : 'text-zinc-300'
                      )}
                    >
                      {modeLabel[m]}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <button
              onClick={() => setDryRun((v) => !v)}
              disabled={disabledControls}
              className={cn(
                'btn w-full justify-between text-xs',
                dryRun ? 'btn-ghost' : 'btn-danger'
              )}
            >
              <span>Dry-run</span>
              <span className="font-medium">{dryRun ? 'включён' : 'выключен'}</span>
            </button>

            <section className="space-y-2 pt-1">
              <div className="flex items-center gap-1.5 text-xs font-medium text-zinc-300">
                <Globe2 className="w-3.5 h-3.5 text-brand-400" />
                Платформы
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                {PLATFORM_OPTIONS.map((platform) => {
                  const checked = runConfig.platforms.includes(platform.id)
                  return (
                    <button
                      key={platform.id}
                      type="button"
                      disabled={disabledControls}
                      onClick={() => togglePlatform(platform.id)}
                      className={cn(
                        'rounded-lg border px-2 py-1.5 text-xs transition-colors',
                        checked
                          ? 'border-brand-500/40 bg-brand-600/20 text-white'
                          : 'border-surface-600 text-zinc-400 hover:text-white hover:bg-surface-700',
                        disabledControls && 'opacity-50 cursor-not-allowed'
                      )}
                    >
                      {platform.label}
                    </button>
                  )
                })}
              </div>
            </section>

            <section className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-medium text-zinc-300">
                <Search className="w-3.5 h-3.5 text-brand-400" />
                Поиск
              </div>
              <textarea
                value={runConfig.search_brief}
                disabled={disabledControls}
                onChange={(e) => updateConfig('search_brief', e.target.value)}
                className="input min-h-20 resize-none text-xs leading-relaxed"
              />
              <div className="grid grid-cols-2 gap-2">
                <NumberField
                  label="Запросов"
                  min={1}
                  max={50}
                  value={runConfig.query_count}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('query_count', value)}
                />
                <NumberField
                  label="Макс. страниц"
                  min={1}
                  max={100}
                  value={runConfig.pages_to_parse}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('pages_to_parse', value)}
                />
                <NumberField
                  label="Кандидатов"
                  min={20}
                  max={2000}
                  value={runConfig.max_projects_per_cycle}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('max_projects_per_cycle', value)}
                />
                <NumberField
                  label="Откликов"
                  min={1}
                  max={20}
                  value={runConfig.limit}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('limit', value)}
                />
              </div>
              <div className="hidden">
                <NumberField
                  label="Запросов"
                  min={1}
                  max={20}
                  value={runConfig.query_count}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('query_count', value)}
                />
                <NumberField
                  label="Глубина"
                  min={1}
                  max={10}
                  value={runConfig.pages_to_parse}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('pages_to_parse', value)}
                />
                <NumberField
                  label="В работу"
                  min={0}
                  max={50}
                  value={runConfig.top_projects}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('top_projects', value)}
                />
                <NumberField
                  label="Откликов"
                  min={1}
                  max={20}
                  value={runConfig.limit}
                  disabled={disabledControls}
                  onChange={(value) => updateConfig('limit', value)}
                />
              </div>
            </section>

            <section className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-medium text-zinc-300">
                <Monitor className="w-3.5 h-3.5 text-brand-400" />
                Рантайм
              </div>
              <div className="grid grid-cols-1 gap-1.5">
                <ToggleButton
                  label="Chrome"
                  checked={!runConfig.browser_headless}
                  disabled={disabledControls}
                  activeText="видимый"
                  inactiveText="без окна"
                  onChange={(checked) => updateConfig('browser_headless', !checked)}
                />
                <ToggleButton
                  label="Session Hub"
                  checked={runConfig.session_hub_required}
                  disabled={disabledControls}
                  activeText="строгий"
                  inactiveText="резерв"
                  onChange={(checked) => updateConfig('session_hub_required', checked)}
                />
              </div>
            </section>

            <section className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-medium text-zinc-300">
                <ShieldCheck className="w-3.5 h-3.5 text-brand-400" />
                Проверки
              </div>
              <div className="grid grid-cols-1 gap-1.5">
                <ToggleButton
                  label="OSINT"
                  checked={runConfig.osint_enabled}
                  disabled={disabledControls}
                  activeText="вкл"
                  onChange={(checked) => updateConfig('osint_enabled', checked)}
                />
                <ToggleButton
                  label="Probiv"
                  checked={runConfig.probiv_enabled}
                  disabled={disabledControls}
                  activeText="вкл"
                  onChange={(checked) => updateConfig('probiv_enabled', checked)}
                />
                <ToggleButton
                  label="Telegram"
                  checked={runConfig.telegram_enabled}
                  disabled={disabledControls}
                  activeText={telegramConfigured ? 'вкл' : 'нет id'}
                  inactiveText="выкл"
                  onChange={(checked) => updateConfig('telegram_enabled', checked)}
                />
              </div>
            </section>

            {/* Status indicator */}
            <div className={cn(
              'flex items-center gap-1.5 px-2 py-1 rounded-md text-xs border border-white/10 bg-black/20',
              status.cycle_running ? 'text-emerald-400' : status.last_error ? 'text-red-400' : 'text-zinc-500'
            )}>
              {status.cycle_running ? (
                <><Loader2 className="w-3 h-3 animate-spin" /> Цикл идёт</>
              ) : status.last_error ? (
                <><AlertCircle className="w-3 h-3" /> {status.last_error}</>
              ) : (
                <><CheckCircle2 className="w-3 h-3" /> Готов</>
              )}
            </div>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="workspace flex-1 flex min-w-0 flex-col overflow-hidden">
        <header
          className="topline flex h-14 shrink-0 items-center justify-between pl-6 pr-48 select-none max-lg:hidden"
          style={{ WebkitAppRegion: 'drag' } as CSSProperties}
        >
          <div>
            <div className="page-kicker">операции агента</div>
            <div className="text-sm text-stone-300">Единая панель запуска, очереди, Telegram и проверок</div>
          </div>
          <div
            className="flex max-w-[460px] items-center justify-end gap-2 overflow-hidden text-xs"
            style={{ WebkitAppRegion: 'no-drag' } as CSSProperties}
          >
            <span className="badge border-white/10 bg-white/[0.03] text-stone-300 max-xl:hidden">
              <span className={cn('status-dot mr-2', status.cycle_running && 'active')} />
              {status.cycle_running ? 'цикл идет' : 'готов'}
            </span>
            <span className="badge border-white/10 bg-white/[0.03] text-stone-300">
              <Activity className="mr-1.5 h-3.5 w-3.5 text-brand-300" />
              спарсено {lastStats.parsed ?? 0}
            </span>
            <span className="badge border-white/10 bg-white/[0.03] text-stone-300">
              TG {telegramConfigured ? 'готов' : 'не задан'}
            </span>
          </div>
        </header>
        <div className="flex-1 overflow-y-auto">
          <Outlet />
        </div>
      </main>

      {/* Toast */}
      {toast && (
        <div className={cn(
          'fixed bottom-4 right-4 flex items-center gap-2 px-4 py-2.5 rounded-xl shadow-2xl text-sm font-medium z-50 transition-all',
          toast.ok
            ? 'bg-emerald-600/90 text-white border border-emerald-500/50'
            : 'bg-red-600/90 text-white border border-red-500/50'
        )}>
          {toast.ok ? <CheckCircle2 className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
          {toast.msg}
        </div>
      )}
    </div>
  )
}
