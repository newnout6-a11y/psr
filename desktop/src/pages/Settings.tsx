import { useState, useEffect } from 'react'
import { Save, RefreshCw, Eye, EyeOff, Plus, Trash2, Send, Search, ExternalLink } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, FiltersData, KworkInspectProject } from '../lib/api'
import { cn } from '../lib/utils'

type Tab = 'general' | 'platforms' | 'execution' | 'filters' | 'telegram' | 'kwork' | 'osint_keys'

const BOOL_KEYS = new Set([
  'OSINT_ENABLED', 'BROWSER_HEADLESS', 'CONTINUOUS_MODE',
  'SESSION_HUB_REQUIRED', 'OSINT_WMN_FULL', 'TELEGRAM_DIGEST_ENABLED',
])

function BoolField({
  envKey, value, onChange,
}: { envKey: string; value: string; onChange: (key: string, val: string) => void }) {
  const on = value === 'true' || value === '1'
  return (
    <div className="flex items-center justify-between py-1">
      <span className="text-sm text-zinc-300">{envKey}</span>
      <button
        type="button"
        onClick={() => onChange(envKey, on ? 'false' : 'true')}
        className={cn(
          'relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors',
          on ? 'bg-brand-500' : 'bg-surface-600'
        )}
      >
        <span className={cn(
          'pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          on ? 'translate-x-4' : 'translate-x-0'
        )} />
      </button>
    </div>
  )
}

const ENV_GROUPS: Record<Tab, { label: string; keys: string[] }> = {
  general: {
    label: 'Общие / LLM',
    keys: ['GROQ_API_KEY', 'GOOGLE_API_KEY', 'GLM_API_KEY',
           'LLM_PROVIDER', 'LLM_FALLBACK', 'GROQ_MODEL', 'GOOGLE_MODEL', 'GLM_MODEL',
           'AI_SCORE_THRESHOLD', 'PLATFORMS', 'SEARCH_BRIEF', 'SEARCH_QUERY',
           'QUERY_COUNT', 'PAGES_TO_PARSE', 'TOP_PROJECTS',
           'PROXY_URL', 'BROWSER_HEADLESS', 'TIMEZONE_REGION'],
  },
  platforms: {
    label: 'Платформы / auth',
    keys: ['KWORK_EMAIL', 'KWORK_PASSWORD', 'KWORK_JWT_TOKEN',
           'KWORK_COOKIE_REMEMBERME', 'KWORK_COOKIE_USERID', 'KWORK_COOKIE_PHPSESSID',
           'KWORK_COOKIE_CSRF', 'KWORK_CSRF_TOKEN',
           'FREELANCE_RU_COOKIES_JSON', 'FREELANCE_RU_COOKIE_SESSION',
           'FREELANCE_RU_COOKIE_DUID', 'FREELANCE_RU_COOKIE_REMEMBER',
           'FL_RU_COOKIE_ID', 'FL_RU_COOKIE_PWD', 'FL_RU_COOKIE_SESSION', 'FL_RU_XSRF_TOKEN',
           'FREELANCER_OAUTH_TOKEN'],
  },
  execution: {
    label: 'Режим работы',
    keys: ['EXECUTION_MODE', 'CONTINUOUS_MODE', 'CYCLE_INTERVAL',
           'AUTO_SEND_SCORE_MIN', 'AUTO_SEND_VET_MIN',
           'AUTO_SEND_MAX_OFFERS', 'AUTO_SEND_MAX_BUDGET',
           'SESSION_HUB_URL', 'SESSION_HUB_REQUIRED'],
  },
  filters: {
    label: 'Фильтры',
    keys: [],
  },
  telegram: {
    label: 'Telegram',
    keys: ['TELEGRAM_TOKEN', 'ADMIN_CHAT_ID', 'TELEGRAM_TRANSPORT',
           'TELEGRAM_BOT_API_BASE', 'TELEGRAM_API_ID', 'TELEGRAM_API_HASH',
           'TELEGRAM_MTPROTO_SESSION', 'APPROVAL_TIMEOUT',
           'TELEGRAM_DIGEST_ENABLED', 'DIGEST_INTERVAL_MIN', 'TELEGRAM_QUEUE_PAGE_SIZE'],
  },
  kwork: {
    label: 'Kwork Inspector',
    keys: [],
  },
  osint_keys: {
    label: 'OSINT / API ключи',
    keys: ['OSINT_ENABLED', 'OSINT_PROVIDERS', 'OSINT_PROBIV_PROVIDERS',
           'OSINT_WMN_FULL',
           'GITHUB_TOKEN', 'EMAILREP_KEY', 'HIBP_API_KEY',
           'LEAKCHECK_API_KEY', 'INTELX_API_KEY', 'INTELX_BASE_URL',
           'INTELX_BUCKETS', 'INTELX_MAX_RESULTS', 'SHODAN_API_KEY'],
  },
}

function EnvField({
  envKey,
  value,
  isSecret,
  onChange,
}: {
  envKey: string
  value: string
  isSecret: boolean
  onChange: (key: string, val: string) => void
}) {
  const [show, setShow] = useState(!isSecret)
  useEffect(() => { if (isSecret) setShow(false) }, [isSecret])
  return (
    <div>
      <label className="label">{envKey}</label>
      <div className="flex gap-2">
        <input
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(envKey, e.target.value)}
          className="input flex-1 font-mono text-xs"
          placeholder={isSecret ? '••••••••' : ''}
        />
        {isSecret && (
          <button type="button" onClick={() => setShow((s) => !s)} className="btn btn-ghost py-1.5 px-2">
            {show ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
          </button>
        )}
      </div>
    </div>
  )
}

function FiltersTab({ data, onSave }: { data: FiltersData; onSave: (d: FiltersData) => Promise<void> }) {
  const [draft, setDraft] = useState<FiltersData>(data)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  function addItem(field: 'required_skills' | 'stop_words', val: string) {
    if (!val.trim()) return
    const arr = [...(draft[field] ?? []), val.trim()]
    setDraft({ ...draft, [field]: arr })
  }

  function removeItem(field: 'required_skills' | 'stop_words', idx: number) {
    const arr = [...(draft[field] ?? [])]
    arr.splice(idx, 1)
    setDraft({ ...draft, [field]: arr })
  }

  async function handleSave() {
    setSaving(true)
    try {
      await onSave(draft)
      setMsg('Сохранено')
      setTimeout(() => setMsg(''), 2000)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Ошибка')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Required skills */}
      <section>
        <h3 className="text-sm font-medium text-zinc-300 mb-3">Ключевые навыки</h3>
        <TagEditor
          items={draft.required_skills ?? []}
          onAdd={(v) => addItem('required_skills', v)}
          onRemove={(i) => removeItem('required_skills', i)}
          placeholder="+ добавить навык"
        />
      </section>

      <section>
        <h3 className="text-sm font-medium text-zinc-300 mb-3">Стоп-слова</h3>
        <TagEditor
          items={draft.stop_words ?? []}
          onAdd={(v) => addItem('stop_words', v)}
          onRemove={(i) => removeItem('stop_words', i)}
          placeholder="+ добавить стоп-слово"
        />
      </section>

      {/* Numeric fields */}
      <div className="grid grid-cols-3 gap-4">
        {(
          [
            ['min_budget', 'Мин. бюджет (₽)'],
            ['max_budget', 'Макс. бюджет (₽)'],
            ['min_client_score', 'Мин. vet score'],
            ['max_age_hours', 'Макс. возраст (ч)'],
            ['per_page', 'На странице'],
            ['max_proposals', 'Макс. откликов (0=любое)'],
          ] as [keyof FiltersData, string][]
        ).map(([key, lbl]) => (
          <div key={key}>
            <label className="label">{lbl}</label>
            <input
              type="number"
              value={String(draft[key] ?? '')}
              onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })}
              className="input"
            />
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={saving} className="btn btn-primary">
          {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Сохранить фильтры
        </button>
        {msg && <span className="text-xs text-emerald-400">{msg}</span>}
      </div>
    </div>
  )
}

function TagEditor({
  items,
  onAdd,
  onRemove,
  placeholder,
}: {
  items: string[]
  onAdd: (v: string) => void
  onRemove: (i: number) => void
  placeholder: string
}) {
  const [input, setInput] = useState('')
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {items.map((item, i) => (
          <span
            key={i}
            className="inline-flex items-center gap-1 px-2 py-0.5 bg-surface-700 border border-surface-600 rounded-md text-xs text-zinc-300"
          >
            {item}
            <button onClick={() => onRemove(i)} className="hover:text-red-400 transition-colors">
              <Trash2 className="w-3 h-3" />
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { onAdd(input); setInput('') }
          }}
          placeholder={placeholder}
          className="input w-48"
        />
        <button onClick={() => { onAdd(input); setInput('') }} className="btn btn-ghost">
          <Plus className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}

function KworkTab() {
  const [projectId, setProjectId] = useState('')
  const [status, setStatus] = useState<{
    configured: boolean
    api_ok: boolean
    api_error?: string | null
    session_hub_url: string
    session_hub_required: boolean
    state_parser: string
  } | null>(null)
  const [inspect, setInspect] = useState<{
    ok: boolean
    url: string
    state_found: boolean
    detail?: string
    project?: KworkInspectProject
  } | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(false)
  const [loadingInspect, setLoadingInspect] = useState(false)
  const [msg, setMsg] = useState('')

  async function refreshStatus() {
    setLoadingStatus(true)
    try {
      setStatus(await api.getKworkStatus())
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Ошибка Kwork status')
    } finally {
      setLoadingStatus(false)
    }
  }

  async function inspectProject() {
    if (!projectId.trim()) return
    setLoadingInspect(true)
    setMsg('')
    try {
      setInspect(await api.inspectKworkProject(projectId.trim()))
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Ошибка инспекции')
    } finally {
      setLoadingInspect(false)
    }
  }

  useEffect(() => {
    refreshStatus()
  }, [])

  const platformData = inspect?.project?.platform_data ?? {}
  const files = Array.isArray(platformData.files) ? platformData.files : []

  return (
    <div className="space-y-5">
      <div className="factory-panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <div className="page-kicker">kwork intelligence</div>
            <h3 className="text-sm font-semibold text-white">Диагностика Kwork</h3>
          </div>
          <button onClick={refreshStatus} disabled={loadingStatus} className="btn btn-ghost">
            <RefreshCw className={cn('h-4 w-4', loadingStatus && 'animate-spin')} />
            Обновить
          </button>
        </div>
        <div className="grid grid-cols-2 gap-3 text-sm max-lg:grid-cols-1">
          <div className="rounded-md border border-white/10 bg-black/20 p-3">
            <div className="mono-label">api</div>
            <div className={cn('mt-1 font-medium', status?.api_ok ? 'text-emerald-300' : 'text-red-300')}>
              {status?.api_ok ? 'подключен' : status?.configured ? 'ошибка' : 'не настроен'}
            </div>
            {status?.api_error && <div className="mt-1 text-xs text-red-300">{status.api_error}</div>}
          </div>
          <div className="rounded-md border border-white/10 bg-black/20 p-3">
            <div className="mono-label">session hub</div>
            <div className="mt-1 text-stone-200">{status?.session_hub_required ? 'строгий режим' : 'fallback разрешен'}</div>
            <div className="mt-1 text-xs text-zinc-500">{status?.session_hub_url}</div>
          </div>
        </div>
      </div>

      <div className="factory-panel p-4">
        <div className="mb-3">
          <div className="page-kicker">stateData</div>
          <h3 className="text-sm font-semibold text-white">Инспектор проекта</h3>
        </div>
        <div className="flex gap-2 max-lg:flex-col">
          <input
            value={projectId}
            onChange={(e) => setProjectId(e.target.value.replace(/[^\d]/g, ''))}
            onKeyDown={(e) => e.key === 'Enter' && inspectProject()}
            placeholder="ID проекта Kwork"
            className="input max-w-xs"
          />
          <button onClick={inspectProject} disabled={loadingInspect || !projectId.trim()} className="btn btn-primary">
            {loadingInspect ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            Проверить
          </button>
        </div>

        {msg && <div className="mt-3 text-sm text-red-300">{msg}</div>}

        {inspect && (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className={cn('badge', inspect.ok ? 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300' : 'border-red-500/30 bg-red-500/15 text-red-300')}>
                {inspect.ok ? 'stateData разобран' : 'не разобрано'}
              </span>
              <span className="badge border-white/10 bg-white/[0.03] text-stone-300">
                state {inspect.state_found ? 'найден' : 'не найден'}
              </span>
              <a href={inspect.url} target="_blank" rel="noreferrer" className="btn btn-ghost py-1 text-xs">
                <ExternalLink className="h-3.5 w-3.5" />
                Открыть
              </a>
            </div>

            {inspect.project ? (
              <div className="rounded-md border border-white/10 bg-black/20 p-3 text-sm">
                <div className="font-semibold text-white">{inspect.project.title}</div>
                <div className="mt-2 grid grid-cols-4 gap-2 text-xs text-zinc-400 max-lg:grid-cols-2">
                  <span>Бюджет: {inspect.project.budget ?? '—'}</span>
                  <span>Отклики: {inspect.project.offers_count ?? 0}</span>
                  <span>Нанимает: {inspect.project.client_hired_percent ?? 0}%</span>
                  <span>User ID: {inspect.project.client_user_id ?? '—'}</span>
                </div>
                <p className="mt-3 line-clamp-4 text-xs text-zinc-300">{inspect.project.description}</p>
                <div className="mt-3 text-xs text-zinc-500">
                  Файлы: {files.length} · parser: {String(platformData.source ?? status?.state_parser ?? 'stateData')}
                </div>
              </div>
            ) : (
              <div className="text-sm text-zinc-400">{inspect.detail}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default function Settings() {
  const [activeTab, setActiveTab] = useState<Tab>('general')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [telegramTesting, setTelegramTesting] = useState(false)
  const [telegramMsg, setTelegramMsg] = useState('')

  const { data: envData, refetch: refetchEnv } = useApi(() => api.getEnv(), [])
  const { data: filtersData, refetch: refetchFilters } = useApi(() => api.getFilters(), [])

  const [envDraft, setEnvDraft] = useState<Record<string, string>>({})
  const secretKeys = new Set(envData?.secret_keys ?? [])

  const envValues = { ...(envData?.values ?? {}), ...envDraft }

  async function handleSaveEnv() {
    if (Object.keys(envDraft).length === 0) return
    setSaving(true)
    try {
      await api.updateEnv(envDraft)
      setMsg('Настройки сохранены')
      setEnvDraft({})
      refetchEnv()
      setTimeout(() => setMsg(''), 2500)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Ошибка')
    } finally {
      setSaving(false)
    }
  }

  async function handleSaveFilters(data: FiltersData) {
    await api.updateFilters(data)
    refetchFilters()
  }

  async function handleTelegramTest() {
    setTelegramTesting(true)
    setTelegramMsg('')
    try {
      const result = await api.testTelegram({ text: 'PSR тест: доставка Telegram работает.' })
      const last = result.attempts[result.attempts.length - 1]
      setTelegramMsg(result.ok ? `Отправлено через ${result.method}` : `Ошибка: ${last?.detail ?? result.detail}`)
    } catch (e) {
      setTelegramMsg(e instanceof Error ? e.message : 'Ошибка Telegram')
    } finally {
      setTelegramTesting(false)
    }
  }

  const tabs = (Object.keys(ENV_GROUPS) as Tab[])

  return (
    <div className="flex h-full max-lg:flex-col">
      {/* Tab sidebar */}
      <div className="w-44 shrink-0 border-r border-white/10 bg-surface-800/80 pt-4 max-lg:flex max-lg:w-full max-lg:overflow-x-auto max-lg:border-b max-lg:border-r-0 max-lg:pt-0">
        {tabs.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              'w-full text-left px-4 py-2 text-sm transition-colors max-lg:w-auto max-lg:shrink-0 max-lg:whitespace-nowrap',
              activeTab === tab
                ? 'text-brand-400 bg-brand-600/10 border-r-2 border-brand-500'
                : 'text-zinc-400 hover:text-white hover:bg-surface-700'
            )}
          >
            {ENV_GROUPS[tab].label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-2xl space-y-4">
          {activeTab === 'kwork' ? (
            <KworkTab />
          ) : activeTab === 'filters' ? (
            filtersData?.data ? (
              <FiltersTab data={filtersData.data} onSave={handleSaveFilters} />
            ) : (
              <p className="text-zinc-500 text-sm">Загрузка...</p>
            )
          ) : (
            <>
              <div className="mb-4 flex items-center justify-between max-lg:flex-col max-lg:items-start max-lg:gap-3">
                <div>
                  <div className="page-kicker">настройки</div>
                  <h2 className="text-base font-semibold text-white">{ENV_GROUPS[activeTab].label}</h2>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {msg && <span className="text-xs text-emerald-400">{msg}</span>}
                  {activeTab === 'telegram' && telegramMsg && (
                    <span className={cn(
                      'text-xs',
                      telegramMsg.startsWith('Отправлено') ? 'text-emerald-400' : 'text-red-400'
                    )}>
                      {telegramMsg}
                    </span>
                  )}
                  {activeTab === 'telegram' && (
                    <button onClick={handleTelegramTest} disabled={telegramTesting} className="btn btn-ghost">
                      {telegramTesting ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                      Тест
                    </button>
                  )}
                  <button onClick={handleSaveEnv} disabled={saving || Object.keys(envDraft).length === 0} className="btn btn-primary">
                    {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    Сохранить
                  </button>
                </div>
              </div>

              <div className="space-y-3">
                {ENV_GROUPS[activeTab].keys.map((key) => (
                  BOOL_KEYS.has(key) ? (
                    <BoolField
                      key={key}
                      envKey={key}
                      value={envValues[key] ?? 'false'}
                      onChange={(k, v) => setEnvDraft((prev) => ({ ...prev, [k]: v }))}
                    />
                  ) : (
                    <EnvField
                      key={key}
                      envKey={key}
                      value={envValues[key] ?? ''}
                      isSecret={secretKeys.has(key)}
                      onChange={(k, v) => setEnvDraft((prev) => ({ ...prev, [k]: v }))}
                    />
                  )
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
