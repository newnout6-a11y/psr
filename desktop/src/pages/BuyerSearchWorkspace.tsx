import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Archive, ChevronDown, Download, Heart, Loader2, Plus, RefreshCw, Search, Send, SlidersHorizontal } from 'lucide-react'

import { buyerSearchApi } from '../features/buyer-search/api'
import { ProjectFeed } from '../features/buyer-search/components/ProjectFeed'
import { DEFAULT_PROJECT_FILTERS, ProjectFilters, type BuyerProjectFilterState } from '../features/buyer-search/components/ProjectFilters'
import { ProjectInspector } from '../features/buyer-search/components/ProjectInspector'
import { SearchBuilder } from '../features/buyer-search/components/SearchBuilder'
import { SearchMonitor } from '../features/buyer-search/components/SearchMonitor'
import type {
  BuyerRunMode,
  BuyerSearchFleet,
  BuyerSearchProject,
  BuyerSearchProjectDetail,
  BuyerSearchProjectListOptions,
  BuyerSearchEvent,
  BuyerSearchQuery,
  BuyerSearchRun,
  BuyerSearchWorkspaceSection,
  BuyerSearchWebSocketMessage,
  CreateBuyerSearchRunPayload,
} from '../features/buyer-search/types'
import { useBuyerWorkspace } from '../features/buyer-search/useBuyerWorkspace'
import { api, type KworkRegistrationAccount } from '../lib/api'

const SECTIONS: Array<{ id: BuyerSearchWorkspaceSection; label: string; icon: typeof Search }> = [
  { id: 'projects', label: 'Проекты', icon: Search },
  { id: 'shortlist', label: 'Избранное', icon: Heart },
  { id: 'outreach', label: 'Отклики', icon: Send },
  { id: 'monitoring', label: 'Мониторинг', icon: SlidersHorizontal },
]

function optionalNumber(value: unknown) {
  if (value === '' || value == null) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function filtersFromRecord(record: Record<string, unknown>): BuyerProjectFilterState {
  return { ...DEFAULT_PROJECT_FILTERS, ...record } as BuyerProjectFilterState
}

function modeFor(brief: string, categories: Array<Record<string, unknown>>, queries: string[]): BuyerRunMode {
  const hasBrief = Boolean(brief.trim())
  const hasCategories = categories.length > 0
  const hasQueries = queries.some(Boolean)
  if (hasCategories && (hasBrief || hasQueries)) return 'hybrid'
  if (hasCategories) return 'category'
  if (hasQueries && !hasBrief) return 'manual'
  return 'brief'
}

type StreamState = 'disconnected' | 'connecting' | 'live' | 'reconnecting'

interface StreamDiagnostics {
  runId: string
  cursor: number
  lastEventType: string | null
  lastEventAt: number | null
  lastCheckedAt: number | null
  checkError: string | null
  connectionError: string | null
  lastIssue: { type: string; source: string | null; kind: string | null; at: number | null } | null
  sourceObservations: Array<{ source: string; type: string; at: number | null }>
}

function eventTimestamp(value: string | null | undefined): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

function elapsedLabel(at: number | null, now: number): string {
  if (at === null) return 'неизвестно'
  const seconds = Math.max(0, Math.floor((now - at) / 1000))
  if (seconds < 60) return `${seconds} с назад`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`
  return `${Math.floor(seconds / 3600)} ч назад`
}

function eventLabel(type: string): string {
  const labels: Record<string, string> = {
    'run.created': 'Запуск создан', 'run.ready': 'План готов', 'run.started': 'Поиск начат',
    'run.resumed': 'Поиск продолжен', 'run.paused': 'Поиск приостановлен',
    'run.stopped': 'Поиск остановлен', 'run.completed': 'Поиск завершён',
    'run.blocked': 'Поиск заблокирован', 'run.failed': 'Ошибка запуска',
    'run.restarted': 'Запуск подготовлен заново', 'query.page.completed': 'Страница получена',
    'query.page.retry': 'Назначен повтор страницы', 'query.page.failed': 'Ошибка страницы',
    'query.refresh.waiting': 'Ожидание новых проектов', 'query.replenishment.waiting': 'Подбор новых запросов',
  }
  return labels[type] || type
}

export default function BuyerSearchWorkspace() {
  const { state, ready, saveState, updateBuilder, updateView } = useBuyerWorkspace()
  const { builder, view } = state
  const [runs, setRuns] = useState<BuyerSearchRun[]>([])
  const [accounts, setAccounts] = useState<KworkRegistrationAccount[]>([])
  const [projects, setProjects] = useState<BuyerSearchProject[]>([])
  const [projectDetail, setProjectDetail] = useState<BuyerSearchProjectDetail | null>(null)
  const [queries, setQueries] = useState<BuyerSearchQuery[]>([])
  const [fleet, setFleet] = useState<BuyerSearchFleet | null>(null)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [projectPageTotal, setProjectPageTotal] = useState<number | null>(null)
  const [projectRunTotal, setProjectRunTotal] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [streamState, setStreamState] = useState<StreamState>('disconnected')
  const [streamDiagnostics, setStreamDiagnostics] = useState<StreamDiagnostics | null>(null)
  const [dataCheckedAt, setDataCheckedAt] = useState<{ runId: string; at: number } | null>(null)
  const [clock, setClock] = useState(Date.now())
  const [reconnectKey, setReconnectKey] = useState(0)
  const streamCursorRef = useRef<{ runId: string; seq: number } | null>(null)
  const loadRequestRef = useRef(0)
  const fetchAbortRef = useRef<AbortController | null>(null)

  const selectedRun = useMemo(() => runs.find((run) => run.run_id === view.selected_run_id) ?? null, [runs, view.selected_run_id])
  const filters = useMemo(() => filtersFromRecord(view.project_filters), [view.project_filters])
  const selectedProject = projectDetail?.project_id === view.selected_project_id ? projectDetail : null

  const loadRuns = useCallback(async () => {
    try {
      const page = await buyerSearchApi.listRuns({ limit: 100 })
      setRuns(page.items)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить запуски')
    }
  }, [])

  const projectOptions = useMemo<BuyerSearchProjectListOptions>(() => ({
    cursor: view.cursor || undefined,
    limit: 60,
    sort: view.project_sort,
    shortlist: view.active_section === 'shortlist' ? 'shortlisted' : undefined,
    text: filters.text || undefined,
    minBudget: optionalNumber(filters.minBudget),
    maxBudget: optionalNumber(filters.maxBudget),
    maxOffers: optionalNumber(filters.maxOffers),
    minScore: optionalNumber(filters.minScore),
    minBuyerHiredPercent: optionalNumber(filters.minBuyerHiredPercent),
    maxAgeSeconds: optionalNumber(filters.maxAgeHours) == null ? undefined : Number(filters.maxAgeHours) * 3600,
    categoryId: optionalNumber(filters.categoryId),
    hasAttachments: filters.hasAttachments || undefined,
    unseen: filters.unseen || undefined,
    proposalState: view.active_section === 'outreach'
      ? ['draft', 'queued', 'sent', 'sending', 'accepted', 'failed']
      : filters.proposalState ? [filters.proposalState] : undefined,
    conversationState: filters.conversationState ? [filters.conversationState] : undefined,
  }), [filters, view.active_section, view.cursor, view.project_sort])

  const loadRunData = useCallback(async () => {
    if (!view.selected_run_id) return
    fetchAbortRef.current?.abort()
    const controller = new AbortController()
    fetchAbortRef.current = controller
    const request = ++loadRequestRef.current
    setLoading(true)
    try {
      const [projectPage, queryPage, fleetState] = await Promise.all([
        buyerSearchApi.listProjects(view.selected_run_id, { ...projectOptions, signal: controller.signal }),
        buyerSearchApi.listQueries(view.selected_run_id, undefined, { signal: controller.signal }),
        buyerSearchApi.getFleet(view.selected_run_id, controller.signal),
      ])
      if (request !== loadRequestRef.current) return
      setProjects(projectPage.items)
      setNextCursor(projectPage.next_cursor)
      setProjectPageTotal(typeof projectPage.total === 'number' ? projectPage.total : null)
      setProjectRunTotal(typeof projectPage.run_total === 'number' ? projectPage.run_total : null)
      setQueries(queryPage.items)
      setFleet(fleetState)
      setDataCheckedAt({ runId: view.selected_run_id, at: Date.now() })
      setError(null)
    } catch (nextError) {
      if (controller.signal.aborted) return
      if (request === loadRequestRef.current) setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить данные поиска')
    } finally {
      if (request === loadRequestRef.current) setLoading(false)
    }
  }, [projectOptions, view.selected_run_id])

  // The active workspace must follow the durable run event stream, including
  // discoveries made while the operator is reading a project or monitoring.
  // Keep a stable socket when filters change, but reconcile with the newest
  // filter callback so slow responses cannot replace a newer selection.
  const loadRunsRef = useRef(loadRuns)
  const loadRunDataRef = useRef(loadRunData)
  const sectionRef = useRef(view.active_section)
  loadRunsRef.current = loadRuns
  loadRunDataRef.current = loadRunData
  sectionRef.current = view.active_section

  useEffect(() => {
    if (!ready || !view.selected_run_id) {
      setStreamState('disconnected')
      setStreamDiagnostics(null)
      return
    }
    const runId = view.selected_run_id
    let disposed = false
    let afterSeq = streamCursorRef.current?.runId === runId ? streamCursorRef.current.seq : 0
    let attempt = 0
    let socket: WebSocket | null = null
    let reconcileTimer: number | null = null
    let reconnectTimer: number | null = null
    let handshakeTimer: number | null = null
    let pollTimer: number | null = null
    let eventUiTimer: number | null = null
    let pendingEventDiagnostics: Partial<StreamDiagnostics> = {}
    let pollInFlight = false
    const pollController = new AbortController()
    let sourceObservations = streamDiagnostics?.runId === runId ? streamDiagnostics.sourceObservations : []
    setStreamDiagnostics((current) => current?.runId === runId
      ? { ...current, connectionError: null }
      : { runId, cursor: afterSeq, lastEventType: null, lastEventAt: null, lastCheckedAt: null, checkError: null, connectionError: null, lastIssue: null, sourceObservations: [] })

    const reconcile = () => {
      if (reconcileTimer !== null) return
      // One refresh per burst, even when the backend produces many page events.
      reconcileTimer = window.setTimeout(() => {
        reconcileTimer = null
        if (disposed) return
        void loadRunsRef.current()
        if (sectionRef.current !== 'setup') void loadRunDataRef.current()
      }, 800)
    }
    const updateDiagnostics = (updates: Partial<StreamDiagnostics>) => {
      if (disposed) return
      setStreamDiagnostics((current) => current?.runId === runId ? { ...current, ...updates } : current)
    }
    const receiveEvent = (event: BuyerSearchEvent) => {
      if (!event || typeof event.type !== 'string' || !event.payload || typeof event.payload !== 'object' ||
        !Number.isSafeInteger(event.seq) || event.seq <= afterSeq) return
      afterSeq = event.seq
      streamCursorRef.current = { runId, seq: afterSeq }
      const at = eventTimestamp(event.created_at)
      const details: Partial<StreamDiagnostics> = { cursor: afterSeq, lastEventType: event.type, lastEventAt: at }
      if (['query.page.failed', 'query.page.retry', 'run.failed', 'run.blocked', 'runtime.warning', 'worker.failed'].includes(event.type)) {
        const source = event.payload.source
        const kind = event.payload.failure_kind
        details.lastIssue = {
          type: event.type,
          source: typeof source === 'string' ? source : null,
          kind: typeof kind === 'string' ? kind : null,
          at,
        }
      }
      const source = event.payload.source
      if (typeof source === 'string' && source.trim() && event.type.startsWith('query.page.')) {
        sourceObservations = [{ source, type: event.type, at }, ...sourceObservations.filter((item) => item.source !== source)].slice(0, 8)
        details.sourceObservations = sourceObservations
      }
      // Backlog replays can contain thousands of messages. Coalesce display
      // updates without delaying the durable sequence cursor or reconciliation.
      pendingEventDiagnostics = { ...pendingEventDiagnostics, ...details }
      if (eventUiTimer === null) {
        eventUiTimer = window.setTimeout(() => {
          eventUiTimer = null
          updateDiagnostics(pendingEventDiagnostics)
          pendingEventDiagnostics = {}
        }, 200)
      }
      reconcile()
    }
    // Durable REST catch-up also detects lost/quiet WebSocket connections.
    // Empty event batches mean "nothing new" rather than "source unhealthy".
    const checkDurableEvents = async () => {
      if (disposed || pollInFlight) return
      pollInFlight = true
      try {
        for (let pageNumber = 0; pageNumber < 3; pageNumber += 1) {
          const page = await buyerSearchApi.getRunEvents(runId, afterSeq, pollController.signal)
          if (disposed) return
          page.items.forEach(receiveEvent)
          if (page.items.length < 500) break
        }
        updateDiagnostics({ lastCheckedAt: Date.now(), checkError: null })
        reconcile()
      } catch (nextError) {
        if (!disposed && !pollController.signal.aborted) {
          updateDiagnostics({ checkError: nextError instanceof Error ? nextError.message : 'REST-проверка недоступна' })
        }
      } finally {
        pollInFlight = false
      }
    }
    const scheduleReconnect = (reason: string) => {
      if (disposed || reconnectTimer !== null) return
      updateDiagnostics({ connectionError: reason })
      setStreamState('reconnecting')
      const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt++, 4))
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        connect()
      }, delay)
      // Do not wait for the socket to recover before reconciling persisted data.
      void checkDurableEvents()
    }
    const connect = () => {
      if (disposed) return
      setStreamState(attempt ? 'reconnecting' : 'connecting')
      let nextSocket: WebSocket
      try {
        nextSocket = new WebSocket(buyerSearchApi.runEventsUrl(runId, afterSeq))
      } catch {
        scheduleReconnect('Не удалось открыть WebSocket. Проверяем сохранённые события через API.')
        return
      }
      socket = nextSocket
      nextSocket.onopen = () => {
        if (disposed || socket !== nextSocket) return
        // The API accepts the socket before it validates the run. Do not
        // report a live stream or reset backoff until its snapshot arrives.
        handshakeTimer = window.setTimeout(() => {
          handshakeTimer = null
          if (!disposed && socket === nextSocket) nextSocket.close()
        }, 10_000)
      }
      nextSocket.onmessage = (message) => {
        if (disposed || socket !== nextSocket) return
        let payload: BuyerSearchWebSocketMessage
        try { payload = JSON.parse(String(message.data)) as BuyerSearchWebSocketMessage } catch { return }
        if (payload.type === 'snapshot' && payload.run?.run_id === runId) {
          if (handshakeTimer !== null) window.clearTimeout(handshakeTimer)
          handshakeTimer = null
          attempt = 0
          setStreamState('live')
          updateDiagnostics({ connectionError: null })
          setRuns((current) => [payload.run, ...current.filter((run) => run.run_id !== payload.run.run_id)])
          reconcile()
        } else if (payload.type === 'event' && payload.event) {
          receiveEvent(payload.event)
        }
      }
      nextSocket.onclose = (event) => {
        if (disposed || socket !== nextSocket) return
        if (handshakeTimer !== null) window.clearTimeout(handshakeTimer)
        handshakeTimer = null
        if (event.code === 1008) {
          setStreamState('disconnected')
          updateDiagnostics({ connectionError: 'Запуск не найден или доступ запрещён. Обновите список запусков или выберите другой.' })
          return
        }
        scheduleReconnect('Поток событий прерван. Переподключаемся; сохранённые события проверяются через API.')
      }
      nextSocket.onerror = () => nextSocket.close()
    }
    const onOnline = () => {
      if (socket?.readyState === WebSocket.OPEN || disposed) return
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (handshakeTimer !== null) window.clearTimeout(handshakeTimer)
      handshakeTimer = null
      reconnectTimer = null
      socket?.close()
      connect()
      void checkDurableEvents()
    }
    connect()
    pollTimer = window.setInterval(() => void checkDurableEvents(), 15_000)
    window.addEventListener('online', onOnline)
    return () => {
      disposed = true
      if (reconcileTimer !== null) window.clearTimeout(reconcileTimer)
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (handshakeTimer !== null) window.clearTimeout(handshakeTimer)
      if (pollTimer !== null) window.clearInterval(pollTimer)
      if (eventUiTimer !== null) window.clearTimeout(eventUiTimer)
      pollController.abort()
      window.removeEventListener('online', onOnline)
      socket?.close()
      setStreamState('disconnected')
      fetchAbortRef.current?.abort()
      // Invalidate requests made for a different run before they can render.
      loadRequestRef.current += 1
    }
  }, [ready, view.selected_run_id, reconnectKey])

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 10_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => { if (ready) void loadRuns() }, [loadRuns, ready])
  useEffect(() => {
    if (!ready) return
    void api.listKworkRegistrationAccounts()
      .then((result) => setAccounts(result.accounts))
      .catch(() => setAccounts([]))
  }, [ready])
  useEffect(() => { if (ready && view.selected_run_id && view.active_section !== 'setup') void loadRunData() }, [loadRunData, ready, view.active_section, view.selected_run_id])

  useEffect(() => {
    if (!view.selected_run_id || !view.selected_project_id || !view.inspector_open) {
      setProjectDetail(null)
      return
    }
    const controller = new AbortController()
    buyerSearchApi.getProject(view.selected_run_id, view.selected_project_id, controller.signal)
      .then(setProjectDetail)
      .catch((nextError) => { if (!(nextError instanceof DOMException && nextError.name === 'AbortError')) setError(String(nextError)) })
    return () => controller.abort()
  }, [view.inspector_open, view.selected_project_id, view.selected_run_id])

  const createRun = async () => {
    const preview = builder.preview_queries.length
      ? builder.preview_queries.filter((query) => query.enabled !== false && query.text.trim()).map((query) => query.text.trim())
      : builder.exact_queries.map((query) => query.trim()).filter(Boolean)
    const categoryScopes = builder.taxonomy_selections.map((item) => item.category_scope || item).filter(Boolean)
    const payload: CreateBuyerSearchRunPayload = {
      name: builder.name.trim() || `Поиск ${new Date().toLocaleString('ru-RU')}`,
      mode: modeFor(builder.brief, builder.taxonomy_selections, preview),
      brief: builder.brief.trim() || undefined,
      category_scope: categoryScopes.length ? {
        category_ids: builder.taxonomy_selections.map((item) => item.category_id),
        category_names: builder.taxonomy_selections.map((item) => item.name),
        category_scopes: categoryScopes,
      } : undefined,
      filters: {
        min_budget: optionalNumber(builder.min_budget),
        max_budget: optionalNumber(builder.max_budget),
        max_offers: optionalNumber(builder.max_offers),
        min_buyer_hired_percent: optionalNumber(builder.min_buyer_hired_percent),
        max_age_seconds: optionalNumber(builder.max_age_hours) == null ? undefined : Number(builder.max_age_hours) * 3600,
      },
      requested_workers: builder.workers,
      query_batch_size: builder.query_batch_size,
      target_unique_projects: builder.target_projects,
      enrichment_policy: builder.enrichment_enabled
        ? { enabled: true, selection: 'manual' }
        : { enabled: false },
      scoring_profile_id: builder.scoring_profile_id.trim() || undefined,
      account_registration_ids: builder.account_registration_ids,
      queries: preview.map((text) => ({ text, rationale: 'Подтверждено оператором' })),
    }
    setBusy(true)
    try {
      const created = await buyerSearchApi.createRun(payload)
      let active = created
      let startWarning: string | null = null
      try {
        active = await buyerSearchApi.patchRun(created.run_id, { state: 'running' })
      } catch (startError) {
        startWarning = `Поиск создан, но не запущен: ${startError instanceof Error ? startError.message : String(startError)}. Проверьте мониторинг и запустите вручную.`
      }
      setRuns((current) => [active, ...current.filter((run) => run.run_id !== active.run_id)])
      updateView({ active_section: 'projects', selected_run_id: active.run_id, selected_project_id: null, cursor: null, cursor_history: [] })
      setError(startWarning)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось создать поиск')
    } finally {
      setBusy(false)
    }
  }

  const selectRun = (runId: string) => updateView({ selected_run_id: runId || null, selected_project_id: null, cursor: null, cursor_history: [], active_section: runId ? 'projects' : 'setup' })

  const setRunState = async (nextState: 'running' | 'paused' | 'stopped') => {
    if (!selectedRun) return
    setBusy(true)
    try {
      const updated = await buyerSearchApi.patchRun(selectedRun.run_id, { state: nextState })
      setRuns((current) => current.map((run) => run.run_id === updated.run_id ? updated : run))
      setError(null)
    } catch (nextError) { setError(nextError instanceof Error ? nextError.message : 'Не удалось изменить состояние') }
    finally { setBusy(false) }
  }

  const favorite = async (project: BuyerSearchProject | BuyerSearchProjectDetail) => {
    if (!selectedRun) return
    const shouldRemove = project.shortlist_state === 'shortlisted'
    await buyerSearchApi.batchProjectAction(selectedRun.run_id, { action: shouldRemove ? 'unshortlist' : 'shortlist', project_ids: [project.project_id], tags: view.shortlist_tags.split(',').map((tag) => tag.trim()).filter(Boolean), note: view.shortlist_note || undefined })
    setProjects((current) => current.map((item) => item.project_id === project.project_id ? { ...item, shortlist_state: shouldRemove ? 'none' : 'shortlisted' } : item))
    setProjectDetail((current) => current?.project_id === project.project_id ? { ...current, shortlist_state: shouldRemove ? 'none' : 'shortlisted' } : current)
  }

  const bulkFavorite = async () => {
    if (!selectedRun || !view.selected_project_ids.length) return
    setBusy(true)
    try {
      await buyerSearchApi.batchProjectAction(selectedRun.run_id, { action: 'shortlist', project_ids: view.selected_project_ids, tags: view.shortlist_tags.split(',').map((tag) => tag.trim()).filter(Boolean), note: view.shortlist_note || undefined })
      updateView({ selected_project_ids: [] })
      await loadRunData()
    } finally { setBusy(false) }
  }

  const exportProjects = async () => {
    if (!selectedRun) return
    setBusy(true)
    try {
      const record = await buyerSearchApi.createExport(selectedRun.run_id, { format: 'csv', selected_project_ids: view.selected_project_ids, include_attachments: view.include_attachments })
      let completed = record
      for (let attempt = 0; completed.state !== 'completed' && completed.state !== 'failed' && attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 350))
        completed = await buyerSearchApi.getExport(selectedRun.run_id, record.export_id)
      }
      if (completed.state === 'completed') window.open(buyerSearchApi.exportDownloadUrl(selectedRun.run_id, record.export_id), '_blank')
    } finally { setBusy(false) }
  }

  const section = view.active_section
  const showProjects = section === 'projects' || section === 'shortlist' || section === 'outreach'
  const diagnostics = streamDiagnostics?.runId === view.selected_run_id ? streamDiagnostics : null
  const lastDataAt = dataCheckedAt?.runId === view.selected_run_id ? dataCheckedAt.at : null
  const counters = selectedRun?.counters
  const execution = fleet?.run_id === view.selected_run_id ? fleet.metrics : undefined
  const capacityMessage = fleet?.run_id === view.selected_run_id
    ? fleet.capacity_reason || fleet.reason || fleet.capacity?.reasons.join('; ')
    : null
  const isDataStale = lastDataAt !== null && clock - lastDataAt > 45_000
  const streamText = streamState === 'live' ? 'WebSocket подключён'
    : streamState === 'connecting' ? 'Подключаем WebSocket…'
      : streamState === 'reconnecting' ? 'WebSocket восстанавливается'
        : 'WebSocket отключён'

  if (!ready) return <div className="buyer-workspace-loading"><Loader2 className="spin" />Восстанавливаем рабочее пространство…</div>

  return (
    <div className="buyer-workspace">
      <div className="buyer-commandbar">
        <div className="buyer-run-picker"><span className={`buyer-status-dot is-${selectedRun?.state || 'idle'}`} /><select value={view.selected_run_id || ''} onChange={(event) => selectRun(event.target.value)}><option value="">Новый поиск</option>{runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.name} · {run.state}</option>)}</select><ChevronDown /></div>
        <div className="buyer-save-status">{saveState === 'saving' ? 'Сохраняем…' : saveState === 'offline' ? 'Нет связи · сохранение ожидает' : 'Все изменения сохранены'}</div>
        {view.selected_run_id && <div className="buyer-save-status" role="status" aria-live="polite">{streamText}{diagnostics?.checkError ? ' · REST-проверка не прошла' : diagnostics?.lastCheckedAt ? ' · REST проверен' : ''}</div>}
        <button type="button" className="buyer-new-button" onClick={() => updateView({ active_section: 'setup', selected_project_id: null })}><Plus />Новый поиск</button>
      </div>

      {error && <div className="buyer-error-banner"><span>{error}</span><button type="button" onClick={() => setError(null)}>Закрыть</button></div>}

      {view.selected_run_id && (
        <section className="mx-3 my-2 rounded-md border border-white/10 bg-surface-950/50 p-3 text-xs text-zinc-300" aria-label="Диагностика поиска">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong className="text-sm text-white">Состояние поиска и свежесть данных</strong>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="btn btn-ghost py-1 text-xs" onClick={() => { void loadRuns(); if (section !== 'setup') void loadRunData() }}><RefreshCw className="h-3 w-3" />Обновить данные</button>
              {streamState !== 'live' && <button type="button" className="btn btn-ghost py-1 text-xs" onClick={() => setReconnectKey((value) => value + 1)}>Повторить подключение</button>}
            </div>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-zinc-400">
            <span>{streamText}</span>
            <span>События: {diagnostics?.lastEventType ? `${eventLabel(diagnostics.lastEventType)} · ${elapsedLabel(diagnostics.lastEventAt, clock)} (время сервера)` : 'пока не получены'}</span>
            <span>REST-журнал: {diagnostics?.lastCheckedAt ? `проверен ${elapsedLabel(diagnostics.lastCheckedAt, clock)}` : 'ещё не проверен'}</span>
            <span className={isDataStale ? 'text-amber-300' : ''}>Проекты/очередь: {lastDataAt ? `обновлены ${elapsedLabel(lastDataAt, clock)}` : 'ещё не загружены'}{isDataStale ? ' · могут устареть' : ''}</span>
            {diagnostics?.cursor ? <span>Последняя последовательность: #{diagnostics.cursor}</span> : null}
          </div>
          {streamState === 'live' && !diagnostics?.lastEventType && <p className="mt-2 text-zinc-500">Соединение установлено, но событий пока не получено. Отсутствие новых событий не означает сбой поиска.</p>}
          {streamState !== 'live' && <p className="mt-2 text-amber-300">Live-обновления недоступны. REST-проверка сохранённых событий продолжается каждые 15 секунд; для восстановления потока можно повторить подключение.</p>}
          {diagnostics?.connectionError && <p className="mt-2 text-amber-300">{diagnostics.connectionError}</p>}
          {diagnostics?.checkError && <p className="mt-2 text-amber-300">Не удалось проверить журнал событий: {diagnostics.checkError}. Проверьте доступность FastAPI и повторите обновление.</p>}
          <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1">
            {counters && (['queued_tasks', 'active_tasks', 'retry_tasks', 'completed_tasks', 'failed_tasks'] as const).map((key) => {
              const label = { queued_tasks: 'В очереди', active_tasks: 'В работе', retry_tasks: 'Ожидают повтора', completed_tasks: 'Завершено задач', failed_tasks: 'Ошибок задач' }[key]
              const value = counters[key]
              return typeof value === 'number' && Number.isFinite(value) ? <span key={key}>{label}: <strong className="text-white">{value}</strong></span> : null
            })}
            {execution && <span>Завершено страниц по учтённым задачам: <strong className="text-white">{execution.completed_requests}</strong></span>}
            {execution && execution.average_latency_ms !== null && execution.measured_latency_count > 0 && <span>Среднее время получения страницы: <strong className="text-white">{execution.average_latency_ms} мс</strong> ({execution.measured_latency_count} измерений)</span>}
            {execution && execution.measured_latency_count === 0 && <span>Время получения страниц: нет измерений</span>}
          </div>
          {execution && <div className="mt-2 text-zinc-400">По источникам (только завершённые страницы): {execution.sources.length
            ? execution.sources.map((source) => `${source.source}: ${source.pages} стр., ${source.projects_seen} наблюдений`).join(' · ')
            : 'пока нет завершённых страниц; это не статус доступности источников'}</div>}
          {diagnostics?.sourceObservations.length ? <div className="mt-1 text-zinc-400">Последние сохранённые сигналы источников: {diagnostics.sourceObservations.map((item) => `${item.source}: ${eventLabel(item.type)} (${elapsedLabel(item.at, clock)})`).join(' · ')}. Сигнал не является проверкой текущей доступности.</div> : null}
          {capacityMessage && <p className="mt-2 text-amber-300">Ограничение воркеров: {capacityMessage}. Проверьте состояние аккаунтов, транспорта и разрешённые лимиты.</p>}
          {selectedRun?.last_error && <p className="mt-2 text-amber-300">Последняя ошибка запуска: {selectedRun.last_error}. Подробности — в журнале и вкладке «Мониторинг».</p>}
          {diagnostics?.lastIssue && <p className="mt-2 text-amber-300">Последний проблемный сигнал: {eventLabel(diagnostics.lastIssue.type)}{diagnostics.lastIssue.source ? ` · ${diagnostics.lastIssue.source}` : ''}{diagnostics.lastIssue.kind ? ` · ${diagnostics.lastIssue.kind}` : ''}{diagnostics.lastIssue.at ? ` · ${elapsedLabel(diagnostics.lastIssue.at, clock)}` : ''}. При повторах соблюдаются ограничения площадки.</p>}
        </section>
      )}

      {section === 'setup' ? <SearchBuilder value={builder} accounts={accounts} busy={busy} onChange={updateBuilder} onCreate={createRun} /> : (
        <>
          <nav className="buyer-work-tabs">{SECTIONS.map(({ id, label, icon: Icon }) => <button type="button" key={id} className={section === id ? 'is-active' : ''} onClick={() => updateView({ active_section: id, cursor: null, cursor_history: [] })}><Icon />{label}</button>)}</nav>
          {showProjects && (
            <div className={`buyer-results-layout ${view.inspector_open ? 'has-inspector' : ''}`}>
              <ProjectFilters value={filters} onChange={(next) => updateView({ project_filters: { ...next }, cursor: null, cursor_history: [] })} />
              <main className="buyer-results-main">
                <div className="buyer-results-toolbar"><div><strong>{section === 'shortlist' ? 'Избранные проекты' : section === 'outreach' ? 'Работа с откликами' : 'Найденные проекты'}</strong><span>{projects.length} на странице{projectPageTotal != null ? ` · ${projectPageTotal} после фильтра` : ''}{projectRunTotal != null ? ` · ${projectRunTotal} в запуске` : ''}</span></div><div><select value={view.project_sort} onChange={(event) => updateView({ project_sort: event.target.value as typeof view.project_sort, cursor: null, cursor_history: [] })}><option value="score_desc">По оценке</option><option value="first_seen_desc">Недавно обнаружены</option><option value="remote_updated_desc">Обновлены на Kwork</option><option value="updated_desc">Недавно перепроверены</option><option value="budget_desc">Бюджет выше</option><option value="offers_asc">Меньше откликов</option><option value="matched_queries_desc">Больше совпадений</option></select><button type="button" title="Обновить" onClick={() => void loadRunData()}><RefreshCw /></button><button type="button" title="Экспорт всей выдачи" onClick={exportProjects}><Download /></button></div></div>
                {section !== 'outreach' && view.selected_project_ids.length > 0 && <div className="buyer-bulkbar"><strong>{view.selected_project_ids.length} выбрано</strong><input value={view.shortlist_tags} onChange={(event) => updateView({ shortlist_tags: event.target.value })} placeholder="Метки через запятую" /><input value={view.shortlist_note} onChange={(event) => updateView({ shortlist_note: event.target.value })} placeholder="Заметка" /><label><input type="checkbox" checked={view.include_attachments} onChange={(event) => updateView({ include_attachments: event.target.checked })} /> вложения</label><button type="button" onClick={bulkFavorite}><Heart />В избранное</button><button type="button" onClick={exportProjects}><Download />Экспорт</button></div>}
                <ProjectFeed mode={section} items={projects} selectedId={view.selected_project_id} checkedIds={view.selected_project_ids} loading={loading} onOpen={(project) => updateView({ selected_project_id: project.project_id, inspector_open: true, inspector_tab: section === 'outreach' ? 'proposal' : view.inspector_tab })} onToggle={(projectId) => updateView({ selected_project_ids: view.selected_project_ids.includes(projectId) ? view.selected_project_ids.filter((id) => id !== projectId) : [...view.selected_project_ids, projectId] })} onFavorite={(project) => void favorite(project)} />
                <div className="buyer-pagination"><button type="button" disabled={!view.cursor_history.length} onClick={() => { const history = [...view.cursor_history]; const cursor = history.pop() || null; updateView({ cursor, cursor_history: history }) }}>Назад</button><button type="button" disabled={!nextCursor} onClick={() => updateView({ cursor_history: [...view.cursor_history, view.cursor || ''], cursor: nextCursor })}>Следующая</button></div>
              </main>
              {view.inspector_open && selectedRun && <ProjectInspector runId={selectedRun.run_id} project={selectedProject} loading={Boolean(view.selected_project_id && !selectedProject)} tab={view.inspector_tab} accountId={builder.account_registration_ids[0]} onTab={(tab) => updateView({ inspector_tab: tab })} onClose={() => updateView({ inspector_open: false })} onFavorite={() => selectedProject && void favorite(selectedProject)} />}
            </div>
          )}
          {section === 'monitoring' && <SearchMonitor run={selectedRun} fleet={fleet} queries={queries} busy={busy} onState={(next) => void setRunState(next)} onRefresh={() => void loadRunData()} onToggleQuery={(query) => void buyerSearchApi.patchQuery(query.run_id, query.query_id, { enabled: !query.enabled }).then(() => loadRunData())} />}
        </>
      )}
    </div>
  )
}
