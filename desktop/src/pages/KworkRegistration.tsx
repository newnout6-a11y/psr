import { useEffect, useState, type FormEvent } from 'react'
import {
  AlertCircle,
  Briefcase,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  MailCheck,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import {
  api,
  type KworkRegistrationAccount,
  type KworkRegistrationAccountSessionCheck,
  type KworkRegistrationBatchResult,
  type KworkRegistrationCredentials,
  type KworkRegistrationRequest,
  type KworkRegistrationResult,
  type KworkRegistrationSessionCheck,
  type NetworkStatus,
} from '../lib/api'
import { cn } from '../lib/utils'

const INITIAL_FORM: KworkRegistrationRequest = {
  email: '',
  mail_provider: 'catchmail',
  mail_password: '',
  user_type: 1,
  promo: '',
  use_simple: false,
  track_client_id: '',
  action_after: '',
  is_subscribed: false,
  captcha_token: '',
  captcha_field: 'smart-token',
  firstmail_api_key: '',
  dry_run: false,
}

const REGISTRATION_FEEDBACK_STORAGE_KEY = 'psr.kwork-registration.feedback.v1'
const REGISTRATION_FEEDBACK_EVENT = 'psr:kwork-registration-feedback'

interface RegistrationFeedback {
  submitting: boolean
  result: KworkRegistrationResult | null
  batchResult: KworkRegistrationBatchResult | null
  error: string
}

const EMPTY_REGISTRATION_FEEDBACK: RegistrationFeedback = {
  submitting: false,
  result: null,
  batchResult: null,
  error: '',
}

function readRegistrationFeedback(): RegistrationFeedback {
  try {
    const raw = window.sessionStorage.getItem(REGISTRATION_FEEDBACK_STORAGE_KEY)
    if (!raw) return EMPTY_REGISTRATION_FEEDBACK
    const parsed = JSON.parse(raw) as Partial<RegistrationFeedback>
    return {
      submitting: parsed.submitting === true,
      result: parsed.result ?? null,
      batchResult: parsed.batchResult ?? null,
      error: typeof parsed.error === 'string' ? parsed.error : '',
    }
  } catch {
    return EMPTY_REGISTRATION_FEEDBACK
  }
}

function saveRegistrationFeedback(feedback: RegistrationFeedback) {
  window.sessionStorage.setItem(REGISTRATION_FEEDBACK_STORAGE_KEY, JSON.stringify(feedback))
  window.dispatchEvent(new Event(REGISTRATION_FEEDBACK_EVENT))
}

function clearRegistrationFeedback() {
  window.sessionStorage.removeItem(REGISTRATION_FEEDBACK_STORAGE_KEY)
  window.dispatchEvent(new Event(REGISTRATION_FEEDBACK_EVENT))
}

const STATUS_COPY: Record<string, { label: string; detail: string; tone: 'emerald' | 'amber' | 'red' | 'zinc' }> = {
  preflight_ok: {
    label: 'Пред-проверка пройдена',
    detail: 'Поля приняты, запрос на регистрацию не отправлялся.',
    tone: 'emerald',
  },
  signup_submitted: {
    label: 'Регистрация отправлена',
    detail: 'Активировать почту не удалось в рамках этого запуска.',
    tone: 'amber',
  },
  activation_pending: {
    label: 'Ожидается активация',
    detail: 'Регистрация принята, но подтверждение из почты ещё не получено.',
    tone: 'amber',
  },
  activated: {
    label: 'Аккаунт активирован',
    detail: 'Подтверждённая активация Kwork завершена.',
    tone: 'emerald',
  },
  captcha_required: {
    label: 'Нужна CAPTCHA',
    detail: 'Добавьте токен CAPTCHA и повторите запуск.',
    tone: 'amber',
  },
  signup_failed: {
    label: 'Регистрация отклонена',
    detail: 'Kwork не подтвердил создание аккаунта.',
    tone: 'red',
  },
  activation_failed: {
    label: 'Активация не подтверждена',
    detail: 'Ссылка активации не дала подтверждённый результат.',
    tone: 'red',
  },
}

function statusCopy(status: string) {
  return STATUS_COPY[status] ?? {
    label: status || 'Статус неизвестен',
    detail: 'Сервер вернул состояние, для которого ещё нет подписи.',
    tone: 'zinc' as const,
  }
}

function registrationError(error: unknown) {
  const raw = error instanceof Error ? error.message : 'Не удалось выполнить регистрацию.'
  const payloadStart = raw.indexOf(': {')
  if (payloadStart < 0) return raw

  try {
    const body = JSON.parse(raw.slice(payloadStart + 2)) as { detail?: unknown; message?: unknown }
    if (typeof body.detail === 'string') return body.detail
    if (body.detail && typeof body.detail === 'object') {
      const detail = body.detail as { message?: unknown }
      if (typeof detail.message === 'string') return detail.message
    }
    if (typeof body.message === 'string') return body.message
  } catch {}

  return raw
}

function formatAccountTimestamp(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(date)
}

function ResultIcon({ tone }: { tone: 'emerald' | 'amber' | 'red' | 'zinc' }) {
  if (tone === 'emerald') return <CheckCircle2 className="h-5 w-5 text-emerald-400" />
  if (tone === 'red') return <AlertCircle className="h-5 w-5 text-red-400" />
  if (tone === 'amber') return <AlertCircle className="h-5 w-5 text-amber-400" />
  return <ShieldCheck className="h-5 w-5 text-zinc-400" />
}

export default function KworkRegistration() {
  const [restoredFeedback] = useState(readRegistrationFeedback)
  const [form, setForm] = useState<KworkRegistrationRequest>(INITIAL_FORM)
  const [submitting, setSubmitting] = useState(restoredFeedback.submitting)
  const [result, setResult] = useState<KworkRegistrationResult | null>(restoredFeedback.result)
  const [batchResult, setBatchResult] = useState<KworkRegistrationBatchResult | null>(restoredFeedback.batchResult)
  const [credentials, setCredentials] = useState<KworkRegistrationCredentials | null>(null)
  const [batchCredentials, setBatchCredentials] = useState<Record<string, KworkRegistrationCredentials>>({})
  const [error, setError] = useState(restoredFeedback.error)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [showMailPassword, setShowMailPassword] = useState(false)
  const [showRegistrationPassword, setShowRegistrationPassword] = useState(false)
  const [accountCount, setAccountCount] = useState(1)
  const [avoidUsedIps, setAvoidUsedIps] = useState(false)
  const [networkStatus, setNetworkStatus] = useState<NetworkStatus | null>(null)
  const [vpnteLoading, setVpnteLoading] = useState(false)
  const [selectedVpnteSlots, setSelectedVpnteSlots] = useState<number[]>([])
  const [storedAccounts, setStoredAccounts] = useState<KworkRegistrationAccount[]>([])
  const [storedAccountsLoading, setStoredAccountsLoading] = useState(true)
  const [storedAccountsError, setStoredAccountsError] = useState('')
  const [selectedStoredAccountIds, setSelectedStoredAccountIds] = useState<string[]>([])
  const [storedAccountCredentials, setStoredAccountCredentials] = useState<Record<string, KworkRegistrationCredentials>>({})
  const [visibleStoredAccountPasswords, setVisibleStoredAccountPasswords] = useState<string[]>([])
  const [storedAccountSessionChecks, setStoredAccountSessionChecks] = useState<Record<string, KworkRegistrationSessionCheck>>({})
  const [storedAccountAction, setStoredAccountAction] = useState<string | null>(null)
  const usesFirstmail = form.mail_provider === 'firstmail'
  const liveVpnteInstances = (networkStatus?.vpnte.instances ?? []).flatMap((instance) => {
    const slot = Number(instance.slot ?? 0)
    const proxyUrl = String(instance.proxyUrl ?? '')
    if (!Number.isInteger(slot) || slot < 1 || !proxyUrl || instance.running !== true) return []
    return [{
      slot,
      country: String(instance.country ?? '').trim(),
      profileName: String(instance.profileName ?? instance.profileId ?? '').trim(),
    }]
  })
  const canSubmit = Boolean(
    (!usesFirstmail || (form.email?.trim() && form.mail_password?.trim() && accountCount === 1))
    && (accountCount === 1 || !form.email?.trim())
    && selectedVpnteSlots.length >= accountCount
    && !vpnteLoading
    && !submitting,
  )
  const missingVpnteSlots = Math.max(accountCount - selectedVpnteSlots.length, 0)
  const reserveVpnteSlots = Math.max(selectedVpnteSlots.length - accountCount, 0)
  const currentStatus = result ? statusCopy(result.status) : null
  const activatedStoredAccountCount = storedAccounts.filter((account) => account.status === 'activated').length
  const allStoredAccountsSelected = storedAccounts.length > 0
    && storedAccounts.every((account) => selectedStoredAccountIds.includes(account.registration_id))

  const refreshVpnteInstances = async () => {
    setVpnteLoading(true)
    try {
      const nextNetwork = await api.getNetworkStatus(true)
      setNetworkStatus(nextNetwork)
      const slots = (nextNetwork.vpnte.instances ?? []).flatMap((instance) => {
        const slot = Number(instance.slot ?? 0)
        return Number.isInteger(slot) && slot > 0 && instance.running === true && String(instance.proxyUrl ?? '') ? [slot] : []
      })
      setSelectedVpnteSlots((current) => current.filter((slot) => slots.includes(slot)).length ? current.filter((slot) => slots.includes(slot)) : slots)
    } catch (nextError) {
      setError(registrationError(nextError))
    } finally {
      setVpnteLoading(false)
    }
  }

  const refreshStoredAccounts = async () => {
    setStoredAccountsLoading(true)
    try {
      const next = await api.listKworkRegistrationAccounts()
      const registrationIds = new Set(next.accounts.map((account) => account.registration_id))
      setStoredAccounts(next.accounts)
      setSelectedStoredAccountIds((current) => current.filter((registrationId) => registrationIds.has(registrationId)))
      setVisibleStoredAccountPasswords((current) => current.filter((registrationId) => registrationIds.has(registrationId)))
      setStoredAccountCredentials((current) => Object.fromEntries(
        Object.entries(current).filter(([registrationId]) => registrationIds.has(registrationId)),
      ))
      setStoredAccountSessionChecks((current) => Object.fromEntries(
        Object.entries(current).filter(([registrationId]) => registrationIds.has(registrationId)),
      ))
      setStoredAccountsError('')
    } catch (nextError) {
      setStoredAccountsError(registrationError(nextError))
    } finally {
      setStoredAccountsLoading(false)
    }
  }

  const removeStoredAccountsFromView = (registrationIds: string[]) => {
    const removed = new Set(registrationIds)
    setStoredAccounts((current) => current.filter((account) => !removed.has(account.registration_id)))
    setSelectedStoredAccountIds((current) => current.filter((registrationId) => !removed.has(registrationId)))
    setVisibleStoredAccountPasswords((current) => current.filter((registrationId) => !removed.has(registrationId)))
    setStoredAccountCredentials((current) => Object.fromEntries(
      Object.entries(current).filter(([registrationId]) => !removed.has(registrationId)),
    ))
    setStoredAccountSessionChecks((current) => Object.fromEntries(
      Object.entries(current).filter(([registrationId]) => !removed.has(registrationId)),
    ))
  }

  const handleStoredAccountPassword = async (registrationId: string) => {
    if (visibleStoredAccountPasswords.includes(registrationId)) {
      setVisibleStoredAccountPasswords((current) => current.filter((item) => item !== registrationId))
      return
    }
    if (storedAccountCredentials[registrationId]) {
      setVisibleStoredAccountPasswords((current) => [...current, registrationId])
      return
    }

    setStoredAccountAction(`credentials:${registrationId}`)
    setStoredAccountsError('')
    try {
      const credentialsForAccount = await api.getKworkRegistrationCredentials(registrationId)
      setStoredAccountCredentials((current) => ({ ...current, [registrationId]: credentialsForAccount }))
      setVisibleStoredAccountPasswords((current) => [...current, registrationId])
    } catch (nextError) {
      setStoredAccountsError(registrationError(nextError))
    } finally {
      setStoredAccountAction(null)
    }
  }

  const handleStoredAccountSessionCheck = async (registrationId: string) => {
    setStoredAccountAction(`session:${registrationId}`)
    setStoredAccountsError('')
    try {
      const next: KworkRegistrationAccountSessionCheck = await api.checkKworkRegistrationAccountSession(registrationId)
      const { session_check: sessionCheck, ...updatedAccount } = next
      setStoredAccounts((current) => current.map((account) => (
        account.registration_id === registrationId ? { ...account, ...updatedAccount } : account
      )))
      setStoredAccountSessionChecks((current) => ({ ...current, [registrationId]: sessionCheck }))
    } catch (nextError) {
      setStoredAccountsError(registrationError(nextError))
    } finally {
      setStoredAccountAction(null)
    }
  }

  const handleStoredAccountActivation = async (account: KworkRegistrationAccount) => {
    const registrationId = account.registration_id
    setStoredAccountAction(`activation:${registrationId}`)
    setStoredAccountsError('')
    try {
      const nextResult = await api.verifyKworkRegistration({
        registration_id: registrationId,
        mail_password: account.mail_provider === 'firstmail' ? form.mail_password?.trim() : '',
        firstmail_api_key: form.firstmail_api_key?.trim(),
      })
      if (result?.registration_id === registrationId) {
        setResult(nextResult)
      }
      await refreshStoredAccounts()
    } catch (nextError) {
      setStoredAccountsError(registrationError(nextError))
    } finally {
      setStoredAccountAction(null)
    }
  }

  const handleStoredAccountDelete = async (registrationId: string) => {
    const account = storedAccounts.find((item) => item.registration_id === registrationId)
    const label = account?.username || account?.email || registrationId
    if (!window.confirm(`Удалить ${label} только из локальной базы? Аккаунт на Kwork останется.`)) return

    setStoredAccountAction(`delete:${registrationId}`)
    setStoredAccountsError('')
    try {
      await api.deleteKworkRegistrationAccount(registrationId)
      removeStoredAccountsFromView([registrationId])
    } catch (nextError) {
      setStoredAccountsError(registrationError(nextError))
    } finally {
      setStoredAccountAction(null)
    }
  }

  const handleSelectedStoredAccountsDelete = async () => {
    const registrationIds = [...selectedStoredAccountIds]
    if (!registrationIds.length) return
    if (!window.confirm(`Удалить ${registrationIds.length} аккаунт(а/ов) только из локальной базы? Аккаунты на Kwork останутся.`)) return

    setStoredAccountAction('delete:selected')
    setStoredAccountsError('')
    const removed: string[] = []
    const failed: string[] = []
    for (const registrationId of registrationIds) {
      try {
        await api.deleteKworkRegistrationAccount(registrationId)
        removed.push(registrationId)
      } catch {
        failed.push(registrationId)
      }
    }
    if (removed.length) removeStoredAccountsFromView(removed)
    if (failed.length) {
      setStoredAccountsError(`Не удалось удалить ${failed.length} из ${registrationIds.length} локальных записей.`)
    }
    setStoredAccountAction(null)
  }

  useEffect(() => {
    void refreshVpnteInstances()
  }, [])

  useEffect(() => {
    void refreshStoredAccounts()
  }, [])

  useEffect(() => {
    const restoreFeedback = () => {
      const feedback = readRegistrationFeedback()
      setSubmitting(feedback.submitting)
      setResult(feedback.result)
      setBatchResult(feedback.batchResult)
      setError(feedback.error)
      setCredentials(null)
      setBatchCredentials({})
      setShowRegistrationPassword(false)
    }

    window.addEventListener(REGISTRATION_FEEDBACK_EVENT, restoreFeedback)
    return () => window.removeEventListener(REGISTRATION_FEEDBACK_EVENT, restoreFeedback)
  }, [])

  const loadCredentials = async (registrationId: string) => {
    const nextCredentials = await api.getKworkRegistrationCredentials(registrationId)
    setCredentials(nextCredentials)
    setShowRegistrationPassword(true)
  }

  const loadBatchCredentials = async (items: KworkRegistrationResult[]) => {
    const settled = await Promise.allSettled(
      items
        .filter((item) => item.registration_id)
        .map(async (item) => api.getKworkRegistrationCredentials(item.registration_id!)),
    )
    const next: Record<string, KworkRegistrationCredentials> = {}
    for (const item of settled) {
      if (item.status === 'fulfilled') next[item.value.registration_id] = item.value
    }
    setBatchCredentials(next)
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return

    const pendingFeedback: RegistrationFeedback = {
      submitting: true,
      result: null,
      batchResult: null,
      error: '',
    }
    saveRegistrationFeedback(pendingFeedback)
    setSubmitting(true)
    setError('')
    setResult(null)
    setBatchResult(null)
    setCredentials(null)
    setBatchCredentials({})
    setShowRegistrationPassword(false)
    try {
      const payload = {
        ...form,
        email: form.email?.trim() ?? '',
        promo: form.promo?.trim(),
        track_client_id: form.track_client_id?.trim(),
        action_after: form.action_after?.trim(),
        captcha_token: form.captcha_token?.trim(),
        firstmail_api_key: form.firstmail_api_key?.trim(),
        mail_password: usesFirstmail ? form.mail_password?.trim() : '',
      }
      const nextBatch = await api.registerKworkAccountsBatch({
        ...payload,
        account_count: accountCount,
        vpnte_slots: selectedVpnteSlots,
        avoid_used_ips: avoidUsedIps,
      })
      const nextResult = nextBatch.results[nextBatch.results.length - 1] || null
      saveRegistrationFeedback({
        submitting: false,
        result: nextResult,
        batchResult: nextBatch,
        error: '',
      })
      setBatchResult(nextBatch)
      setResult(nextResult)
      await loadBatchCredentials(nextBatch.results)
      if (nextResult?.registration_id) {
        try {
          await loadCredentials(nextResult.registration_id)
        } catch {
          // The registration result remains usable even if credentials cannot be loaded immediately.
        }
      }
      if (accountCount === 1 && nextResult?.email && !form.email?.trim()) {
        setForm((prev) => ({ ...prev, email: nextResult.email }))
      }
      if (nextResult?.captcha_required) setAdvancedOpen(true)
      void refreshStoredAccounts()
    } catch (nextError) {
      const message = registrationError(nextError)
      saveRegistrationFeedback({
        submitting: false,
        result: null,
        batchResult: null,
        error: message,
      })
      setError(message)
    } finally {
      setSubmitting(false)
    }
  }

  const clearFeedback = () => {
    clearRegistrationFeedback()
    setResult(null)
    setBatchResult(null)
    setCredentials(null)
    setBatchCredentials({})
    setError('')
    setShowRegistrationPassword(false)
  }

  const handleRegistrationPassword = async () => {
    if (!result?.registration_id || submitting) return
    if (credentials?.registration_id === result.registration_id) {
      setShowRegistrationPassword((value) => !value)
      return
    }

    setSubmitting(true)
    setError('')
    try {
      await loadCredentials(result.registration_id)
    } catch (nextError) {
      setError(registrationError(nextError))
    } finally {
      setSubmitting(false)
    }
  }

  const handleVerifyActivation = async () => {
    if (!result?.registration_id || submitting) return

    setSubmitting(true)
    setError('')
    try {
      const nextResult = await api.verifyKworkRegistration({
        registration_id: result.registration_id,
        mail_password: usesFirstmail ? form.mail_password?.trim() : '',
        firstmail_api_key: form.firstmail_api_key?.trim(),
      })
      saveRegistrationFeedback({
        submitting: false,
        result: nextResult,
        batchResult,
        error: '',
      })
      setResult(nextResult)
      void refreshStoredAccounts()
    } catch (nextError) {
      const message = registrationError(nextError)
      saveRegistrationFeedback({
        submitting: false,
        result,
        batchResult,
        error: message,
      })
      setError(message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-5 p-6 max-lg:p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="page-kicker">kwork accounts</div>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-white">
            <UserPlus className="h-5 w-5 text-brand-400" />
            Регистрация Kwork
          </h1>
        </div>
        <span className={cn(
          'badge',
          form.dry_run
            ? 'border-amber-500/30 bg-amber-500/10 text-amber-200'
            : 'border-white/10 bg-white/[0.03] text-stone-300',
        )}>
          {form.dry_run ? 'пред-проверка' : 'создание аккаунта'}
        </span>
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(320px,0.72fr)]">
        <form onSubmit={handleSubmit} className="factory-panel p-5">
          <fieldset disabled={submitting} className="space-y-5">
            <div className="border-b border-white/10 pb-4">
              <span className="label">Почтовый провайдер</span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => setForm((prev) => ({ ...prev, mail_provider: 'catchmail', mail_password: '' }))}
                  className={cn(
                    'flex min-h-11 items-center gap-2 rounded-md border px-3 text-left text-sm transition-colors',
                    form.mail_provider === 'catchmail'
                      ? 'border-brand-500/50 bg-brand-600/15 text-white'
                      : 'border-surface-600 bg-surface-900/50 text-zinc-400 hover:text-white',
                  )}
                >
                  <Mail className="h-4 w-4" />
                  CatchMail
                </button>
                <button
                  type="button"
                  onClick={() => setForm((prev) => ({ ...prev, mail_provider: 'firstmail' }))}
                  className={cn(
                    'flex min-h-11 items-center gap-2 rounded-md border px-3 text-left text-sm transition-colors',
                    form.mail_provider === 'firstmail'
                      ? 'border-brand-500/50 bg-brand-600/15 text-white'
                      : 'border-surface-600 bg-surface-900/50 text-zinc-400 hover:text-white',
                  )}
                >
                  <Mail className="h-4 w-4" />
                  Firstmail
                </button>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <label className="block">
                <span className="label">{usesFirstmail ? 'Email Firstmail' : 'Email CatchMail'}</span>
                <span className="relative block">
                  <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                  <input
                    type="email"
                    autoComplete="email"
                    value={form.email}
                    onChange={(event) => setForm((prev) => ({ ...prev, email: event.target.value }))}
                    className="input pl-9"
                    placeholder={usesFirstmail ? 'name@firstmail.ltd' : 'оставьте пустым для нового адреса'}
                    required={usesFirstmail}
                  />
                </span>
              </label>

              {usesFirstmail ? <label className="block">
                <span className="label">Пароль Firstmail</span>
                <span className="relative block">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                  <input
                    type={showMailPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    value={form.mail_password}
                    onChange={(event) => setForm((prev) => ({ ...prev, mail_password: event.target.value }))}
                    className="input px-9"
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowMailPassword((value) => !value)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-zinc-500 hover:text-white"
                    title={showMailPassword ? 'Скрыть пароль Firstmail' : 'Показать пароль Firstmail'}
                    aria-label={showMailPassword ? 'Скрыть пароль Firstmail' : 'Показать пароль Firstmail'}
                  >
                    {showMailPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </span>
              </label> : <div className="self-end pb-2 text-sm text-zinc-400">
                CatchMail не требует пароля или ключа.
              </div>}

            </div>

            <div className="border-t border-white/10 pt-4">
              <span className="label">Тип аккаунта</span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => setForm((prev) => ({ ...prev, user_type: 1 }))}
                  className={cn(
                    'flex min-h-11 items-center gap-2 rounded-md border px-3 text-left text-sm transition-colors',
                    form.user_type === 1
                      ? 'border-brand-500/50 bg-brand-600/15 text-white'
                      : 'border-surface-600 bg-surface-900/50 text-zinc-400 hover:text-white',
                  )}
                >
                  <Users className="h-4 w-4" />
                  Покупатель
                </button>
                <button
                  type="button"
                  onClick={() => setForm((prev) => ({ ...prev, user_type: 2 }))}
                  className={cn(
                    'flex min-h-11 items-center gap-2 rounded-md border px-3 text-left text-sm transition-colors',
                    form.user_type === 2
                      ? 'border-brand-500/50 bg-brand-600/15 text-white'
                      : 'border-surface-600 bg-surface-900/50 text-zinc-400 hover:text-white',
                  )}
                >
                  <Briefcase className="h-4 w-4" />
                  Продавец
                </button>
              </div>
            </div>

            <div className="border-t border-white/10 pt-4">
              <div className="grid gap-4 md:grid-cols-[180px_minmax(0,1fr)]">
                <label className="block">
                  <span className="label">Количество аккаунтов</span>
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={accountCount}
                    onChange={(event) => setAccountCount(Math.max(1, Math.min(100, Number(event.target.value) || 1)))}
                    className="input font-mono"
                  />
                </label>
                <div>
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <span className="label">VPNTE Control API</span>
                    <button
                      type="button"
                      onClick={() => void refreshVpnteInstances()}
                      disabled={vpnteLoading}
                      className="rounded p-1 text-zinc-400 hover:text-white disabled:opacity-50"
                      title="Обновить VPNTE-инстансы"
                      aria-label="Обновить VPNTE-инстансы"
                    >
                      <RotateCcw className={cn('h-4 w-4', vpnteLoading && 'animate-spin')} />
                    </button>
                  </div>
                  {liveVpnteInstances.length ? (
                    <div className="grid gap-2 sm:grid-cols-2">
                      {liveVpnteInstances.map((instance) => {
                        const selected = selectedVpnteSlots.includes(instance.slot)
                        return (
                          <button
                            key={instance.slot}
                            type="button"
                            onClick={() => setSelectedVpnteSlots((current) => (
                              selected ? current.filter((slot) => slot !== instance.slot) : [...current, instance.slot]
                            ))}
                            className={cn(
                              'min-h-11 rounded-md border px-3 text-left text-sm transition-colors',
                              selected
                                ? 'border-brand-500/50 bg-brand-600/15 text-white'
                                : 'border-surface-600 bg-surface-900/50 text-zinc-400 hover:text-white',
                            )}
                          >
                            <span className="font-mono">slot {instance.slot}</span>
                            {(instance.country || instance.profileName) && (
                              <span className="ml-2 text-xs text-zinc-400">{instance.country || instance.profileName}</span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  ) : (
                    <p className="text-sm text-amber-300">
                      {networkStatus?.vpnte.detail || 'Нет активных VPNTE-инстансов с proxyUrl.'}
                    </p>
                  )}
                  <span className="mt-2 block text-xs text-zinc-500">
                    {networkStatus?.vpnte.control_url || 'http://127.0.0.1:17873'} · выбрано: {selectedVpnteSlots.length} · нужно: {accountCount}{reserveVpnteSlots ? ` · кандидатов резерва: ${reserveVpnteSlots}` : ''} · перед запуском проверяются Kwork и уникальный IP · до {Math.min(5, accountCount)} параллельно
                  </span>
                </div>
              </div>
              {missingVpnteSlots > 0 && (
                <p className="mt-3 text-xs text-amber-300">
                  Выберите ещё {missingVpnteSlots} VPNTE-слот(а): один аккаунт требует отдельный IP.
                </p>
              )}
              <label className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
                <input
                  type="checkbox"
                  checked={avoidUsedIps}
                  onChange={(event) => setAvoidUsedIps(event.target.checked)}
                  className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                />
                Не использовать IP, уже сохранённые в базе аккаунтов
              </label>
              {usesFirstmail && accountCount > 1 && (
                <p className="mt-3 text-xs text-amber-300">Для пачки используйте CatchMail.</p>
              )}
              {!usesFirstmail && accountCount > 1 && form.email?.trim() && (
                <p className="mt-3 text-xs text-amber-300">Для пачки очистите Email: адреса CatchMail создаются автоматически.</p>
              )}
            </div>

            <details
              className="border-t border-white/10 pt-4"
              open={advancedOpen}
              onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
            >
              <summary className="cursor-pointer text-sm font-medium text-zinc-300 marker:text-zinc-500 hover:text-white">
                Дополнительные параметры
              </summary>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <label className="block">
                  <span className="label">Промокод</span>
                  <input
                    value={form.promo}
                    onChange={(event) => setForm((prev) => ({ ...prev, promo: event.target.value }))}
                    className="input"
                  />
                </label>
                <label className="block">
                  <span className="label">Action after</span>
                  <input
                    value={form.action_after}
                    onChange={(event) => setForm((prev) => ({ ...prev, action_after: event.target.value }))}
                    className="input"
                  />
                </label>
                <label className="block md:col-span-2">
                  <span className="label">Track client ID</span>
                  <input
                    value={form.track_client_id}
                    onChange={(event) => setForm((prev) => ({ ...prev, track_client_id: event.target.value }))}
                    className="input font-mono"
                  />
                </label>
                <label className="block md:col-span-2">
                  <span className="label">Токен CAPTCHA</span>
                  <textarea
                    value={form.captcha_token}
                    onChange={(event) => setForm((prev) => ({ ...prev, captcha_token: event.target.value }))}
                    className="input min-h-20 resize-y font-mono text-xs"
                  />
                </label>
                <label className="block">
                  <span className="label">Поле CAPTCHA</span>
                  <select
                    value={form.captcha_field}
                    onChange={(event) => setForm((prev) => ({
                      ...prev,
                      captcha_field: event.target.value as KworkRegistrationRequest['captcha_field'],
                    }))}
                    className="input"
                  >
                    <option value="smart-token">smart-token</option>
                    <option value="g-recaptcha-response">g-recaptcha-response</option>
                  </select>
                </label>
                {usesFirstmail && <label className="block md:col-span-2">
                  <span className="label">Firstmail API key (optional)</span>
                  <input
                    type="password"
                    value={form.firstmail_api_key}
                    onChange={(event) => setForm((prev) => ({ ...prev, firstmail_api_key: event.target.value }))}
                    className="input font-mono"
                    autoComplete="off"
                  />
                </label>}
                <div className="space-y-2 self-end pb-1 text-sm text-zinc-400">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={Boolean(form.use_simple)}
                      onChange={(event) => setForm((prev) => ({ ...prev, use_simple: event.target.checked }))}
                      className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                    />
                    Simple signup
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={Boolean(form.is_subscribed)}
                      onChange={(event) => setForm((prev) => ({ ...prev, is_subscribed: event.target.checked }))}
                      className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                    />
                    Подписка на рассылку
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={Boolean(form.dry_run)}
                      onChange={(event) => setForm((prev) => ({ ...prev, dry_run: event.target.checked }))}
                      className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                    />
                    Только пред-проверка
                  </label>
                </div>
              </div>
            </details>
          </fieldset>

          <div className="mt-5 flex items-center justify-between gap-3 border-t border-white/10 pt-4">
            <span className="text-xs text-zinc-500">Данные аккаунтов сохраняются в локальной базе.</span>
            <button type="submit" disabled={!canSubmit} className="btn btn-primary shrink-0">
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />}
              {submitting
                ? 'Проверка маршрутов...'
                : form.dry_run
                  ? 'Проверить данные'
                  : accountCount > 1
                    ? `Создать ${accountCount} аккаунтов`
                    : 'Создать аккаунт'}
            </button>
          </div>
        </form>

        <section className="factory-panel min-h-64 p-5" aria-live="polite">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="page-kicker">registration status</div>
              <h2 className="mt-1 text-base font-semibold text-white">Статус активации</h2>
            </div>
            {(result || error) && (
              <button
                type="button"
                onClick={clearFeedback}
                className="btn btn-ghost h-8 w-8 justify-center p-0"
                title="Очистить результат"
                aria-label="Очистить результат"
              >
                <RotateCcw className="h-4 w-4" />
              </button>
            )}
          </div>

          {!result && !error && !submitting && (
            <div className="mt-10 flex flex-col items-center text-center text-zinc-500">
              <ShieldCheck className="h-10 w-10 text-zinc-700" />
              <span className="mt-3 text-sm">Ожидание запуска</span>
            </div>
          )}

          {submitting && (
            <div className="mt-10 flex flex-col items-center text-center text-zinc-400">
              <Loader2 className="h-9 w-9 animate-spin text-brand-400" />
              <span className="mt-3 text-sm">Выполняется регистрация и проверка почты</span>
            </div>
          )}

          {error && (
            <div className="mt-5 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
              <div className="flex items-center gap-2 font-medium">
                <AlertCircle className="h-4 w-4" />
                Запрос не выполнен
              </div>
              <p className="mt-2 break-words text-red-100/90">{error}</p>
            </div>
          )}

          {result && currentStatus && (
            <div className="mt-5 space-y-4">
              <div className={cn(
                'rounded-md border p-3',
                currentStatus.tone === 'emerald' && 'border-emerald-500/30 bg-emerald-500/10',
                currentStatus.tone === 'amber' && 'border-amber-500/30 bg-amber-500/10',
                currentStatus.tone === 'red' && 'border-red-500/30 bg-red-500/10',
                currentStatus.tone === 'zinc' && 'border-zinc-700 bg-surface-900/60',
              )}>
                <div className="flex items-start gap-2">
                  <ResultIcon tone={currentStatus.tone} />
                  <div className="min-w-0">
                    <div className="font-medium text-white">{currentStatus.label}</div>
                    <p className="mt-1 text-xs text-zinc-400">{result.message || currentStatus.detail}</p>
                  </div>
                </div>
              </div>

              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
                <div className="min-w-0">
                  <dt className="text-xs text-zinc-500">Email</dt>
                  <dd className="truncate text-zinc-200" title={result.email}>{result.email || '—'}</dd>
                </div>
                {result.mail_provider && (
                  <div>
                    <dt className="text-xs text-zinc-500">Почта</dt>
                    <dd className="text-zinc-200">{result.mail_provider === 'catchmail' ? 'CatchMail' : 'Firstmail'}</dd>
                  </div>
                )}
                <div className="min-w-0">
                  <dt className="text-xs text-zinc-500">Логин</dt>
                  <dd className="truncate font-mono text-zinc-200" title={result.username}>{result.username || '—'}</dd>
                </div>
                {result.registration_id && (
                  <div className="min-w-0">
                    <dt className="text-xs text-zinc-500">Пароль</dt>
                    <dd className="flex items-center gap-2 font-mono text-zinc-200">
                      <span className="min-w-0 truncate" title={showRegistrationPassword ? credentials?.password : undefined}>
                        {credentials?.registration_id === result.registration_id
                          ? (showRegistrationPassword ? credentials.password : '••••••••••••••••••••')
                          : 'сохранён в базе'}
                      </span>
                      <button
                        type="button"
                        onClick={handleRegistrationPassword}
                        className="shrink-0 rounded p-1 text-zinc-400 hover:text-white"
                        title={showRegistrationPassword ? 'Скрыть пароль' : 'Показать пароль'}
                        aria-label={showRegistrationPassword ? 'Скрыть пароль' : 'Показать пароль'}
                      >
                        {showRegistrationPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </dd>
                  </div>
                )}
                <div>
                  <dt className="text-xs text-zinc-500">Тип</dt>
                  <dd className="text-zinc-200">{result.user_type === 2 ? 'Продавец' : 'Покупатель'}</dd>
                </div>
                <div>
                  <dt className="text-xs text-zinc-500">Активация</dt>
                  <dd className={result.activated ? 'text-emerald-300' : 'text-zinc-300'}>
                    {result.activated ? 'подтверждена' : 'не подтверждена'}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-zinc-500">Телефон</dt>
                  <dd className={result.phone_fields_sent ? 'text-amber-300' : 'text-emerald-300'}>
                    {result.phone_fields_sent ? 'отправлялся' : 'не отправлялся'}
                  </dd>
                </div>
                {result.signup_ip && (
                  <div>
                    <dt className="text-xs text-zinc-500">IP сессии</dt>
                    <dd className="font-mono text-zinc-200">{result.activation_ip || result.signup_ip}</dd>
                  </div>
                )}
                {result.session_cookie_count !== undefined && (
                  <div>
                    <dt className="text-xs text-zinc-500">Cookies</dt>
                    <dd className="font-mono text-zinc-200">{result.session_cookie_count}</dd>
                  </div>
                )}
                {result.status_code !== undefined && (
                  <div>
                    <dt className="text-xs text-zinc-500">HTTP</dt>
                    <dd className="font-mono text-zinc-200">{result.status_code}</dd>
                  </div>
                )}
                {result.post_activation && (
                  <div className="col-span-2">
                    <dt className="text-xs text-zinc-500">Проверка входа</dt>
                    <dd className={result.post_activation.ok ? 'text-emerald-300' : 'text-red-300'}>
                      {result.post_activation.ok ? 'actor подтверждён' : result.post_activation.reason || 'не подтверждена'}
                    </dd>
                  </div>
                )}
              </dl>

              {batchResult && (
                <div className="border-t border-white/10 pt-4">
                  <div className="flex items-center justify-between gap-3 text-sm">
                    <span className="font-medium text-zinc-200">Пачка аккаунтов · до {batchResult.parallel_limit} параллельно</span>
                    <span className="font-mono text-emerald-300">
                      {batchResult.activated_count}/{batchResult.requested_count}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
                    <span>Проверено: {batchResult.selected_proxy_count ?? batchResult.proxy_count}</span>
                    <span>Отвечают: {batchResult.reachable_proxy_count ?? batchResult.proxy_count}</span>
                    <span className="text-emerald-300">Уникальных свободных IP: {batchResult.healthy_proxy_count ?? batchResult.proxy_count}</span>
                    <span>Резерв: {batchResult.reserve_proxy_count ?? 0}</span>
                    <span>Старые IP: {batchResult.avoid_used_ips ? 'исключались' : 'разрешены'}</span>
                    {((batchResult.failed_proxy_count ?? 0) > 0 || (batchResult.duplicate_proxy_count ?? 0) > 0 || (batchResult.used_ip_proxy_count ?? 0) > 0) && (
                      <span className="text-amber-300">
                        Отсеяно: {batchResult.failed_proxy_count ?? 0} недоступных, {batchResult.duplicate_proxy_count ?? 0} повторов, {batchResult.used_ip_proxy_count ?? 0} использованных
                      </span>
                    )}
                  </div>
                  <div className="mt-3 divide-y divide-white/10 border-y border-white/10">
                    {batchResult.results.map((item, index) => {
                      const saved = item.registration_id ? batchCredentials[item.registration_id] : undefined
                      return (
                        <div key={item.registration_id || `${item.batch_index || index}`} className="grid gap-1 py-3 text-xs md:grid-cols-[36px_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)]">
                          <span className="font-mono text-zinc-500">{item.batch_index || index + 1}</span>
                          <span className="truncate font-mono text-zinc-200" title={item.username}>{item.username || '—'}</span>
                          <span className="truncate font-mono text-zinc-200" title={saved?.password}>{saved?.password || '—'}</span>
                          <span className={item.activated ? 'text-emerald-300' : 'text-amber-300'}>
                            {item.vpnte_slot ? `slot ${item.vpnte_slot} · ` : ''}{item.activation_ip || item.signup_ip || item.preflight_ip || item.code || item.status}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {!result.activated && (result.status === 'activation_pending' || result.status === 'activation_failed') && (
                <button
                  type="button"
                  onClick={handleVerifyActivation}
                  disabled={submitting || !result.registration_id}
                  className="btn btn-secondary w-full"
                >
                  {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                  Проверить активацию
                </button>
              )}

              {result.reason && (
                <div className="border-t border-white/10 pt-3 text-xs text-zinc-500">
                  Причина: <span className="font-mono text-zinc-300">{result.reason}</span>
                </div>
              )}
            </div>
          )}
        </section>
      </div>

      <section className="factory-panel p-5" aria-live="polite">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="page-kicker">local account store</div>
            <h2 className="mt-1 flex items-center gap-2 text-base font-semibold text-white">
              <Database className="h-4 w-4 text-brand-400" />
              Сохранённые аккаунты
            </h2>
            <p className="mt-1 text-sm text-zinc-400">
              {storedAccounts.length} всего · {activatedStoredAccountCount} активировано
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {selectedStoredAccountIds.length > 0 && (
              <button
                type="button"
                onClick={handleSelectedStoredAccountsDelete}
                disabled={storedAccountAction !== null}
                className="btn h-9 border border-red-500/30 bg-red-500/10 px-3 text-red-200 hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {storedAccountAction === 'delete:selected' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                Удалить из базы ({selectedStoredAccountIds.length})
              </button>
            )}
            <button
              type="button"
              onClick={() => void refreshStoredAccounts()}
              disabled={storedAccountsLoading || storedAccountAction !== null}
              className="btn btn-secondary h-9 px-3"
            >
              <RefreshCw className={cn('h-4 w-4', storedAccountsLoading && 'animate-spin')} />
              Обновить
            </button>
          </div>
        </div>

        {storedAccountsError && (
          <div className="mt-4 flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-300" />
            <p className="break-words">{storedAccountsError}</p>
          </div>
        )}

        {storedAccountsLoading && storedAccounts.length === 0 && (
          <div className="mt-8 flex items-center justify-center gap-2 py-6 text-sm text-zinc-400">
            <Loader2 className="h-4 w-4 animate-spin text-brand-400" />
            Загрузка локальных аккаунтов
          </div>
        )}

        {!storedAccountsLoading && storedAccounts.length === 0 && !storedAccountsError && (
          <div className="mt-8 flex flex-col items-center py-6 text-center text-zinc-500">
            <Database className="h-9 w-9 text-zinc-700" />
            <p className="mt-3 text-sm">В локальной базе пока нет зарегистрированных аккаунтов.</p>
          </div>
        )}

        {storedAccounts.length > 0 && (
          <div className="mt-5 overflow-x-auto border-y border-white/10">
            <table className="min-w-[1040px] table-fixed text-left text-sm">
              <thead className="bg-white/[0.025] text-xs font-medium text-zinc-500">
                <tr>
                  <th className="w-10 px-3 py-3">
                    <input
                      type="checkbox"
                      checked={allStoredAccountsSelected}
                      onChange={(event) => setSelectedStoredAccountIds(
                        event.target.checked ? storedAccounts.map((account) => account.registration_id) : [],
                      )}
                      disabled={storedAccountAction !== null}
                      className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                      aria-label="Выбрать все аккаунты"
                    />
                  </th>
                  <th className="w-[23%] px-3 py-3">Аккаунт</th>
                  <th className="w-[14%] px-3 py-3">Статус</th>
                  <th className="w-[15%] px-3 py-3">Сессия</th>
                  <th className="w-[14%] px-3 py-3">Сеть</th>
                  <th className="w-[14%] px-3 py-3">Создан</th>
                  <th className="w-[12%] px-3 py-3">Пароль</th>
                  <th className="w-36 px-3 py-3 text-right">Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/10">
                {storedAccounts.map((account) => {
                  const accountStatus = statusCopy(account.status)
                  const sessionCheck = storedAccountSessionChecks[account.registration_id]
                  const credentialsForAccount = storedAccountCredentials[account.registration_id]
                  const passwordVisible = visibleStoredAccountPasswords.includes(account.registration_id)
                  const isSessionChecking = storedAccountAction === `session:${account.registration_id}`
                  const isActivating = storedAccountAction === `activation:${account.registration_id}`
                  const isPasswordLoading = storedAccountAction === `credentials:${account.registration_id}`
                  const isDeleting = storedAccountAction === `delete:${account.registration_id}`
                  const canRetryActivation = account.status === 'activation_pending' || account.status === 'activation_failed'
                  const accountSelected = selectedStoredAccountIds.includes(account.registration_id)
                  return (
                    <tr key={account.registration_id} className="align-top hover:bg-white/[0.018]">
                      <td className="px-3 py-3">
                        <input
                          type="checkbox"
                          checked={accountSelected}
                          onChange={(event) => setSelectedStoredAccountIds((current) => (
                            event.target.checked
                              ? [...current, account.registration_id]
                              : current.filter((registrationId) => registrationId !== account.registration_id)
                          ))}
                          disabled={storedAccountAction !== null}
                          className="h-4 w-4 rounded border-surface-600 bg-surface-900 text-brand-500 focus:ring-brand-500"
                          aria-label={`Выбрать ${account.username}`}
                        />
                      </td>
                      <td className="min-w-0 px-3 py-3">
                        <div className="truncate font-mono text-zinc-100" title={account.username}>{account.username}</div>
                        <div className="mt-1 truncate text-xs text-zinc-500" title={account.email}>{account.email}</div>
                        <div className="mt-1 text-xs text-zinc-500">
                          {account.user_type === 2 ? 'продавец' : 'покупатель'} · {account.mail_provider}
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <span className={cn(
                          'badge max-w-full truncate',
                          accountStatus.tone === 'emerald' && 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200',
                          accountStatus.tone === 'amber' && 'border-amber-500/30 bg-amber-500/10 text-amber-200',
                          accountStatus.tone === 'red' && 'border-red-500/30 bg-red-500/10 text-red-200',
                          accountStatus.tone === 'zinc' && 'border-white/10 bg-white/[0.03] text-zinc-300',
                        )} title={accountStatus.detail}>
                          {accountStatus.label}
                        </span>
                        {account.last_error && (
                          <p className="mt-1 line-clamp-2 text-xs text-red-300/80" title={account.last_error}>{account.last_error}</p>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        {sessionCheck ? (
                          <div>
                            <div className={sessionCheck.ok ? 'text-emerald-300' : 'text-red-300'}>
                              {sessionCheck.ok ? 'cookies живы' : sessionCheck.reason || 'не подтверждена'}
                            </div>
                            {sessionCheck.status_code !== undefined && (
                              <div className="mt-1 font-mono text-xs text-zinc-500">HTTP {sessionCheck.status_code}</div>
                            )}
                          </div>
                        ) : (
                          <span className="text-zinc-500">не проверена</span>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <div className="font-mono text-xs text-zinc-300">{account.activation_ip || account.signup_ip || '—'}</div>
                        <div className="mt-1 text-xs text-zinc-500">cookies: {account.session_cookie_count ?? 0}</div>
                      </td>
                      <td className="px-3 py-3 text-xs text-zinc-400">
                        {formatAccountTimestamp(account.created_at || account.registration_started_at)}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex min-w-0 items-center gap-1.5 font-mono text-xs text-zinc-300">
                          <span className="min-w-0 truncate" title={passwordVisible ? credentialsForAccount?.password : undefined}>
                            {passwordVisible ? credentialsForAccount?.password : '••••••••'}
                          </span>
                          <button
                            type="button"
                            onClick={() => void handleStoredAccountPassword(account.registration_id)}
                            disabled={storedAccountAction !== null}
                            className="shrink-0 rounded p-1 text-zinc-400 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                            title={passwordVisible ? 'Скрыть пароль' : 'Показать пароль'}
                            aria-label={passwordVisible ? 'Скрыть пароль' : 'Показать пароль'}
                          >
                            {isPasswordLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : passwordVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                          </button>
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex justify-end gap-1">
                          {canRetryActivation && (
                            <button
                              type="button"
                              onClick={() => void handleStoredAccountActivation(account)}
                              disabled={storedAccountAction !== null}
                              className="rounded p-1.5 text-amber-300 hover:bg-amber-500/10 hover:text-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
                              title="Повторить активацию аккаунта"
                              aria-label="Повторить активацию аккаунта"
                            >
                              {isActivating ? <Loader2 className="h-4 w-4 animate-spin" /> : <MailCheck className="h-4 w-4" />}
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => void handleStoredAccountSessionCheck(account.registration_id)}
                            disabled={storedAccountAction !== null}
                            className="rounded p-1.5 text-zinc-400 hover:bg-white/5 hover:text-emerald-200 disabled:cursor-not-allowed disabled:opacity-50"
                            title="Проверить cookie-сессию"
                            aria-label="Проверить cookie-сессию"
                          >
                            {isSessionChecking ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleStoredAccountDelete(account.registration_id)}
                            disabled={storedAccountAction !== null}
                            className="rounded p-1.5 text-zinc-400 hover:bg-red-500/10 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-50"
                            title="Удалить из локальной базы"
                            aria-label="Удалить из локальной базы"
                          >
                            {isDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
