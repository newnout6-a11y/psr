import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  FilePenLine,
  FileText,
  Loader2,
  RefreshCw,
  SendToBack,
  Send,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'

import { cn } from '../../lib/utils'
import { buyerOutreachApi, BuyerOutreachApiError } from './outreach-api'
import type {
  BuyerOutreachDraft,
  BuyerOutreachJson,
  BuyerOutreachPreflight,
  BuyerOutreachPreflightPayload,
  BuyerOutreachPromotion,
  BuyerOutreachSendIntent,
} from './outreach-types'

export interface BuyerOutreachWorkspaceProps {
  runId: string
  projectId: string
  senderAccountRegistrationId?: string
  serviceProfile?: BuyerOutreachJson
  operatorId?: string
  className?: string
}

type BusyAction = 'promote' | 'generate' | 'save' | 'preflight' | 'outbox' | 'deliver' | 'reconcile' | 'refresh' | null

const DEFAULT_PREFLIGHT: BuyerOutreachPreflightPayload = {
  project_is_active: true,
  duplicate_send_intent: false,
  outgoing_attachment_count: 0,
  attachment_upload_capability_verified: false,
}

const EMPTY_SERVICE_PROFILE: BuyerOutreachJson = {}

function prettyJson(value: BuyerOutreachJson): string {
  return JSON.stringify(value, null, 2)
}

function parseProfile(value: string): BuyerOutreachJson {
  const parsed: unknown = JSON.parse(value || '{}')
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Профиль услуги должен быть JSON-объектом')
  }
  return parsed as BuyerOutreachJson
}

function optionalNumber(value: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error('Введите неотрицательное число')
  return parsed
}

function optionalPositiveInteger(value: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error('Срок выполнения должен быть положительным целым числом')
  return parsed
}

function draftSourceLabel(source: string): string {
  return {
    generated: 'сгенерирован',
    manual: 'вручную',
    operator: 'оператор',
  }[source] || source
}

function sendIntentStateLabel(state: string): string {
  return {
    draft: 'черновик',
    preflight: 'проверка',
    pending_send: 'ожидает отправки',
    sending: 'отправляется',
    accepted: 'принято',
    unknown: 'требует сверки',
    failed: 'ошибка',
  }[state] || state
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function formatDraftLabel(draft: BuyerOutreachDraft): string {
  return `v${draft.version} ${draftSourceLabel(draft.source)}${draft.parent_draft_id ? ' (редакция)' : ''}`
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Не удалось выполнить операцию в рабочей области откликов.'
}

function PreflightBadge({ preflight }: { preflight: BuyerOutreachPreflight | null }) {
  if (!preflight) return <span className="border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-500">не проверено</span>
  return preflight.passed ? (
    <span className="border border-emerald-500/40 px-1.5 py-0.5 text-[10px] text-emerald-300">пройдено</span>
  ) : (
    <span className="border border-rose-500/40 px-1.5 py-0.5 text-[10px] text-rose-300">требует проверки</span>
  )
}

function CompactToggle({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <label className="flex min-w-0 items-center justify-between gap-3 border-t border-surface-800 py-2 text-xs text-zinc-300">
      <span className="min-w-0">{label}</span>
      <input
        type="checkbox"
        className="h-4 w-4 shrink-0 rounded border-surface-600 bg-surface-900 text-cyan-400 focus:ring-cyan-400"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
    </label>
  )
}

/**
 * Compact, durable proposal workspace for a selected Buyer Search project.
 * The component only creates immutable drafts, preflights, and outbox intents.
 * It intentionally has no remote-delivery control.
 */
export function BuyerOutreachWorkspace({
  runId,
  projectId,
  senderAccountRegistrationId = '',
  serviceProfile = EMPTY_SERVICE_PROFILE,
  operatorId = '',
  className,
}: BuyerOutreachWorkspaceProps) {
  const [promotion, setPromotion] = useState<BuyerOutreachPromotion | null>(null)
  const [drafts, setDrafts] = useState<BuyerOutreachDraft[]>([])
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null)
  const [preflight, setPreflight] = useState<BuyerOutreachPreflight | null>(null)
  const [sendIntent, setSendIntent] = useState<BuyerOutreachSendIntent | null>(null)
  const [accountId, setAccountId] = useState(senderAccountRegistrationId)
  const [operator, setOperator] = useState(operatorId)
  const [profileText, setProfileText] = useState(() => prettyJson(serviceProfile))
  const [body, setBody] = useState('')
  const [price, setPrice] = useState('')
  const [deliveryDays, setDeliveryDays] = useState('')
  const [currency, setCurrency] = useState('RUB')
  const [modelAlias, setModelAlias] = useState('')
  const [preflightDraft, setPreflightDraft] = useState<BuyerOutreachPreflightPayload>(DEFAULT_PREFLIGHT)
  const [confirmed, setConfirmed] = useState(false)
  const [deliveryConfirmed, setDeliveryConfirmed] = useState(false)
  const [busy, setBusy] = useState<BusyAction>('refresh')
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState('')
  const selectedDraftIdRef = useRef<string | null>(null)
  const requestRef = useRef<AbortController | null>(null)

  const selectedDraft = useMemo(
    () => drafts.find((draft) => draft.draft_id === selectedDraftId) ?? null,
    [drafts, selectedDraftId],
  )

  const selectDraft = useCallback((draft: BuyerOutreachDraft | null) => {
    selectedDraftIdRef.current = draft?.draft_id ?? null
    setSelectedDraftId(draft?.draft_id ?? null)
    setBody(draft?.body ?? '')
    setPrice(draft?.price == null ? '' : String(draft.price))
    setDeliveryDays(draft?.delivery_days == null ? '' : String(draft.delivery_days))
    setCurrency(draft?.currency || 'RUB')
    setPreflight(null)
    setSendIntent(null)
    setConfirmed(false)
    setDeliveryConfirmed(false)
  }, [])

  const refresh = useCallback(async (preferredDraftId?: string | null, intentId?: string | null) => {
    requestRef.current?.abort()
    const controller = new AbortController()
    requestRef.current = controller
    setBusy('refresh')
    setError(null)

    try {
      const [promotionResult, draftResult] = await Promise.allSettled([
        buyerOutreachApi.getPromotion(runId, projectId, controller.signal),
        buyerOutreachApi.listDrafts(runId, projectId, controller.signal),
      ])
      if (controller.signal.aborted) return

      const noPromotion = promotionResult.status === 'rejected'
        && promotionResult.reason instanceof BuyerOutreachApiError
        && promotionResult.reason.status === 404
      if (noPromotion) {
        setPromotion(null)
        setDrafts([])
        selectDraft(null)
        setStatus('Добавьте проект в отклики, чтобы создать его рабочую область.')
        return
      }
      if (promotionResult.status === 'rejected') throw promotionResult.reason
      if (draftResult.status === 'rejected') throw draftResult.reason

      const nextPromotion = promotionResult.value
      const nextDrafts = draftResult.value
      const targetId = preferredDraftId ?? selectedDraftIdRef.current
      const nextDraft = nextDrafts.find((draft) => draft.draft_id === targetId) ?? nextDrafts[nextDrafts.length - 1] ?? null
      setPromotion(nextPromotion)
      setDrafts(nextDrafts)
      selectDraft(nextDraft)

      const nextIntentId = intentId ?? null
      const [nextPreflight, nextIntent] = await Promise.all([
        nextDraft ? buyerOutreachApi.getPreflight(nextDraft.draft_id, controller.signal) : Promise.resolve(null),
        nextIntentId ? buyerOutreachApi.getSendIntent(nextIntentId, controller.signal) : Promise.resolve(null),
      ])
      if (controller.signal.aborted) return
      setPreflight(nextPreflight)
      setSendIntent(nextIntent)
      setStatus(nextDraft ? `Загружено неизменяемых версий черновика: ${nextDrafts.length}.` : 'Проект добавлен в отклики, но черновиков пока нет.')
    } catch (refreshError) {
      if (!isAbort(refreshError)) setError(getErrorMessage(refreshError))
    } finally {
      if (!controller.signal.aborted) setBusy(null)
    }
  }, [projectId, runId, selectDraft])

  useEffect(() => {
    setAccountId(senderAccountRegistrationId)
  }, [senderAccountRegistrationId])

  useEffect(() => {
    setOperator(operatorId)
  }, [operatorId])

  useEffect(() => {
    setProfileText(prettyJson(serviceProfile))
  }, [serviceProfile])

  useEffect(() => {
    selectedDraftIdRef.current = null
    setPromotion(null)
    setDrafts([])
    setSelectedDraftId(null)
    setPreflight(null)
    setSendIntent(null)
    setStatus('')
    setError(null)
  }, [projectId, runId])

  useEffect(() => {
    void refresh()
    return () => requestRef.current?.abort()
  }, [refresh])

  useEffect(() => {
    if (!selectedDraftId) return
    const controller = new AbortController()
    void buyerOutreachApi.getPreflight(selectedDraftId, controller.signal)
      .then((nextPreflight) => setPreflight(nextPreflight))
      .catch((readError) => {
        if (!isAbort(readError)) setError(getErrorMessage(readError))
      })
    return () => controller.abort()
  }, [selectedDraftId])

  const promote = useCallback(async () => {
    if (!accountId.trim()) {
      setError('Для закрепления рабочей области требуется ID регистрации аккаунта.')
      return
    }
    setBusy('promote')
    setError(null)
    try {
      const nextPromotion = await buyerOutreachApi.promote(runId, projectId, {
        sender_account_registration_id: accountId.trim(),
        service_profile: parseProfile(profileText),
      })
      setPromotion(nextPromotion)
      setStatus('Проект добавлен в отклики и закреплен за выбранным аккаунтом.')
      await refresh()
    } catch (promoteError) {
      setError(getErrorMessage(promoteError))
    } finally {
      setBusy(null)
    }
  }, [accountId, profileText, projectId, refresh, runId])

  const generate = useCallback(async () => {
    if (!promotion) return
    setBusy('generate')
    setError(null)
    try {
      const draft = await buyerOutreachApi.generateDraft(runId, projectId, {
        price: optionalNumber(price),
        delivery_days: optionalPositiveInteger(deliveryDays),
        currency: currency.trim() || 'RUB',
        model_alias: modelAlias.trim() || null,
      })
      await refresh(draft.draft_id)
      setStatus(`Создана неизменяемая версия черновика ${draft.version}.`)
    } catch (generateError) {
      setError(getErrorMessage(generateError))
    } finally {
      setBusy(null)
    }
  }, [currency, deliveryDays, modelAlias, price, projectId, promotion, refresh, runId])

  const saveRevision = useCallback(async () => {
    if (!selectedDraft || !body.trim()) return
    setBusy('save')
    setError(null)
    try {
      const draft = await buyerOutreachApi.editDraft(selectedDraft.draft_id, {
        body: body.trim(),
        price: optionalNumber(price),
        delivery_days: optionalPositiveInteger(deliveryDays),
        currency: currency.trim() || null,
      })
      await refresh(draft.draft_id)
      setStatus(`Сохранена неизменяемая редакция v${draft.version}; исходный черновик не изменен.`)
    } catch (saveError) {
      setError(getErrorMessage(saveError))
    } finally {
      setBusy(null)
    }
  }, [body, currency, deliveryDays, price, refresh, selectedDraft])

  const runPreflight = useCallback(async () => {
    if (!selectedDraft) return
    setBusy('preflight')
    setError(null)
    try {
      const result = await buyerOutreachApi.preflight(selectedDraft.draft_id, preflightDraft)
      setPreflight(result.preflight)
      setStatus(result.preflight.passed ? 'Проверка пройдена и сохранена для этой версии.' : 'Перед подтверждением очереди устраните замечания проверки.')
    } catch (preflightError) {
      setError(getErrorMessage(preflightError))
    } finally {
      setBusy(null)
    }
  }, [preflightDraft, selectedDraft])

  const createOutboxIntent = useCallback(async () => {
    if (!selectedDraft || !confirmed || !accountId.trim() || !operator.trim()) return
    setBusy('outbox')
    setError(null)
    try {
      const result = await buyerOutreachApi.createSendIntent(selectedDraft.draft_id, {
        sender_account_registration_id: accountId.trim(),
        confirmed_by: operator.trim(),
      })
      setPreflight(result.preflight)
      setSendIntent(result.send_intent)
      setConfirmed(false)
      setDeliveryConfirmed(false)
      setStatus('Подтвержденный запрос в очереди сохранен. Предложение не отправлено.')
    } catch (intentError) {
      setError(getErrorMessage(intentError))
    } finally {
      setBusy(null)
    }
  }, [accountId, confirmed, operator, selectedDraft])

  const updatePreflight = useCallback((key: keyof BuyerOutreachPreflightPayload, value: boolean) => {
    setPreflightDraft((current) => ({ ...current, [key]: value }))
  }, [])

  const deliver = useCallback(async () => {
    if (!sendIntent || !accountId.trim() || !operator.trim() || !deliveryConfirmed) return
    setBusy('deliver')
    setError(null)
    try {
      const result = await buyerOutreachApi.deliverSendIntent(sendIntent.intent_id, {
        sender_account_registration_id: accountId.trim(),
        confirmed_by: operator.trim(),
        delivery_confirmation_id: crypto.randomUUID(),
        explicit_delivery_confirmation: true,
      })
      setSendIntent(result.send_intent)
      setDeliveryConfirmed(false)
      setStatus(result.delivery.requires_reconciliation ? 'Результат доставки неизвестен: перед новой попыткой выполните сверку.' : `Состояние доставки: ${sendIntentStateLabel(result.send_intent.state)}`)
    } catch (deliveryError) {
      setError(getErrorMessage(deliveryError))
    } finally {
      setBusy(null)
    }
  }, [accountId, deliveryConfirmed, operator, sendIntent])

  const reconcileDelivery = useCallback(async () => {
    if (!sendIntent || !accountId.trim()) return
    setBusy('reconcile')
    setError(null)
    try {
      const result = await buyerOutreachApi.reconcileDelivery(sendIntent.intent_id, accountId.trim())
      setSendIntent(result.send_intent)
      setStatus(`Состояние после сверки: ${sendIntentStateLabel(result.send_intent.state)}`)
    } catch (reconcileError) {
      setError(getErrorMessage(reconcileError))
    } finally {
      setBusy(null)
    }
  }, [accountId, sendIntent])

  const isBusy = busy !== null
  const canCreateIntent = Boolean(selectedDraft && preflight?.passed && confirmed && accountId.trim() && operator.trim())
  const canDeliver = Boolean(sendIntent?.state === 'pending_send' && deliveryConfirmed && accountId.trim() && operator.trim())

  return (
    <section className={cn('min-w-0 border-t border-surface-700 bg-surface-925', className)} aria-busy={isBusy}>
      <header className="flex items-center justify-between gap-3 border-b border-surface-700 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
            <FileText className="h-3.5 w-3.5 text-cyan-300" /> Рабочая область откликов
          </div>
          <div className="mt-1 truncate font-mono text-[10px] text-zinc-600">{projectId}</div>
        </div>
        <button
          type="button"
          className="btn btn-ghost h-7 w-7 shrink-0 justify-center px-0"
          title="Обновить сохраненные данные откликов"
          aria-label="Обновить сохраненные данные откликов"
          disabled={isBusy}
          onClick={() => void refresh()}
        >
          <RefreshCw className={cn('h-3.5 w-3.5', busy === 'refresh' && 'animate-spin')} />
        </button>
      </header>

      {error ? <div role="alert" className="border-b border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-200">{error}</div> : null}
      {status ? <div className="border-b border-surface-800 px-4 py-2 text-[11px] text-zinc-500">{status}</div> : null}

      <div className="space-y-0">
        <section className="border-b border-surface-800 px-4 py-3">
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-500">1. Добавление в отклики</span>
            {promotion ? <span className="border border-emerald-500/40 px-1.5 py-0.5 text-[10px] text-emerald-300">закреплен</span> : <span className="border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-500">не добавлен</span>}
          </div>
          <label className="block text-[11px] text-zinc-500">
            ID регистрации аккаунта
            <input className="input mt-1 h-8 w-full py-1 text-xs" value={accountId} onChange={(event) => setAccountId(event.target.value)} disabled={Boolean(promotion)} />
          </label>
          {!promotion ? (
            <details className="mt-2 border-t border-surface-800 pt-2">
              <summary className="cursor-pointer text-xs text-zinc-400">JSON-профиль услуги</summary>
              <textarea
                className="input mt-2 min-h-24 w-full resize-y py-2 font-mono text-[11px] leading-5"
                value={profileText}
                onChange={(event) => setProfileText(event.target.value)}
                spellCheck={false}
              />
            </details>
          ) : null}
          <button type="button" className="btn btn-ghost mt-3 h-8 w-full justify-center text-xs" disabled={isBusy || Boolean(promotion)} onClick={() => void promote()}>
            {busy === 'promote' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5 text-cyan-300" />} Добавить проект в отклики
          </button>
        </section>

        <section className="border-b border-surface-800 px-4 py-3">
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-500">2. Неизменяемые черновики</span>
            <span className="font-mono text-[10px] text-zinc-600">{drafts.length}</span>
          </div>
          {drafts.length ? (
            <label className="block text-[11px] text-zinc-500">
              Версия
              <div className="relative mt-1">
                <select className="input h-8 w-full appearance-none py-1 pr-8 text-xs" value={selectedDraft?.draft_id ?? ''} onChange={(event) => selectDraft(drafts.find((draft) => draft.draft_id === event.target.value) ?? null)}>
                  {drafts.map((draft) => <option key={draft.draft_id} value={draft.draft_id}>{formatDraftLabel(draft)}</option>)}
                </select>
                <ChevronDown className="pointer-events-none absolute right-2 top-2 h-3.5 w-3.5 text-zinc-500" />
              </div>
            </label>
          ) : null}
          <div className="mt-3 grid grid-cols-2 gap-2">
            <label className="text-[11px] text-zinc-500">Цена<input className="input mt-1 h-8 w-full py-1 font-mono text-xs" inputMode="decimal" value={price} onChange={(event) => setPrice(event.target.value)} disabled={!promotion || isBusy} /></label>
            <label className="text-[11px] text-zinc-500">Срок, дн.<input className="input mt-1 h-8 w-full py-1 font-mono text-xs" inputMode="numeric" value={deliveryDays} onChange={(event) => setDeliveryDays(event.target.value)} disabled={!promotion || isBusy} /></label>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <label className="text-[11px] text-zinc-500">Валюта<input className="input mt-1 h-8 w-full py-1 font-mono text-xs" value={currency} onChange={(event) => setCurrency(event.target.value.toUpperCase())} disabled={!promotion || isBusy} /></label>
            <label className="text-[11px] text-zinc-500">Алиас модели<input className="input mt-1 h-8 w-full py-1 text-xs" value={modelAlias} onChange={(event) => setModelAlias(event.target.value)} disabled={!promotion || isBusy} /></label>
          </div>
          {!selectedDraft ? (
            <button type="button" className="btn btn-ghost mt-3 h-8 w-full justify-center text-xs" disabled={!promotion || isBusy} onClick={() => void generate()}>
              {busy === 'generate' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-cyan-300" />} Создать черновик
            </button>
          ) : (
            <>
              <textarea className="input mt-3 min-h-36 w-full resize-y py-2 text-xs leading-5" value={body} onChange={(event) => setBody(event.target.value)} disabled={isBusy} />
              <button type="button" className="btn btn-ghost mt-2 h-8 w-full justify-center text-xs" disabled={isBusy || !body.trim()} onClick={() => void saveRevision()}>
                {busy === 'save' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FilePenLine className="h-3.5 w-3.5 text-cyan-300" />} Сохранить новую версию
              </button>
            </>
          )}
        </section>

        <section className="border-b border-surface-800 px-4 py-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-500">3. Проверка перед отправкой</span>
            <PreflightBadge preflight={preflight} />
          </div>
          <CompactToggle label="Проект активен" checked={preflightDraft.project_is_active === true} onChange={(value) => updatePreflight('project_is_active', value)} />
          <CompactToggle label="Аккаунт отправителя соответствует требованиям" checked={preflightDraft.sender_account_eligible === true} onChange={(value) => updatePreflight('sender_account_eligible', value)} />
          <CompactToggle label="Сессия подтверждена" checked={preflightDraft.account_session_valid === true} onChange={(value) => updatePreflight('account_session_valid', value)} />
          <CompactToggle label="Достаточно коннектов" checked={preflightDraft.connects_sufficient === true} onChange={(value) => updatePreflight('connects_sufficient', value)} />
          <CompactToggle label="Шаблон корректен" checked={preflightDraft.template_valid === true} onChange={(value) => updatePreflight('template_valid', value)} />
          <CompactToggle label="Требования к портфолио выполнены" checked={preflightDraft.portfolio_requirements_met === true} onChange={(value) => updatePreflight('portfolio_requirements_met', value)} />
          <button type="button" className="btn btn-ghost mt-3 h-8 w-full justify-center text-xs" disabled={!selectedDraft || isBusy} onClick={() => void runPreflight()}>
            {busy === 'preflight' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ClipboardCheck className="h-3.5 w-3.5 text-cyan-300" />} Запустить проверку
          </button>
          {preflight?.diagnostics.length ? (
            <ul className="mt-3 space-y-1 border-t border-surface-800 pt-2 text-[11px]">
              {preflight.diagnostics.map((item) => <li key={item.code} className={cn('flex gap-2', item.passed ? 'text-emerald-300' : item.severity === 'warning' ? 'text-amber-200' : 'text-rose-300')}>
                {item.passed ? <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0" /> : <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />}<span>{item.message}</span>
              </li>)}
            </ul>
          ) : null}
        </section>

        <section className="px-4 py-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-[10px] font-medium uppercase tracking-wide text-zinc-500">4. Подтвержденная очередь</span>
            {sendIntent ? <span className="border border-cyan-500/40 px-1.5 py-0.5 font-mono text-[10px] text-cyan-200">{sendIntentStateLabel(sendIntent.state)}</span> : <span className="border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-500">не создан</span>}
          </div>
          <label className="block text-[11px] text-zinc-500">Подтвердил<input className="input mt-1 h-8 w-full py-1 text-xs" value={operator} onChange={(event) => setOperator(event.target.value)} disabled={isBusy || Boolean(sendIntent)} /></label>
          <CompactToggle label="Подтверждаю добавление этого черновика в очередь" checked={confirmed} onChange={setConfirmed} />
          <button type="button" className="btn btn-ghost mt-3 h-8 w-full justify-center text-xs" disabled={!canCreateIntent || isBusy || Boolean(sendIntent)} onClick={() => void createOutboxIntent()}>
            {busy === 'outbox' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <SendToBack className="h-3.5 w-3.5 text-cyan-300" />} Создать запрос в очереди
          </button>
          <p className="mt-2 text-[10px] leading-4 text-zinc-600">Будет создано только локальное состояние очереди с подтверждением. Предложение не отправляется.</p>
          {sendIntent ? <div className="mt-3 border-t border-surface-800 pt-2 text-[11px] text-zinc-400"><div className="flex items-center justify-between gap-2"><span>Доставка</span><button type="button" className="btn btn-ghost h-6 w-6 justify-center px-0" title="Обновить запрос в очереди" aria-label="Обновить запрос в очереди" disabled={isBusy} onClick={() => void refresh(selectedDraft?.draft_id, sendIntent.intent_id)}><RefreshCw className="h-3 w-3" /></button></div><div className="mt-1 break-all font-mono text-[10px] text-zinc-600">{sendIntent.intent_id}</div>{sendIntent.state === 'pending_send' ? <><CompactToggle label="Подтверждаю удаленную отправку предложения" checked={deliveryConfirmed} onChange={setDeliveryConfirmed} /><button type="button" className="btn btn-ghost mt-2 h-8 w-full justify-center text-xs" disabled={!canDeliver || isBusy} onClick={() => void deliver()}>{busy === 'deliver' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5 text-lime-300" />} Отправить предложение</button></> : null}{sendIntent.state === 'unknown' ? <button type="button" className="btn btn-ghost mt-2 h-8 w-full justify-center text-xs" disabled={isBusy || !accountId.trim()} onClick={() => void reconcileDelivery()}>{busy === 'reconcile' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5 text-cyan-300" />} Сверить доставку</button> : null}{sendIntent.remote_receipt ? <div className="mt-1 font-mono text-[10px] text-emerald-300">{sendIntent.remote_receipt}</div> : null}{sendIntent.failure_reason ? <div className="mt-1 text-rose-300">{sendIntent.failure_reason}</div> : null}</div> : null}
        </section>
      </div>
    </section>
  )
}

export default BuyerOutreachWorkspace
