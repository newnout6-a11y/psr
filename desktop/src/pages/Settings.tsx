import { useState, useEffect } from 'react'
import { Save, RefreshCw, Eye, EyeOff, Plus, Trash2, Send, Search, ExternalLink, Power, PowerOff, PlugZap, RotateCcw, ShieldCheck, Wifi } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, FiltersData, KworkInspectProject, NetworkStatus } from '../lib/api'
import { cn } from '../lib/utils'

type Tab = 'general' | 'platforms' | 'execution' | 'network' | 'filters' | 'telegram' | 'kwork' | 'osint_keys'

const BOOL_KEYS = new Set([
  'OSINT_ENABLED', 'BROWSER_HEADLESS', 'CONTINUOUS_MODE',
  'SESSION_HUB_REQUIRED', 'OSINT_WMN_FULL', 'TELEGRAM_DIGEST_ENABLED',
  'OPENAI_DISABLE_RESPONSE_STORAGE', 'ATTACHMENT_CONTEXT_ENABLED',
  'PROPOSAL_IMAGE_ENABLED', 'KWORK_IMAGE_ATTACH_CONFIRMED',
  'KWORK_AUTO_APPROVE_ORDERS', 'KWORK_FAST_INBOX_POLLING',
  'KWORK_AUTO_REVIEW', 'KWORK_AUTO_PAUSE_KWORKS',
  'SCRAPE_COMPETITOR_PRICES',
  'KWORK_PACING', 'KWORK_TLS_IMPERSONATE',
  'KWORK_MARKET_USE_PROXY', 'KWORK_MARKET_PROXY_STRICT',
  'VPNTE_PROXY_ENABLED', 'VPNTE_PROXY_ROTATE_ON_NEXT', 'VPNTE_PROXY_STRICT',
])

const PROVIDER_OPTIONS = ['auto', 'deepseek', 'openai', 'groq']
const DEEPSEEK_MODEL_OPTIONS = ['deepseek-v4-pro', 'deepseek-v4-flash']

function isDeepSeekModel(value: string) {
  return DEEPSEEK_MODEL_OPTIONS.includes(value)
}

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
    keys: ['OPENAI_API_KEY', 'OPENAI_API_KEYS', 'OPENAI_BASE_URL', 'OPENAI_API_PREFIX', 'OPENAI_WIRE_API',
           'OPENAI_REASONING_EFFORT', 'OPENAI_DISABLE_RESPONSE_STORAGE',
           'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_API_PREFIX', 'DEEPSEEK_WIRE_API',
           'DEEPSEEK_REASONING_EFFORT', 'DEEPSEEK_THINKING',
           'DEEPSEEK_MODEL_QUERY_GENERATION', 'DEEPSEEK_MODEL_SCORING',
           'GROQ_API_KEY',
           'AI_SCORE_THRESHOLD', 'AI_SCORE_MODE', 'AI_SCORE_BATCH_SIZE', 'AI_SCORE_DEEPSEEK_BATCH_SIZE', 'AI_SCORE_MAX_CANDIDATES',
           'PLATFORMS', 'SEARCH_BRIEF', 'SEARCH_QUERY', 'DISCOVERY_MODE',
           'ATTACHMENT_CONTEXT_ENABLED', 'ATTACHMENT_MAX_FILES',
           'ATTACHMENT_MAX_BYTES', 'ATTACHMENT_MAX_CHARS',
           'PROPOSAL_IMAGE_ENABLED', 'PROPOSAL_IMAGE_MODEL',
           'PROPOSAL_IMAGE_MIN_AI_SCORE', 'PROPOSAL_IMAGE_MIN_VET_SCORE', 'KWORK_IMAGE_ATTACH_CONFIRMED',
           'QUERY_COUNT', 'PAGES_TO_PARSE', 'MAX_PAGES_PER_QUERY',
           'MAX_PROJECTS_PER_CYCLE', 'MAX_PARSE_SECONDS', 'TOP_PROJECTS',
           'BROWSER_HEADLESS', 'TIMEZONE_REGION'],
  },
  platforms: {
    label: 'Платформы / auth',
    keys: ['KWORK_EMAIL', 'KWORK_PASSWORD', 'KWORK_PHONE', 'KWORK_JWT_TOKEN',
           'KWORK_COOKIE_REMEMBERME', 'KWORK_COOKIE_USERID', 'KWORK_COOKIE_PHPSESSID',
           'KWORK_COOKIE_CSRF', 'KWORK_CSRF_TOKEN',
           'CATCHMAIL_DOMAIN', 'CATCHMAIL_API_BASE_URL', 'FIRSTMAIL_API_KEY',
           'KWORK_REGISTRATION_MAIL_TIMEOUT', 'KWORK_REGISTRATION_MAIL_POLL_INTERVAL',
           'KWORK_REGISTRATION_MAIL_INITIAL_DELAY',
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
  network: {
    label: 'Сеть / VPNTE',
    keys: ['PROXY_URL',
           'VPNTE_PROXY_ENABLED', 'VPNTE_PROXY_ROTATE_ON_NEXT', 'VPNTE_PROXY_STRICT',
           'VPNTE_PROXY_COUNTRY', 'VPNTE_PROXY_PROFILE_ID', 'VPNTE_PROXY_SLOT', 'VPNTE_PROXY_PORT',
           'VPNTE_PROXY_TIMEOUT', 'VPNTE_PROXY_HEALTH_TIMEOUT', 'VPNTE_PROXY_HEALTH_WARMUP', 'VPNTE_PROXY_CACHE_TTL',
           'KWORK_REGISTRATION_ROUTE_TIMEOUT', 'KWORK_REGISTRATION_ROUTE_CONCURRENCY',
           'VPNTE_CONTROL_URL', 'VPNTE_CONTROL_TOKEN',
           'SESSION_HUB_URL', 'SESSION_HUB_REQUIRED', 'KWORK_SESSION_HUB_COOKIE_TTL',
            'KWORK_PACING', 'KWORK_PACE_MIN', 'KWORK_PACE_MAX',
            'KWORK_BURST_LIMIT', 'KWORK_BURST_WINDOW',
            'KWORK_MARKET_USE_PROXY', 'KWORK_MARKET_PROXY_STRICT',
            'KWORK_MARKET_API_TIMEOUT', 'KWORK_MARKET_SEED_DISCOVERY_TIMEOUT',
            'KWORK_PROXY_LIST'],
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
    label: 'Kwork',
    keys: [],
  },
  osint_keys: {
    label: 'Client Signals / API',
    keys: ['OSINT_ENABLED', 'OSINT_PROVIDERS', 'OSINT_PROBIV_PROVIDERS',
           'OSINT_WMN_FULL',
           'GITHUB_TOKEN', 'EMAILREP_KEY', 'HIBP_API_KEY',
           'LEAKCHECK_API_KEY', 'INTELX_API_KEY', 'INTELX_BASE_URL',
           'INTELX_BUCKETS', 'INTELX_MAX_RESULTS', 'SHODAN_API_KEY'],
  },
}

function EnvField({
  envKey,
  label,
  value,
  isSecret,
  onChange,
  onReveal,
}: {
  envKey: string
  label?: string
  value: string
  isSecret: boolean
  onChange: (key: string, val: string) => void
  onReveal?: (key: string) => Promise<void>
}) {
  const [show, setShow] = useState(!isSecret)
  const [revealing, setRevealing] = useState(false)
  useEffect(() => { if (isSecret) setShow(false) }, [isSecret])

  async function handleToggleSecret() {
    if (show) {
      setShow(false)
      return
    }
    if (value === '********' && onReveal) {
      setRevealing(true)
      try {
        await onReveal(envKey)
      } finally {
        setRevealing(false)
      }
    }
    setShow(true)
  }

  return (
    <div>
      <label className="label">{label ?? envKey}</label>
      <div className="flex gap-2">
        <input
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(envKey, e.target.value)}
          className="input flex-1 font-mono text-xs"
          placeholder={isSecret ? '••••••••' : ''}
        />
        {isSecret && (
          <button
            type="button"
            onClick={handleToggleSecret}
            disabled={revealing}
            className="btn btn-ghost py-1.5 px-2"
          >
            {revealing ? (
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
            ) : show ? (
              <EyeOff className="w-3.5 h-3.5" />
            ) : (
              <Eye className="w-3.5 h-3.5" />
            )}
          </button>
        )}
      </div>
    </div>
  )
}

function SelectEnvField({
  label,
  envKey,
  value,
  options,
  onChange,
}: {
  label: string
  envKey: string
  value: string
  options: string[]
  onChange: (key: string, val: string) => void
}) {
  return (
    <div>
      <label className="label">{label}</label>
      <select
        value={value || options[0]}
        onChange={(e) => onChange(envKey, e.target.value)}
        className="input"
      >
        {options.map((option) => (
          <option key={`${envKey}-${option || 'default'}`} value={option}>
            {option || 'provider default'}
          </option>
        ))}
      </select>
    </div>
  )
}

function StaticEnvField({
  label,
  value,
}: {
  label: string
  value: string
}) {
  return (
    <div>
      <label className="label">{label}</label>
      <div className="input flex items-center text-sm text-zinc-400">{value}</div>
    </div>
  )
}

function providerModelKey(provider: string) {
  if (provider === 'deepseek') return 'DEEPSEEK_MODEL'
  if (provider === 'openai') return 'OPENAI_MODEL'
  if (provider === 'groq') return 'GROQ_MODEL'
  return ''
}

function providerModelValue(provider: string, values: Record<string, string>) {
  const key = providerModelKey(provider)
  return key ? (values[key] ?? '') : ''
}

function isModelCompatible(provider: string, model: string) {
  const normalized = model.trim().toLowerCase()
  if (!normalized) return true
  if (provider === 'deepseek') return normalized.startsWith('deepseek-')
  if (provider === 'openai') {
    return !normalized.startsWith('deepseek-')
      && !normalized.startsWith('llama-')
      && !normalized.startsWith('mixtral-')
      && !normalized.startsWith('gemma-')
  }
  if (provider === 'groq') return !normalized.startsWith('deepseek-') && !normalized.startsWith('gpt-')
  return true
}

function RouteModelField({
  label,
  provider,
  values,
  onChange,
  parser = false,
  modelKey = '',
}: {
  label: string
  provider: string
  values: Record<string, string>
  onChange: (key: string, val: string) => void
  parser?: boolean
  modelKey?: string
}) {
  const parserModel = values.PARSER_LLM_MODEL ?? ''

  if (provider === 'auto') {
    return <StaticEnvField label={label} value="provider default" />
  }

  if (modelKey) {
    if (provider === 'deepseek') {
      return (
        <SelectEnvField
          label={label}
          envKey={modelKey}
          value={values[modelKey] ?? ''}
          options={['', ...DEEPSEEK_MODEL_OPTIONS]}
          onChange={onChange}
        />
      )
    }

    return (
      <EnvField
        label={label}
        envKey={modelKey}
        value={values[modelKey] ?? ''}
        isSecret={false}
        onChange={onChange}
      />
    )
  }

  if (parser) {
    if (provider === 'deepseek') {
      return (
        <SelectEnvField
          label={label}
          envKey="PARSER_LLM_MODEL"
          value={isDeepSeekModel(parserModel) ? parserModel : ''}
          options={['', ...DEEPSEEK_MODEL_OPTIONS]}
          onChange={onChange}
        />
      )
    }

    return (
      <EnvField
        label={label}
        envKey="PARSER_LLM_MODEL"
        value={isModelCompatible(provider, parserModel) && parserModel ? parserModel : providerModelValue(provider, values)}
        isSecret={false}
        onChange={onChange}
      />
    )
  }

  if (provider === 'deepseek') {
    return (
      <SelectEnvField
        label={label}
        envKey="DEEPSEEK_MODEL"
        value={values.DEEPSEEK_MODEL ?? 'deepseek-v4-pro'}
        options={DEEPSEEK_MODEL_OPTIONS}
        onChange={onChange}
      />
    )
  }

  const defaultModelKey = providerModelKey(provider)
  if (!defaultModelKey) {
    return <StaticEnvField label={label} value="provider default" />
  }

  return (
    <EnvField
      label={label}
      envKey={defaultModelKey}
      value={values[defaultModelKey] ?? ''}
      isSecret={false}
      onChange={onChange}
    />
  )
}

function LlmRoutingPanel({
  values,
  onChange,
}: {
  values: Record<string, string>
  onChange: (key: string, val: string) => void
}) {
  const proposalProvider = values.PROPOSAL_WRITING_PROVIDER || 'openai'
  const queryProvider = values.QUERY_GENERATION_PROVIDER || 'deepseek'
  const scoringProvider = values.SCORING_PROVIDER || 'deepseek'
  const fallbackProvider = values.LLM_PROVIDER || 'openai'
  const proposalModel = providerModelValue(proposalProvider, values)

  function handleProviderChange(key: string, val: string) {
    onChange(key, val)
    if (key === 'QUERY_GENERATION_PROVIDER' && val !== 'deepseek') {
      onChange('DEEPSEEK_MODEL_QUERY_GENERATION', '')
    }
    if (key === 'SCORING_PROVIDER' && val !== 'deepseek') {
      onChange('DEEPSEEK_MODEL_SCORING', '')
    }
  }

  return (
    <section className="factory-panel space-y-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="page-kicker">llm routing</div>
          <h3 className="text-sm font-semibold text-white">Кто сейчас отвечает</h3>
        </div>
        <div className="rounded-md border border-brand-500/20 bg-brand-500/10 px-2 py-1 text-xs text-brand-200">
          отклики: {proposalProvider}{proposalModel ? ` / ${proposalModel}` : ''}
        </div>
      </div>

      <div className="grid grid-cols-4 gap-4 max-xl:grid-cols-2 max-lg:grid-cols-1">
        <div className="space-y-3">
          <SelectEnvField
            label="Отклики"
            envKey="PROPOSAL_WRITING_PROVIDER"
            value={proposalProvider}
            options={PROVIDER_OPTIONS}
            onChange={handleProviderChange}
          />
          <RouteModelField label="Model" provider={proposalProvider} values={values} onChange={onChange} />
        </div>

        <div className="space-y-3">
          <SelectEnvField
            label="Запросы"
            envKey="QUERY_GENERATION_PROVIDER"
            value={queryProvider}
            options={PROVIDER_OPTIONS}
            onChange={handleProviderChange}
          />
          <RouteModelField
            label="Model"
            provider={queryProvider}
            values={values}
            onChange={onChange}
            modelKey="DEEPSEEK_MODEL_QUERY_GENERATION"
          />
        </div>

        <div className="space-y-3">
          <SelectEnvField
            label="Скоринг"
            envKey="SCORING_PROVIDER"
            value={scoringProvider}
            options={PROVIDER_OPTIONS}
            onChange={handleProviderChange}
          />
          <RouteModelField
            label="Model"
            provider={scoringProvider}
            values={values}
            onChange={onChange}
            modelKey="DEEPSEEK_MODEL_SCORING"
          />
        </div>

        <div className="space-y-3">
          <SelectEnvField
            label="Запасной"
            envKey="LLM_PROVIDER"
            value={fallbackProvider}
            options={PROVIDER_OPTIONS.filter((option) => option !== 'auto')}
            onChange={handleProviderChange}
          />
          <RouteModelField label="Model" provider={fallbackProvider} values={values} onChange={onChange} />
        </div>
      </div>
    </section>
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
      <section className="factory-panel p-4">
        <h3 className="text-sm font-medium text-zinc-300 mb-3">Ключевые навыки</h3>
        <TagEditor
          items={draft.required_skills ?? []}
          onAdd={(v) => addItem('required_skills', v)}
          onRemove={(i) => removeItem('required_skills', i)}
          placeholder="+ добавить навык"
        />
      </section>

      <section className="factory-panel p-4">
        <h3 className="text-sm font-medium text-zinc-300 mb-3">Стоп-слова</h3>
        <TagEditor
          items={draft.stop_words ?? []}
          onAdd={(v) => addItem('stop_words', v)}
          onRemove={(i) => removeItem('stop_words', i)}
          placeholder="+ добавить стоп-слово"
        />
      </section>

      {/* Numeric fields */}
      <section className="factory-panel p-4">
        <h3 className="text-sm font-medium text-zinc-300 mb-3">Бюджет, возраст, конкуренция, клиент</h3>
      <div className="grid grid-cols-3 gap-4 max-lg:grid-cols-1">
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
      </section>

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

function NetworkStatusCard({
  title,
  ok,
  detail,
  meta,
}: {
  title: string
  ok: boolean
  detail: string
  meta?: string
}) {
  return (
    <div className="rounded-md border border-white/10 bg-black/20 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="mono-label">{title}</div>
        <span className={cn(
          'rounded-md border px-2 py-0.5 text-xs',
          ok ? 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300' : 'border-red-500/30 bg-red-500/15 text-red-300'
        )}>
          {ok ? 'ok' : 'error'}
        </span>
      </div>
      <div className="mt-2 break-all text-sm text-stone-200">{detail || '—'}</div>
      {meta && <div className="mt-1 break-all text-xs text-zinc-500">{meta}</div>}
    </div>
  )
}

function NetworkTab({
  values,
  secretKeys,
  onChange,
  onReveal,
  onSave,
  saving,
  dirty,
}: {
  values: Record<string, string>
  secretKeys: Set<string>
  onChange: (key: string, val: string) => void
  onReveal: (key: string) => Promise<void>
  onSave: () => Promise<void>
  saving: boolean
  dirty: boolean
}) {
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState('')
  const [msg, setMsg] = useState('')

  async function refreshStatus(probe = true) {
    setLoading(true)
    try {
      setStatus(await api.getNetworkStatus(probe))
      setMsg('')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Network status error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refreshStatus(true)
  }, [])

  function applyKworkSafeMode() {
    const updates: Record<string, string> = {
      VPNTE_PROXY_ENABLED: 'true',
      VPNTE_PROXY_ROTATE_ON_NEXT: 'false',
      VPNTE_PROXY_STRICT: 'true',
      VPNTE_PROXY_SLOT: values.VPNTE_PROXY_SLOT || '',
      VPNTE_PROXY_PORT: values.VPNTE_PROXY_PORT || '17990',
      VPNTE_PROXY_CACHE_TTL: values.VPNTE_PROXY_CACHE_TTL || '60',
      VPNTE_PROXY_TIMEOUT: values.VPNTE_PROXY_TIMEOUT || '10',
      VPNTE_PROXY_HEALTH_TIMEOUT: values.VPNTE_PROXY_HEALTH_TIMEOUT || '20',
      VPNTE_PROXY_HEALTH_WARMUP: values.VPNTE_PROXY_HEALTH_WARMUP || '1',
      KWORK_REGISTRATION_ROUTE_TIMEOUT: values.KWORK_REGISTRATION_ROUTE_TIMEOUT || '10',
      KWORK_REGISTRATION_ROUTE_CONCURRENCY: values.KWORK_REGISTRATION_ROUTE_CONCURRENCY || '12',
      KWORK_PACING: 'true',
      KWORK_PACE_MIN: '4',
      KWORK_PACE_MAX: '9',
      KWORK_BURST_LIMIT: '4',
      KWORK_BURST_WINDOW: '90',
      KWORK_SESSION_HUB_COOKIE_TTL: '180',
    }
    Object.entries(updates).forEach(([key, value]) => onChange(key, value))
    setMsg('Щадящий режим подготовлен. Сохрани настройки или нажми старт/ротацию.')
  }

  function actionPayload() {
    const rawPort = Number(values.VPNTE_PROXY_PORT || 0)
    const rawSlot = Number(values.VPNTE_PROXY_SLOT || 0)
    const profileId = values.VPNTE_PROXY_PROFILE_ID || undefined
    return {
      slot: rawSlot > 0 ? rawSlot : undefined,
      country: values.VPNTE_PROXY_COUNTRY || undefined,
      profile_id: profileId,
      id: profileId,
      port: rawPort > 0 ? rawPort : undefined,
    }
  }

  type VpnteActionKind = 'start' | 'rotate' | 'connect' | 'trigger' | 'stop'

  async function runVpnteAction(kind: VpnteActionKind) {
    setAction(kind)
    try {
      if (dirty) {
        await onSave()
      }
      const payload = actionPayload()
      if (kind === 'stop' && !payload.slot) {
        throw new Error('Укажи слот VPNTE перед остановкой.')
      }
      const result = kind === 'start'
        ? await api.startVpnteProxy(payload)
        : kind === 'rotate'
          ? await api.rotateVpnteProxy(payload)
          : kind === 'connect'
            ? await api.connectVpnteProxy(payload)
            : kind === 'trigger'
              ? await api.triggerVpnteProxy(payload)
              : await api.stopVpnteProxy({ slot: payload.slot! })
      setStatus(result.network)
      const successMessage: Record<VpnteActionKind, string> = {
        start: 'VPNTE запущен',
        rotate: 'VPNTE профиль обновлён',
        connect: 'VPNTE подключил выбранный профиль',
        trigger: 'VPNTE профиль перезапущен',
        stop: 'VPNTE слот остановлен',
      }
      setMsg(result.ok ? successMessage[kind] : result.detail || 'VPNTE action failed')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'VPNTE action error')
    } finally {
      setAction('')
    }
  }

  const vpnte = status?.vpnte
  const hub = status?.session_hub
  const currentProxy = vpnte?.proxy_url || ''
  const liveInstanceCount = vpnte?.instances?.length ?? 0
  const liveInstances = vpnte?.instances ?? []
  const rawSlot = Number(values.VPNTE_PROXY_SLOT || 0)
  const hasProfileTarget = rawSlot > 0 && Boolean(values.VPNTE_PROXY_PROFILE_ID?.trim())

  function selectInstance(instance: Record<string, unknown>) {
    const slot = Number(instance.slot ?? 0)
    if (Number.isInteger(slot) && slot > 0) onChange('VPNTE_PROXY_SLOT', String(slot))
    const profileId = String(instance.profileId ?? '').trim()
    if (profileId) onChange('VPNTE_PROXY_PROFILE_ID', profileId)
    const country = String(instance.country ?? '').trim()
    if (country) onChange('VPNTE_PROXY_COUNTRY', country)
    setMsg(`Выбран слот ${slot || '—'}${profileId ? ` · профиль ${profileId}` : ''}`)
  }

  const networkKeys = [
    'VPNTE_PROXY_ENABLED', 'VPNTE_PROXY_ROTATE_ON_NEXT', 'VPNTE_PROXY_STRICT',
    'VPNTE_PROXY_COUNTRY', 'VPNTE_PROXY_PROFILE_ID', 'VPNTE_PROXY_SLOT', 'VPNTE_PROXY_PORT',
    'VPNTE_PROXY_TIMEOUT', 'VPNTE_PROXY_HEALTH_TIMEOUT', 'VPNTE_PROXY_HEALTH_WARMUP', 'VPNTE_PROXY_CACHE_TTL',
    'KWORK_REGISTRATION_ROUTE_TIMEOUT', 'KWORK_REGISTRATION_ROUTE_CONCURRENCY',
    'VPNTE_CONTROL_URL', 'VPNTE_CONTROL_TOKEN', 'PROXY_URL',
  ]
  const sessionKeys = ['SESSION_HUB_URL', 'SESSION_HUB_REQUIRED', 'KWORK_SESSION_HUB_COOKIE_TTL']
  const pacingKeys = [
    'KWORK_PACING', 'KWORK_PACE_MIN', 'KWORK_PACE_MAX',
    'KWORK_REGISTRATION_PACE_MIN', 'KWORK_REGISTRATION_PACE_MAX', 'KWORK_REGISTRATION_BURST_LIMIT',
    'KWORK_BURST_LIMIT', 'KWORK_BURST_WINDOW', 'KWORK_PROXY_LIST',
  ]
  const marketKeys = ['KWORK_MARKET_USE_PROXY', 'KWORK_MARKET_PROXY_STRICT', 'KWORK_MARKET_API_TIMEOUT', 'KWORK_MARKET_SEED_DISCOVERY_TIMEOUT']
  const marketSupplyKeys = ['KWORK_MARKET_SUPPLY_MIN_CARDS', 'KWORK_MARKET_SUPPLY_MAX_REQUESTS', 'KWORK_MARKET_SUPPLY_PROXY_PORTS']
  const marketAssistantKeys = [
    'KWORK_MARKET_ASSISTANT_MAP_CHARS', 'KWORK_MARKET_ASSISTANT_MAP_CONCURRENCY',
    'KWORK_MARKET_ASSISTANT_MAP_ATTEMPTS', 'KWORK_MARKET_ASSISTANT_LLM_TIMEOUT_SECONDS',
    'KWORK_MARKET_ASSISTANT_MAP_MODEL', 'KWORK_MARKET_ASSISTANT_REDUCE_MODEL',
    'KWORK_MARKET_ASSISTANT_MAP_REASONING', 'KWORK_MARKET_ASSISTANT_REDUCE_REASONING',
  ]

  const renderEnv = (key: string) => BOOL_KEYS.has(key) ? (
    <BoolField key={key} envKey={key} value={values[key] ?? 'false'} onChange={onChange} />
  ) : (
    <EnvField
      key={key}
      envKey={key}
      value={values[key] ?? ''}
      isSecret={secretKeys.has(key)}
      onChange={onChange}
      onReveal={onReveal}
    />
  )

  return (
    <div className="space-y-5">
      <section className="factory-panel p-4">
        <div className="mb-4 flex items-start justify-between gap-3 max-lg:flex-col">
          <div>
            <div className="page-kicker">network control</div>
            <h3 className="text-sm font-semibold text-white">Сеть, VPNTE и Session Hub</h3>
          </div>
          <div className="flex flex-wrap gap-2">
            <button onClick={() => refreshStatus(true)} disabled={loading} className="btn btn-ghost">
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
              Обновить
            </button>
            <button onClick={onSave} disabled={saving || !dirty} className="btn btn-primary">
              {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Сохранить
            </button>
            <button onClick={() => runVpnteAction('start')} disabled={!!action} className="btn btn-ghost">
              {action === 'start' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Power className="h-4 w-4" />}
              Старт
            </button>
            <button onClick={() => runVpnteAction('rotate')} disabled={!!action} className="btn btn-ghost">
              {action === 'rotate' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              Ротация
            </button>
            <button onClick={() => runVpnteAction('connect')} disabled={!!action || !hasProfileTarget} className="btn btn-ghost">
              {action === 'connect' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
              Подключить профиль
            </button>
            <button onClick={() => runVpnteAction('trigger')} disabled={!!action || !hasProfileTarget} className="btn btn-ghost">
              {action === 'trigger' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
              Перезапустить
            </button>
            <button onClick={() => runVpnteAction('stop')} disabled={!!action || rawSlot <= 0} className="btn btn-ghost">
              {action === 'stop' ? <RefreshCw className="h-4 w-4 animate-spin" /> : <PowerOff className="h-4 w-4" />}
              Остановить
            </button>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3 text-sm max-xl:grid-cols-1">
          <NetworkStatusCard
            title="VPNTE"
            ok={!vpnte?.enabled || Boolean(vpnte?.ok && vpnte.running)}
            detail={vpnte?.enabled ? (vpnte.running ? 'работает' : 'включён, но не запущен') : 'выключен'}
            meta={currentProxy || vpnte?.detail || `live /instances: ${liveInstanceCount}`}
          />
          <NetworkStatusCard
            title="Session Hub"
            ok={Boolean(hub?.ok)}
            detail={hub?.ok ? 'локальный источник кук доступен' : hub?.detail || 'нет ответа'}
            meta={hub ? `${hub.health_url} · proxy isolated: ${hub.proxy_isolated ? 'yes' : 'no'}` : ''}
          />
          <NetworkStatusCard
            title="Kwork traffic"
            ok={Boolean(values.KWORK_PACING === 'true' || values.KWORK_PACING === '1')}
            detail={values.VPNTE_PROXY_ENABLED === 'true' || values.VPNTE_PROXY_ENABLED === '1' ? 'через VPNTE proxy' : 'без VPNTE'}
            meta={`pace ${values.KWORK_PACE_MIN || '—'}-${values.KWORK_PACE_MAX || '—'}s · burst ${values.KWORK_BURST_LIMIT || '—'}/${values.KWORK_BURST_WINDOW || '—'}s`}
          />
        </div>

        <div className="mt-4 overflow-hidden rounded-md border border-white/10 bg-black/20">
          <div className="flex items-center justify-between gap-3 border-b border-white/10 px-3 py-2">
            <div className="mono-label">live instances</div>
            <span className="text-xs text-zinc-500">{liveInstanceCount} запущено</span>
          </div>
          <div className="max-h-72 overflow-auto">
            {liveInstances.length ? (
              <table className="w-full min-w-[680px] text-left text-xs">
                <thead className="sticky top-0 bg-[#171717] text-zinc-500">
                  <tr>
                    <th className="px-3 py-2 font-medium">Слот</th>
                    <th className="px-3 py-2 font-medium">Профиль</th>
                    <th className="px-3 py-2 font-medium">Страна</th>
                    <th className="px-3 py-2 font-medium">proxyUrl</th>
                    <th className="px-3 py-2 font-medium">PID</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {liveInstances.map((instance, index) => {
                    const slot = Number(instance.slot ?? 0)
                    const profileId = String(instance.profileId ?? '')
                    const profileName = String(instance.profileName ?? '')
                    const proxyUrl = String(instance.proxyUrl ?? '')
                    return (
                      <tr key={`${slot || 'instance'}-${profileId || index}`} className="text-zinc-300 hover:bg-white/5">
                        <td className="px-3 py-2 align-top">
                          <button type="button" className="font-mono text-brand-300 hover:text-brand-200" onClick={() => selectInstance(instance)}>
                            {slot || '—'}
                          </button>
                        </td>
                        <td className="max-w-56 px-3 py-2 align-top">
                          <div className="truncate text-zinc-200">{profileName || '—'}</div>
                          <div className="truncate font-mono text-[11px] text-zinc-500">{profileId || '—'}</div>
                        </td>
                        <td className="px-3 py-2 align-top">{String(instance.country ?? '—')}</td>
                        <td className="max-w-64 px-3 py-2 align-top font-mono text-zinc-400">{proxyUrl || '—'}</td>
                        <td className="px-3 py-2 align-top font-mono text-zinc-500">{String(instance.pid ?? '—')}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            ) : (
              <div className="px-3 py-4 text-xs text-zinc-500">Нет подтверждённых live-инстансов в /instances.</div>
            )}
          </div>
        </div>

        {msg && <div className="mt-3 text-sm text-zinc-300">{msg}</div>}

        <button onClick={applyKworkSafeMode} className="btn btn-ghost mt-4">
          <ShieldCheck className="h-4 w-4" />
          Щадящий Kwork
        </button>
      </section>

      <section className="factory-panel p-4">
        <div className="mb-3 flex items-center gap-2">
          <Wifi className="h-4 w-4 text-brand-300" />
          <h3 className="text-sm font-semibold text-white">VPNTE proxy</h3>
        </div>
        <div className="space-y-3">{networkKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <h3 className="mb-3 text-sm font-semibold text-white">Session Hub</h3>
        <div className="space-y-3">{sessionKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <h3 className="mb-3 text-sm font-semibold text-white">Kwork pacing</h3>
        <div className="space-y-3">{pacingKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <h3 className="mb-3 text-sm font-semibold text-white">Kwork Market</h3>
        <div className="space-y-3">{marketKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <h3 className="mb-3 text-sm font-semibold text-white">Market supply scan</h3>
        <div className="space-y-3">{marketSupplyKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <h3 className="mb-3 text-sm font-semibold text-white">Market assistant</h3>
        <div className="space-y-3">{marketAssistantKeys.map(renderEnv)}</div>
      </section>
    </div>
  )
}

function KworkTab({
  values,
  secretKeys,
  onChange,
  onReveal,
  onSave,
  saving,
  dirty,
}: {
  values: Record<string, string>
  secretKeys: Set<string>
  onChange: (key: string, value: string) => void
  onReveal: (key: string) => Promise<void>
  onSave: () => Promise<void>
  saving: boolean
  dirty: boolean
}) {
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
  const automationKeys = [
    'KWORK_AUTO_APPROVE_ORDERS', 'KWORK_FAST_INBOX_POLLING', 'KWORK_INBOX_POLL_INTERVAL',
    'KWORK_RESPONSE_TIME_ALERT_HOURS', 'KWORK_SUCCESS_RATE_WARN', 'KWORK_SUCCESS_RATE_BLOCK',
    'KWORK_BUSY_THRESHOLD', 'KWORK_AUTO_PAUSE_KWORKS', 'KWORK_AUTO_REVIEW',
    'KWORK_AUTO_REVIEW_RATING', 'KWORK_AUTO_REVIEW_TEXT', 'KWORK_CONNECTS_WARN',
    'KWORK_CONNECTS_BLOCK', 'SCRAPE_COMPETITOR_PRICES',
  ]
  const coverImageKeys = [
    'KWORK_COVER_IMAGE_API_KEY', 'KWORK_COVER_IMAGE_BASE_URL', 'KWORK_COVER_IMAGE_API_PREFIX',
    'KWORK_COVER_IMAGE_CONN', 'KWORK_COVER_IMAGE_MODEL', 'KWORK_COVER_IMAGE_SIZE',
    'KWORK_COVER_IMAGE_QUALITY', 'KWORK_COVER_IMAGE_TIMEOUT', 'KWORK_COVER_VISION_PROVIDER',
    'KWORK_COVER_VISION_MODEL', 'KWORK_COVER_VISION_MAX_TOKENS', 'KWORK_COVER_PROMPT_PROVIDER',
    'KWORK_COVER_PROMPT_MODEL',
  ]
  const renderEnv = (key: string) => BOOL_KEYS.has(key) ? (
    <BoolField key={key} envKey={key} value={values[key] ?? 'false'} onChange={onChange} />
  ) : (
    <EnvField
      key={key}
      envKey={key}
      value={values[key] ?? ''}
      isSecret={secretKeys.has(key)}
      onChange={onChange}
      onReveal={onReveal}
    />
  )

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

      <section className="factory-panel p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <div className="page-kicker">kwork controls</div>
            <h3 className="text-sm font-semibold text-white">Automation and account health</h3>
          </div>
          <button onClick={onSave} disabled={saving || !dirty} className="btn btn-primary">
            {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Save
          </button>
        </div>
        <div className="space-y-3">{automationKeys.map(renderEnv)}</div>
      </section>

      <section className="factory-panel p-4">
        <div className="mb-3">
          <div className="page-kicker">cover image</div>
          <h3 className="text-sm font-semibold text-white">Kwork cover generation</h3>
        </div>
        <div className="space-y-3">{coverImageKeys.map(renderEnv)}</div>
      </section>

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
  const [revealedEnv, setRevealedEnv] = useState<Record<string, string>>({})
  const secretKeys = new Set(envData?.secret_keys ?? [])

  const envValues = { ...(envData?.values ?? {}), ...revealedEnv, ...envDraft }

  async function revealSecret(key: string) {
    const result = await api.revealEnvValue(key)
    setRevealedEnv((prev) => ({ ...prev, [key]: result.value }))
  }

  async function handleSaveEnv() {
    if (Object.keys(envDraft).length === 0) return
    setSaving(true)
    try {
      await api.updateEnv(envDraft)
      setMsg('Настройки сохранены')
      setEnvDraft({})
      setRevealedEnv({})
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
            <KworkTab
              values={envValues}
              secretKeys={secretKeys}
              onChange={(k, v) => setEnvDraft((prev) => ({ ...prev, [k]: v }))}
              onReveal={handleReveal}
              onSave={handleSaveEnv}
              saving={saving}
              dirty={Object.keys(envDraft).length > 0}
            />
          ) : activeTab === 'network' ? (
            <NetworkTab
              values={envValues}
              secretKeys={secretKeys}
              onChange={(k, v) => setEnvDraft((prev) => ({ ...prev, [k]: v }))}
              onReveal={revealSecret}
              onSave={handleSaveEnv}
              saving={saving}
              dirty={Object.keys(envDraft).length > 0}
            />
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
                {activeTab === 'general' && (
                  <LlmRoutingPanel
                    values={envValues}
                    onChange={(k, v) => setEnvDraft((prev) => ({ ...prev, [k]: v }))}
                  />
                )}
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
                      onReveal={revealSecret}
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
