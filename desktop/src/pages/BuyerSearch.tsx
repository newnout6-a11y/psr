import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  ChevronLeft,
  ChevronRight,
  Check,
  Download,
  Eye,
  FileSearch,
  GripVertical,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  WifiOff,
  X,
} from 'lucide-react'

import { buyerSearchApi } from '../features/buyer-search/api'
import { BuyerConversationWorkspace } from '../features/buyer-search/BuyerConversationWorkspace'
import { BuyerOutreachWorkspace } from '../features/buyer-search/BuyerOutreachWorkspace'
import { BuyerTaxonomyPlanner } from '../features/buyer-search/BuyerTaxonomyPlanner'
import type { BuyerTaxonomySelection } from '../features/buyer-search/taxonomy-types'
import type {
  BuyerRunMode,
  BuyerSearchFleet,
  BuyerSearchFleetWorker,
  BuyerSearchEnrichmentResult,
  BuyerSearchExport,
  BuyerSearchFacets,
  BuyerSearchProject,
  BuyerSearchProjectDetail,
  BuyerSearchProjectListOptions,
  BuyerSearchProjectSort,
  BuyerSearchQuery,
  BuyerSearchQueryCollisionReport,
  BuyerSearchQueryBundles,
  BuyerSearchQueryGenerationResult,
  BuyerSearchQueryPatch,
  BuyerSearchRun,
  BuyerSearchWebSocketMessage,
  CreateBuyerSearchRunPayload,
} from '../features/buyer-search/types'

const MODE_LABELS: Record<BuyerRunMode, string> = {
  brief: 'Описание',
  category: 'Рубрика',
  manual: 'Запросы',
  hybrid: 'Описание + рубрика',
}

const RUN_STATE_LABELS: Record<BuyerSearchRun['state'], string> = {
  draft: 'Черновик',
  planning: 'Планирование',
  ready: 'Готов',
  running: 'В работе',
  pausing: 'Пауза',
  paused: 'На паузе',
  completing: 'Завершение',
  completed: 'Завершен',
  stopping: 'Остановка',
  stopped: 'Остановлен',
  blocked: 'Заблокирован',
  failed: 'Ошибка',
}

const STATE_CLASS: Record<BuyerSearchRun['state'], string> = {
  draft: 'border-zinc-600 text-zinc-300',
  planning: 'border-cyan-500/60 text-cyan-200',
  ready: 'border-cyan-500/60 text-cyan-200',
  running: 'border-lime-500/60 text-lime-200',
  pausing: 'border-amber-500/60 text-amber-200',
  paused: 'border-amber-500/60 text-amber-200',
  completing: 'border-cyan-500/60 text-cyan-200',
  completed: 'border-lime-500/60 text-lime-200',
  stopping: 'border-amber-500/60 text-amber-200',
  stopped: 'border-zinc-600 text-zinc-300',
  blocked: 'border-rose-500/60 text-rose-200',
  failed: 'border-rose-500/60 text-rose-200',
}

const DISPLAY_STATE_LABELS: Record<string, string> = {
  active: 'активен',
  approved: 'подтвержден',
  assigned: 'назначен',
  completed: 'завершен',
  disabled: 'отключен',
  exhausted: 'исчерпан',
  failed: 'ошибка',
  generated: 'сгенерирован',
  idle: 'ожидает',
  pending: 'ожидает',
  pending_send: 'ожидает отправки',
  ready: 'готов',
  rejected: 'отклонен',
  removed: 'убрано из избранного',
  running: 'в работе',
  shortlisted: 'в избранном',
  stopped: 'остановлен',
  unknown: 'неизвестно',
}

const PROJECT_SORT_OPTIONS: Array<{ value: BuyerSearchProjectSort; label: string }> = [
  { value: 'score_desc', label: 'Оценка: по убыванию' },
  { value: 'updated_desc', label: 'Обновление: сначала новые' },
  { value: 'budget_desc', label: 'Бюджет: по убыванию' },
  { value: 'offers_asc', label: 'Отклики: сначала меньше' },
  { value: 'views_desc', label: 'Просмотры: по убыванию' },
  { value: 'attachment_count_desc', label: 'Вложения: по убыванию' },
  { value: 'matched_queries_desc', label: 'Совпадения: по убыванию' },
]

const DEFAULT_PROJECT_SORT: BuyerSearchProjectSort = 'score_desc'
const PROJECT_PAGE_SIZE = 100
const DEFAULT_PROJECT_MAX_BUDGET = 10_000
const BUYER_PANE_LAYOUT_STORAGE_KEY = 'psr.buyer-search.pane-layout.v1'
const DEFAULT_RUNS_PANE_WIDTH = 260
const DEFAULT_INSPECTOR_PANE_WIDTH = 340
const MIN_RUNS_PANE_WIDTH = 220
const MAX_RUNS_PANE_WIDTH = 480
const MIN_INSPECTOR_PANE_WIDTH = 280
const MAX_INSPECTOR_PANE_WIDTH = 600
const MIN_WORKSPACE_PANE_WIDTH = 340
const BUYER_PANE_DIVIDER_WIDTH = 16

type BuyerPaneSide = 'runs' | 'inspector'

interface BuyerPaneLayout {
  runs: number
  inspector: number
}

interface BuyerPaneResizeSession {
  side: BuyerPaneSide
  startX: number
  startLayout: BuyerPaneLayout
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max)
}

function normalizeBuyerPaneLayout(layout: BuyerPaneLayout, containerWidth?: number): BuyerPaneLayout {
  let runs = clamp(Math.round(layout.runs), MIN_RUNS_PANE_WIDTH, MAX_RUNS_PANE_WIDTH)
  let inspector = clamp(Math.round(layout.inspector), MIN_INSPECTOR_PANE_WIDTH, MAX_INSPECTOR_PANE_WIDTH)

  if (!containerWidth) {
    return { runs, inspector }
  }

  const maxCombinedWidth = Math.max(
    MIN_RUNS_PANE_WIDTH + MIN_INSPECTOR_PANE_WIDTH,
    containerWidth - MIN_WORKSPACE_PANE_WIDTH - BUYER_PANE_DIVIDER_WIDTH,
  )
  const overflow = runs + inspector - maxCombinedWidth

  if (overflow > 0) {
    const inspectorReduction = Math.min(overflow, inspector - MIN_INSPECTOR_PANE_WIDTH)
    inspector -= inspectorReduction
    runs = Math.max(MIN_RUNS_PANE_WIDTH, runs - (overflow - inspectorReduction))
  }

  return { runs, inspector }
}

function getStoredBuyerPaneLayout(): BuyerPaneLayout {
  const fallback = {
    runs: DEFAULT_RUNS_PANE_WIDTH,
    inspector: DEFAULT_INSPECTOR_PANE_WIDTH,
  }

  if (typeof window === 'undefined') {
    return fallback
  }

  try {
    const rawLayout = window.localStorage.getItem(BUYER_PANE_LAYOUT_STORAGE_KEY)
    if (!rawLayout) {
      return fallback
    }

    const savedLayout = JSON.parse(rawLayout) as Partial<BuyerPaneLayout>
    if (!Number.isFinite(savedLayout.runs) || !Number.isFinite(savedLayout.inspector)) {
      return fallback
    }

    return normalizeBuyerPaneLayout({
      runs: savedLayout.runs as number,
      inspector: savedLayout.inspector as number,
    })
  } catch {
    return fallback
  }
}

function storeBuyerPaneLayout(layout: BuyerPaneLayout) {
  if (typeof window === 'undefined') {
    return
  }

  try {
    window.localStorage.setItem(BUYER_PANE_LAYOUT_STORAGE_KEY, JSON.stringify(layout))
  } catch {
    // Preferences remain usable even when browser storage is unavailable.
  }
}

type UrlChanges = Record<string, string | number | null | undefined>

interface ProjectFilterDraft {
  minScore: string
  maxBudget: string
  maxOffers: string
  category: string
  shortlist: string
}

function formatNumber(value: number | null | undefined): string {
  return typeof value === 'number' ? new Intl.NumberFormat('ru-RU').format(value) : '—'
}

function formatRefreshTime(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return null
  return timestamp.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${seconds} с`
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч`
  return `${Math.round(seconds / 86400)} д`
}

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—'
  if (value < 1024) return `${value} Б`
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`
  return `${(value / (1024 * 1024)).toFixed(1)} МБ`
}

function formatDetailValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function parseFiniteNumber(value: string | null, minimum = Number.NEGATIVE_INFINITY): number | undefined {
  if (!value?.trim()) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= minimum ? parsed : undefined
}

function parseInteger(value: string | null, minimum = 0): number | undefined {
  const parsed = parseFiniteNumber(value, minimum)
  return parsed !== undefined && Number.isInteger(parsed) ? parsed : undefined
}

function parseProjectSort(value: string | null): BuyerSearchProjectSort {
  return PROJECT_SORT_OPTIONS.some((option) => option.value === value)
    ? (value as BuyerSearchProjectSort)
    : DEFAULT_PROJECT_SORT
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function projectFilterDraftFromUrl(searchParams: URLSearchParams): ProjectFilterDraft {
  return {
    minScore: searchParams.get('minScore') ?? '',
    maxBudget: searchParams.get('maxBudget') ?? String(DEFAULT_PROJECT_MAX_BUDGET),
    maxOffers: searchParams.get('maxOffers') ?? '',
    category: searchParams.get('category') ?? '',
    shortlist: searchParams.get('shortlist') ?? '',
  }
}

function RunState({ state }: { state: BuyerSearchRun['state'] }) {
  return <span className={`inline-flex border px-2 py-0.5 text-[11px] font-medium ${STATE_CLASS[state]}`}>{RUN_STATE_LABELS[state]}</span>
}

function displayState(value: string | null | undefined): string {
  if (!value) return '—'
  return DISPLAY_STATE_LABELS[value] ?? value
}

function displayRunName(name: string): string {
  if (/^buyer search final smoke$/i.test(name)) return 'Поиск заказов: финальная проверка'
  if (/^validation smoke$/i.test(name)) return 'Проверочный запуск'
  if (/^buyer api smoke$/i.test(name)) return 'Проверка API поиска заказов'
  return name.replace(/^buyer search\b/i, 'Поиск заказов')
}

function Metric({
  label,
  value,
  tone = 'text-zinc-100',
  hint,
}: {
  label: string
  value: string | number
  tone?: string
  hint?: string
}) {
  return (
    <div className="border-l border-surface-700 px-3 first:border-l-0">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 font-mono text-sm tabular-nums ${tone}`}>{value}</div>
      {hint ? <div className="mt-1 min-h-3 truncate text-[10px] text-zinc-500" title={hint}>{hint}</div> : null}
    </div>
  )
}

function fleetCapacityReason(fleet: BuyerSearchFleet | null): string | null {
  if (!fleet) return 'Загрузка состояния доступной мощности'
  if (fleet.capacity_reason) return localizedFleetReason(fleet.capacity_reason)
  if (fleet.reason) return localizedFleetReason(fleet.reason)
  if (fleet.capacity?.reasons.length) return fleet.capacity.reasons.map(localizedFleetReason).join(', ')
  if (fleet.live_discovery_enabled === false) return 'Поиск в реальном времени отключен'
  return null
}

function localizedFleetReason(reason: string): string {
  const normalized = reason.trim().toLowerCase()
  if (normalized === 'query_queue_exhausted') return 'Текущая пачка запросов исчерпана'
  if (normalized === 'sources_exhausted') return 'Новых результатов по всем запросам больше нет'
  if (normalized === 'target_reached') return 'Цель поиска достигнута'
  if (normalized === 'auto_batch_limit') return 'Достигнут лимит автоматических пачек запросов'
  if (normalized === 'live discovery disabled by feature flag') return 'Поиск в реальном времени отключен флагом функции'
  if (normalized.startsWith('no approved durable shadow gate for ')) return 'Нет подтвержденного допуска для запуска воркеров'
  if (normalized.includes('no idle buyer worker')) return 'Нет свободного воркера с маршрутом VPNTE'
  if (normalized.includes('strict capacity preflight did not satisfy requested workers')) return 'Недостаточно свободных воркеров для выбранного количества'
  if (normalized === 'unique_account_capacity') return 'Не хватает свободных готовых аккаунтов Kwork'
  if (normalized === 'unique_transport_capacity') return 'Не хватает свободных маршрутов VPNTE'
  if (normalized === 'unique_egress_capacity') return 'Недостаточно разных внешних IP VPNTE'
  return reason
}

function categoryNamesFromRun(run: BuyerSearchRun | null): string[] {
  if (!run) return []
  const scope = run.category_scope
  const names: string[] = []
  const append = (value: unknown) => {
    if (typeof value !== 'string' || !value.trim()) return
    const name = value.trim()
    if (!names.includes(name)) names.push(name)
  }

  if (Array.isArray(scope.category_names)) scope.category_names.forEach(append)
  if (Array.isArray(scope.category_scopes)) {
    scope.category_scopes.forEach((item) => {
      if (item && typeof item === 'object') append((item as Record<string, unknown>).category_name)
    })
  }
  append(scope.category_name)
  return names
}

function categoryNameForQuery(run: BuyerSearchRun | null, categoryId: number | null): string | null {
  if (!run || categoryId === null) return null
  const scope = run.category_scope
  const ids = Array.isArray(scope.category_ids) ? scope.category_ids : []
  const names = Array.isArray(scope.category_names) ? scope.category_names : []
  const index = ids.findIndex((value) => Number(value) === categoryId)
  if (index >= 0 && typeof names[index] === 'string' && names[index].trim()) return names[index].trim()

  if (Array.isArray(scope.category_scopes)) {
    const match = scope.category_scopes.find((item) => (
      item
      && typeof item === 'object'
      && Number((item as Record<string, unknown>).category_id) === categoryId
    )) as Record<string, unknown> | undefined
    if (typeof match?.category_name === 'string' && match.category_name.trim()) return match.category_name.trim()
  }

  if (Number(scope.category_id) === categoryId && typeof scope.category_name === 'string' && scope.category_name.trim()) {
    return scope.category_name.trim()
  }
  return null
}

function fleetWorkerTask(worker: BuyerSearchFleetWorker): string {
  if (typeof worker.current_task === 'string' && worker.current_task) return worker.current_task
  if (worker.current_task && typeof worker.current_task === 'object') {
    return worker.current_task.query_text ?? worker.current_task.title ?? worker.current_task.task_id ?? worker.current_task.query_id ?? 'активна'
  }
  return worker.current_task_id ?? worker.task_id ?? '—'
}

function projectObservationAccount(project: BuyerSearchProjectDetail | null): string {
  const observations = project?.observations
  if (!observations) return ''
  for (const observation of observations) {
    const account = observation.account_registration_id
    if (typeof account === 'string' && account.trim()) return account.trim()
  }
  return ''
}

export default function BuyerSearch() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [runs, setRuns] = useState<BuyerSearchRun[]>([])
  const [queries, setQueries] = useState<BuyerSearchQuery[]>([])
  const [projects, setProjects] = useState<BuyerSearchProject[]>([])
  const [fleet, setFleet] = useState<BuyerSearchFleet | null>(null)
  const [facets, setFacets] = useState<BuyerSearchFacets | null>(null)
  const [selectedProjectDetail, setSelectedProjectDetail] = useState<BuyerSearchProjectDetail | null>(null)
  const [accountRegistrationId, setAccountRegistrationId] = useState('')
  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>([])
  const [shortlistTags, setShortlistTags] = useState('')
  const [shortlistNote, setShortlistNote] = useState('')
  const [projectActionId, setProjectActionId] = useState<string | null>(null)
  const [exportRecord, setExportRecord] = useState<BuyerSearchExport | null>(null)
  const [includeAttachmentsInExport, setIncludeAttachmentsInExport] = useState(false)
  const [lastEnrichment, setLastEnrichment] = useState<BuyerSearchEnrichmentResult | null>(null)
  const [lastQueryGeneration, setLastQueryGeneration] = useState<BuyerSearchQueryGenerationResult | null>(null)
  const [queryCollisionReport, setQueryCollisionReport] = useState<BuyerSearchQueryCollisionReport | null>(null)
  const [queryBundles, setQueryBundles] = useState<BuyerSearchQueryBundles | null>(null)
  const [selectedCollisionQueryIds, setSelectedCollisionQueryIds] = useState<string[]>([])
  const [finalScoreProfileId, setFinalScoreProfileId] = useState('')
  const [finalScoreProfileVersion, setFinalScoreProfileVersion] = useState('1')
  const [nextProjectCursor, setNextProjectCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [runDataLoading, setRunDataLoading] = useState(false)
  const [projectDetailLoading, setProjectDetailLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [action, setAction] = useState<'running' | 'paused' | 'stopped' | 'restart' | 'delete' | 'export' | 'distribute' | 'generate' | 'regenerate-collisions' | 'shortlist' | 'unshortlist' | null>(null)
  const [queryActionId, setQueryActionId] = useState<string | null>(null)
  const [editingQueryId, setEditingQueryId] = useState<string | null>(null)
  const [editingQueryText, setEditingQueryText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [statusMessage, setStatusMessage] = useState('')
  const [streamState, setStreamState] = useState<'offline' | 'connecting' | 'connected'>('offline')
  const [projectRefreshVersion, setProjectRefreshVersion] = useState(0)
  const [mode, setMode] = useState<BuyerRunMode>('manual')
  const [name, setName] = useState('')
  const [brief, setBrief] = useState('')
  const [manualQueries, setManualQueries] = useState('')
  const [categoryScope, setCategoryScope] = useState<Record<string, unknown>>({})
  const [selectedTaxonomySelections, setSelectedTaxonomySelections] = useState<BuyerTaxonomySelection[]>([])
  const [taxonomySelectionKey, setTaxonomySelectionKey] = useState(0)
  const [workers, setWorkers] = useState(2)
  const [targetProjects, setTargetProjects] = useState(100)
  const [maxProjectBudget, setMaxProjectBudget] = useState(String(DEFAULT_PROJECT_MAX_BUDGET))
  const [paneLayout, setPaneLayout] = useState<BuyerPaneLayout>(getStoredBuyerPaneLayout)
  const [paneResizeSession, setPaneResizeSession] = useState<BuyerPaneResizeSession | null>(null)
  const [paneContainerWidth, setPaneContainerWidth] = useState<number | undefined>(undefined)
  const cursorHistoryRef = useRef<string[]>([])
  const eventSequenceRef = useRef(0)
  const reconcileTimerRef = useRef<number | null>(null)
  const paneLayoutRef = useRef(paneLayout)
  const paneLayoutContainerRef = useRef<HTMLDivElement | null>(null)

  const getPaneContainerWidth = useCallback(() => paneLayoutContainerRef.current?.getBoundingClientRect().width, [])
  const constrainedPaneLayout = useMemo(
    () => normalizeBuyerPaneLayout(paneLayout, paneContainerWidth),
    [paneContainerWidth, paneLayout],
  )
  const commitPaneLayout = useCallback((layout: BuyerPaneLayout) => {
    const nextLayout = normalizeBuyerPaneLayout(layout, getPaneContainerWidth())
    paneLayoutRef.current = nextLayout
    setPaneLayout(nextLayout)
    return nextLayout
  }, [getPaneContainerWidth])

  const beginPaneResize = useCallback((side: BuyerPaneSide, event: ReactPointerEvent<HTMLDivElement>) => {
    if (!window.matchMedia('(min-width: 1280px)').matches) {
      return
    }

    event.preventDefault()
    setPaneResizeSession({
      side,
      startX: event.clientX,
      startLayout: normalizeBuyerPaneLayout(paneLayoutRef.current, getPaneContainerWidth()),
    })
  }, [getPaneContainerWidth])

  const adjustPaneWidth = useCallback((side: BuyerPaneSide, offset: number) => {
    const currentLayout = normalizeBuyerPaneLayout(paneLayoutRef.current, getPaneContainerWidth())
    const nextLayout = side === 'runs'
      ? { ...currentLayout, runs: currentLayout.runs + offset }
      : { ...currentLayout, inspector: currentLayout.inspector + offset }
    storeBuyerPaneLayout(commitPaneLayout(nextLayout))
  }, [commitPaneLayout, getPaneContainerWidth])

  const handlePaneResizeKeyDown = useCallback((side: BuyerPaneSide, event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return
    }

    event.preventDefault()
    const direction = event.key === 'ArrowRight' ? 16 : -16
    adjustPaneWidth(side, side === 'runs' ? direction : -direction)
  }, [adjustPaneWidth])

  useEffect(() => {
    if (!paneResizeSession) {
      return undefined
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handlePointerMove = (event: PointerEvent) => {
      const horizontalOffset = event.clientX - paneResizeSession.startX
      const nextLayout = paneResizeSession.side === 'runs'
        ? { ...paneResizeSession.startLayout, runs: paneResizeSession.startLayout.runs + horizontalOffset }
        : { ...paneResizeSession.startLayout, inspector: paneResizeSession.startLayout.inspector - horizontalOffset }
      commitPaneLayout(nextLayout)
    }

    const finishResize = () => {
      storeBuyerPaneLayout(commitPaneLayout(paneLayoutRef.current))
      setPaneResizeSession(null)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', finishResize, { once: true })
    window.addEventListener('pointercancel', finishResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', finishResize)
      window.removeEventListener('pointercancel', finishResize)
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [commitPaneLayout, paneResizeSession])

  useEffect(() => {
    const updatePaneContainerWidth = () => {
      const nextWidth = getPaneContainerWidth()
      setPaneContainerWidth((currentWidth) => currentWidth === nextWidth ? currentWidth : nextWidth)
    }

    updatePaneContainerWidth()
    window.addEventListener('resize', updatePaneContainerWidth)
    return () => window.removeEventListener('resize', updatePaneContainerWidth)
  }, [getPaneContainerWidth])

  const paneLayoutStyle = useMemo(() => ({
    '--buyer-runs-pane-width': `${constrainedPaneLayout.runs}px`,
    '--buyer-inspector-pane-width': `${constrainedPaneLayout.inspector}px`,
  }) as CSSProperties, [constrainedPaneLayout.inspector, constrainedPaneLayout.runs])

  const selectedRunId = searchParams.get('run')
  const selectedProjectId = searchParams.get('project')
  const projectSort = parseProjectSort(searchParams.get('sort'))
  const projectCursor = searchParams.get('cursor') || undefined
  const projectFilterDraft = useMemo(() => projectFilterDraftFromUrl(searchParams), [searchParams])
  const [filterDraft, setFilterDraft] = useState<ProjectFilterDraft>(() => projectFilterDraftFromUrl(searchParams))
  const filterSignature = `${projectFilterDraft.minScore}|${projectFilterDraft.maxBudget}|${projectFilterDraft.maxOffers}|${projectFilterDraft.category}|${projectFilterDraft.shortlist}`
  const minScore = parseFiniteNumber(projectFilterDraft.minScore)
  const maxBudget = parseFiniteNumber(projectFilterDraft.maxBudget, 0)
  const maxOffers = parseInteger(projectFilterDraft.maxOffers, 0)
  const selectedTaxonomyKworksTotal = useMemo(() => {
    if (!selectedTaxonomySelections.length || selectedTaxonomySelections.some((selection) => typeof selection.kworksCount !== 'number')) return null
    return selectedTaxonomySelections.reduce((total, selection) => total + (selection.kworksCount ?? 0), 0)
  }, [selectedTaxonomySelections])
  const categoryId = parseInteger(projectFilterDraft.category, 1)
  const selectedRun = useMemo(() => runs.find((run) => run.run_id === selectedRunId) ?? null, [runs, selectedRunId])
  const selectedRunCategoryNames = useMemo(() => categoryNamesFromRun(selectedRun), [selectedRun])
  const selectedFleet = useMemo(
    () => (fleet?.run_id === selectedRunId ? fleet : null),
    [fleet, selectedRunId],
  )
  const selectedProjectCard = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) ?? null,
    [projects, selectedProjectId],
  )
  const inspectorProject = selectedProjectDetail ?? selectedProjectCard
  const inferredProjectAccountId = useMemo(() => projectObservationAccount(selectedProjectDetail), [selectedProjectDetail])
  const selectedProjectIdSet = useMemo(() => new Set(selectedProjectIds), [selectedProjectIds])
  const selectedProjectCount = selectedProjectIds.length
  const projectCategoryOptions = useMemo(() => {
    const options = new Map<number, number>()
    for (const facet of facets?.categories ?? []) {
      const categoryId = parseInteger(String(facet.value ?? ''), 1)
      if (categoryId !== undefined) options.set(categoryId, facet.count)
    }
    return [...options.entries()]
      .map(([categoryId, count]) => ({ categoryId, count }))
      .sort((left, right) => left.categoryId - right.categoryId)
  }, [facets])
  const projectOptions = useMemo<BuyerSearchProjectListOptions>(
    () => ({
      cursor: projectCursor,
      limit: PROJECT_PAGE_SIZE,
      sort: projectSort,
      shortlist: projectFilterDraft.shortlist || undefined,
      minScore,
      maxBudget,
      maxOffers,
      categoryId,
    }),
    [categoryId, maxBudget, maxOffers, minScore, projectCursor, projectFilterDraft.shortlist, projectSort],
  )
  const projectScopeKey = `${selectedRunId ?? ''}|${projectSort}|${minScore ?? ''}|${maxBudget ?? ''}|${maxOffers ?? ''}|${categoryId ?? ''}|${projectFilterDraft.shortlist}`
  const exportFilters = useMemo<Record<string, unknown>>(() => {
    const filters: Record<string, unknown> = { ...(selectedRun?.filters ?? {}) }
    if (minScore !== undefined) filters.min_score = minScore
    if (maxBudget !== undefined) filters.max_budget = maxBudget
    if (maxOffers !== undefined) filters.max_offers = maxOffers
    if (categoryId !== undefined) filters.category_id = categoryId
    if (projectFilterDraft.shortlist) filters.shortlist_state = projectFilterDraft.shortlist
    return filters
  }, [categoryId, maxBudget, maxOffers, minScore, projectFilterDraft.shortlist, selectedRun?.filters])

  const updateUrl = useCallback(
    (changes: UrlChanges, replace = true) => {
      const next = new URLSearchParams(searchParams)
      for (const [key, value] of Object.entries(changes)) {
        if (value === undefined || value === null || value === '') next.delete(key)
        else next.set(key, String(value))
      }
      setSearchParams(next, { replace })
    },
    [searchParams, setSearchParams],
  )

  const loadRuns = useCallback(async (signal?: AbortSignal, quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const page = await buyerSearchApi.listRuns({ signal })
      if (signal?.aborted) return
      setRuns(page.items)
      setError(null)
    } catch (nextError) {
      if (signal?.aborted || isAbortError(nextError)) return
      setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить запуски')
    } finally {
      if (!signal?.aborted && !quiet) setLoading(false)
    }
  }, [])

  const loadRunData = useCallback(
    async (runId: string, signal?: AbortSignal, quiet = false) => {
      if (!quiet) setRunDataLoading(true)
      try {
        const [queryPage, projectPage, fleetSnapshot, facetSnapshot] = await Promise.all([
          buyerSearchApi.listQueries(runId, undefined, { signal }),
          buyerSearchApi.listProjects(runId, { ...projectOptions, signal }),
          buyerSearchApi.getFleet(runId, signal),
          buyerSearchApi.getFacets(runId, projectOptions.shortlist, signal),
        ])
        if (signal?.aborted) return
        setQueries(queryPage.items)
        setProjects(projectPage.items)
        setNextProjectCursor(projectPage.next_cursor)
        setFleet(fleetSnapshot)
        setFacets(facetSnapshot)
        setError(null)
      } catch (nextError) {
        if (signal?.aborted || isAbortError(nextError)) return
        setQueries([])
        setProjects([])
        setNextProjectCursor(null)
        setFleet(null)
        setFacets(null)
        setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить данные запуска')
      } finally {
        if (!signal?.aborted && !quiet) setRunDataLoading(false)
      }
    },
    [projectOptions],
  )

  const loadQueryPlanEvidence = useCallback(async (runId: string, signal?: AbortSignal) => {
    const [collisionResult, bundleResult] = await Promise.allSettled([
      buyerSearchApi.getQueryCollisions(runId, { limit: 500, signal }),
      buyerSearchApi.getQueryBundles(runId, false, signal),
    ])
    if (signal?.aborted) return
    setQueryCollisionReport(collisionResult.status === 'fulfilled' ? collisionResult.value : null)
    setQueryBundles(bundleResult.status === 'fulfilled' ? bundleResult.value : null)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void loadRuns(controller.signal)
    return () => controller.abort()
  }, [loadRuns])

  useEffect(() => {
    if (loading) return
    if (!runs.length) {
      if (selectedRunId) updateUrl({ run: null, project: null, cursor: null })
      return
    }
    if (!selectedRunId || !runs.some((run) => run.run_id === selectedRunId)) {
      updateUrl({ run: runs[0].run_id, project: null, cursor: null })
    }
  }, [loading, runs, selectedRunId, updateUrl])

  useEffect(() => {
    setFilterDraft(projectFilterDraft)
  }, [filterSignature])

  useEffect(() => {
    cursorHistoryRef.current = []
    setSelectedProjectIds([])
  }, [projectScopeKey])

  useEffect(() => {
    if (!selectedRunId) {
      setQueries([])
      setProjects([])
      setNextProjectCursor(null)
      setFleet(null)
      setFacets(null)
      setExportRecord(null)
      setLastQueryGeneration(null)
      setQueryCollisionReport(null)
      setQueryBundles(null)
      setSelectedCollisionQueryIds([])
      return
    }
    setExportRecord(null)
    setLastQueryGeneration(null)
    setQueryCollisionReport(null)
    setQueryBundles(null)
    setSelectedCollisionQueryIds([])
    const controller = new AbortController()
    void loadRunData(selectedRunId, controller.signal)
    void loadQueryPlanEvidence(selectedRunId, controller.signal)
    return () => controller.abort()
  }, [loadQueryPlanEvidence, loadRunData, selectedRunId])

  useEffect(() => {
    if (!selectedRunId || selectedRun?.state !== 'ready') return undefined

    let disposed = false
    const refreshCapacity = async () => {
      try {
        const snapshot = await buyerSearchApi.getFleet(selectedRunId)
        if (!disposed) setFleet(snapshot)
      } catch {
        // A transient capacity check must not clear the already rendered run data.
      }
    }

    void refreshCapacity()
    const timer = window.setInterval(() => {
      void refreshCapacity()
    }, 3_000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [selectedRun?.state, selectedRunId])

  useEffect(() => {
    if (!selectedRunId || !selectedProjectId) {
      setSelectedProjectDetail(null)
      setProjectDetailLoading(false)
      setLastEnrichment(null)
      return
    }
    const controller = new AbortController()
    setProjectDetailLoading(true)
    setSelectedProjectDetail(null)
    void buyerSearchApi
      .getProject(selectedRunId, selectedProjectId, controller.signal)
      .then((project) => {
        if (!controller.signal.aborted) {
          setSelectedProjectDetail(project)
          setError(null)
        }
      })
      .catch((nextError) => {
        if (!controller.signal.aborted && !isAbortError(nextError)) {
          setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить проект')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setProjectDetailLoading(false)
      })
    return () => controller.abort()
  }, [projectRefreshVersion, selectedProjectId, selectedRunId])

  useEffect(() => {
    setLastEnrichment(null)
  }, [selectedProjectId, selectedRunId])

  useEffect(() => {
    setAccountRegistrationId(inferredProjectAccountId)
  }, [inferredProjectAccountId, selectedProjectId])

  const loadRunsRef = useRef(loadRuns)
  const loadRunDataRef = useRef(loadRunData)
  const loadQueryPlanEvidenceRef = useRef(loadQueryPlanEvidence)
  useEffect(() => {
    loadRunsRef.current = loadRuns
    loadRunDataRef.current = loadRunData
    loadQueryPlanEvidenceRef.current = loadQueryPlanEvidence
  }, [loadQueryPlanEvidence, loadRunData, loadRuns])

  useEffect(() => {
    if (!selectedRunId) {
      eventSequenceRef.current = 0
      setStreamState('offline')
      return
    }

    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let reconnectAttempt = 0
    eventSequenceRef.current = 0

    const scheduleReconcile = () => {
      if (reconcileTimerRef.current !== null) return
      reconcileTimerRef.current = window.setTimeout(() => {
        reconcileTimerRef.current = null
        void loadRunsRef.current(undefined, true)
        void loadRunDataRef.current(selectedRunId, undefined, true)
        void loadQueryPlanEvidenceRef.current(selectedRunId)
      }, 180)
    }

    const connect = () => {
      if (disposed) return
      setStreamState('connecting')
      socket = new WebSocket(buyerSearchApi.runEventsUrl(selectedRunId, eventSequenceRef.current))
      socket.onopen = () => {
        reconnectAttempt = 0
        if (!disposed) setStreamState('connected')
      }
      socket.onmessage = (message) => {
        let payload: BuyerSearchWebSocketMessage
        try {
          payload = JSON.parse(String(message.data)) as BuyerSearchWebSocketMessage
        } catch {
          return
        }
        if (payload.type === 'snapshot') {
          eventSequenceRef.current = Math.max(eventSequenceRef.current, payload.after_seq || 0)
          setRuns((current) => [payload.run, ...current.filter((run) => run.run_id !== payload.run.run_id)])
          scheduleReconcile()
          return
        }
        if (payload.type === 'event') {
          eventSequenceRef.current = Math.max(eventSequenceRef.current, payload.event.seq || 0)
          scheduleReconcile()
        }
      }
      socket.onclose = () => {
        if (disposed) return
        setStreamState('connecting')
        if (reconnectTimer !== null) return
        const delay = Math.min(10_000, 1_000 * 2 ** reconnectAttempt)
        reconnectAttempt += 1
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null
          connect()
        }, delay)
      }
      socket.onerror = () => {
        // The close handler owns reconnect scheduling so failed handshakes are coalesced.
      }
    }

    connect()
    return () => {
      disposed = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      socket?.close()
      if (reconcileTimerRef.current !== null) {
        window.clearTimeout(reconcileTimerRef.current)
        reconcileTimerRef.current = null
      }
    }
  }, [selectedRunId])

  const selectRun = (runId: string) => {
    updateUrl({ run: runId, project: null, cursor: null }, false)
  }

  const selectProject = (projectId: string) => {
    updateUrl({ project: projectId }, false)
  }

  const applyProjectFilters = () => {
    const nextDraft = {
      minScore: parseFiniteNumber(filterDraft.minScore)?.toString() ?? '',
      maxBudget: parseFiniteNumber(filterDraft.maxBudget, 0)?.toString() ?? '',
      maxOffers: parseInteger(filterDraft.maxOffers, 0)?.toString() ?? '',
      category: parseInteger(filterDraft.category, 1)?.toString() ?? '',
      shortlist: filterDraft.shortlist,
    }
    cursorHistoryRef.current = []
    setFilterDraft(nextDraft)
    updateUrl(
      {
        minScore: nextDraft.minScore || null,
        maxBudget: nextDraft.maxBudget || null,
        maxOffers: nextDraft.maxOffers || null,
        category: nextDraft.category || null,
        shortlist: nextDraft.shortlist || null,
        cursor: null,
      },
      false,
    )
  }

  const clearProjectFilters = () => {
    cursorHistoryRef.current = []
    setFilterDraft({ minScore: '', maxBudget: String(DEFAULT_PROJECT_MAX_BUDGET), maxOffers: '', category: '', shortlist: '' })
    updateUrl({ minScore: null, maxBudget: DEFAULT_PROJECT_MAX_BUDGET, maxOffers: null, category: null, shortlist: null, cursor: null }, false)
  }

  const changeSort = (sort: BuyerSearchProjectSort) => {
    cursorHistoryRef.current = []
    updateUrl({ sort: sort === DEFAULT_PROJECT_SORT ? null : sort, cursor: null }, false)
  }

  const goToNextProjectPage = () => {
    if (!nextProjectCursor) return
    cursorHistoryRef.current = [...cursorHistoryRef.current, projectCursor ?? '']
    updateUrl({ cursor: nextProjectCursor, project: null }, false)
  }

  const goToPreviousProjectPage = () => {
    const previousCursor = cursorHistoryRef.current.pop()
    if (previousCursor === undefined) return
    updateUrl({ cursor: previousCursor || null, project: null }, false)
  }

  const selectTaxonomyCategories = useCallback((selections: BuyerTaxonomySelection[]) => {
    setSelectedTaxonomySelections(selections)
    if (!selections.length) {
      setCategoryScope({})
      setStatusMessage('Выбор рубрик очищен.')
      return
    }

    const scopes = selections.map((selection) => ({
      ...selection.categoryScope,
      kworks_count: selection.kworksCount ?? null,
    }))
    const primaryScope = scopes[0]
    const totalKworks = selections.every((selection) => typeof selection.kworksCount === 'number')
      ? selections.reduce((total, selection) => total + (selection.kworksCount ?? 0), 0)
      : null
    setCategoryScope({
      ...primaryScope,
      category_ids: selections.map((selection) => selection.category.category_id),
      category_names: selections.map((selection) => selection.category.name),
      category_scopes: scopes,
      selected_kworks_total: totalKworks,
    })
    setMode((current) => (current === 'hybrid' ? 'hybrid' : 'category'))
    if (selections.some((selection) => selection.kworksCount === undefined)) {
      setStatusMessage(selections.length === 1
        ? `Выбрана рубрика: ${selections[0].category.name}`
        : `Выбрано рубрик: ${selections.length}`)
    }
  }, [])

  const createRun = async () => {
    const queryInputs = manualQueries
      .split(/\r?\n/)
      .map((text) => text.trim())
      .filter(Boolean)
      .map((text) => ({ text, rationale: 'Операторский запрос' }))
    const requestedWorkers = Math.max(1, Math.min(30, workers))
    const maxBudget = parseFiniteNumber(maxProjectBudget, 0)
    if (mode === 'manual' && queryInputs.length < requestedWorkers) {
      setError(`Для ${requestedWorkers} воркеров добавьте минимум ${requestedWorkers} уникальных запросов`)
      return
    }
    if ((mode === 'category' || mode === 'hybrid') && !selectedTaxonomySelections.length) {
      setError('Сначала выберите хотя бы одну рубрику из каталога.')
      return
    }
    const payload: CreateBuyerSearchRunPayload = {
      name: name.trim() || `Поиск заказов ${new Date().toLocaleString('ru-RU')}`,
      mode,
      brief: mode === 'brief' || mode === 'hybrid' ? brief.trim() || undefined : undefined,
      category_scope: mode === 'category' || mode === 'hybrid' ? categoryScope : undefined,
      requested_workers: requestedWorkers,
      target_unique_projects: Math.max(1, targetProjects),
      filters: maxBudget === undefined ? undefined : { max_budget: maxBudget, price_to: maxBudget },
      queries: queryInputs,
    }
    setCreating(true)
    try {
      const created = await buyerSearchApi.createRun(payload)
      setRuns((current) => [created, ...current.filter((item) => item.run_id !== created.run_id)])
      updateUrl({ run: created.run_id, project: null, cursor: null }, false)
      setName('')
      setBrief('')
      setManualQueries('')
      setMaxProjectBudget(String(DEFAULT_PROJECT_MAX_BUDGET))
      setCategoryScope({})
      setSelectedTaxonomySelections([])
      setTaxonomySelectionKey((current) => current + 1)
      setError(null)
      setStatusMessage(`Запуск «${displayRunName(created.name)}» создан`)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось создать запуск')
    } finally {
      setCreating(false)
    }
  }

  const patchState = async (state: 'running' | 'paused' | 'stopped') => {
    if (!selectedRun) return
    setAction(state)
    try {
      const updated = await buyerSearchApi.patchRun(selectedRun.run_id, { state })
      setRuns((current) => current.map((item) => (item.run_id === updated.run_id ? updated : item)))
      setError(null)
      setStatusMessage(`Запуск «${displayRunName(updated.name)}»: ${RUN_STATE_LABELS[updated.state]}`)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось изменить состояние запуска')
    } finally {
      setAction(null)
    }
  }

  const restartRun = async () => {
    if (!selectedRun) return
    setAction('restart')
    try {
      const updated = await buyerSearchApi.restartRun(selectedRun.run_id)
      setRuns((current) => current.map((item) => (item.run_id === updated.run_id ? updated : item)))
      setError(null)
      setStatusMessage(`Запуск «${displayRunName(updated.name)}» подготовлен к повторному запуску`)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось перезапустить запуск')
    } finally {
      setAction(null)
    }
  }

  const deleteRun = async () => {
    if (!selectedRun) return
    const deletableStates: BuyerSearchRun['state'][] = ['draft', 'ready', 'paused', 'completed', 'stopped', 'blocked', 'failed']
    if (!deletableStates.includes(selectedRun.state)) {
      setError('Сначала остановите запуск, затем его можно удалить.')
      return
    }
    if (!window.confirm(`Удалить запуск «${displayRunName(selectedRun.name)}» вместе с его результатами?`)) return
    const runId = selectedRun.run_id
    const nextRun = runs.find((run) => run.run_id !== runId) ?? null
    setAction('delete')
    try {
      await buyerSearchApi.deleteRun(runId)
      setRuns((current) => current.filter((run) => run.run_id !== runId))
      setQueries([])
      setProjects([])
      setFleet(null)
      setFacets(null)
      setSelectedProjectIds([])
      setSelectedProjectDetail(null)
      setError(null)
      setStatusMessage(`Запуск «${displayRunName(selectedRun.name)}» удалён`)
      updateUrl({ run: nextRun?.run_id ?? null, project: null, cursor: null }, false)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось удалить запуск')
    } finally {
      setAction(null)
    }
  }

  const createExport = async () => {
    if (!selectedRun) return
    setAction('export')
    try {
      const created = await buyerSearchApi.createExport(selectedRun.run_id, {
        format: 'zip',
        filters: exportFilters,
        selected_project_ids: selectedProjectIds,
        include_attachments: includeAttachmentsInExport,
      })
      const result = await buyerSearchApi.getExport(selectedRun.run_id, created.export_id).catch(() => created)
      setExportRecord(result)
      setError(null)
      setStatusMessage(`Экспорт ${result.export_id}: ${result.state}`)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось создать экспорт')
    } finally {
      setAction(null)
    }
  }

  const generateQueries = async () => {
    if (!selectedRun) return
    setAction('generate')
    try {
      const result = await buyerSearchApi.generateQueries(selectedRun.run_id, {
        mode: selectedRun.mode,
        brief: selectedRun.brief ?? undefined,
        category_scope: selectedRun.category_scope,
        filters: selectedRun.filters,
        requested_workers: selectedRun.requested_workers,
        query_batch_size: selectedRun.query_batch_size,
        minimum_count: Math.max(1, selectedRun.requested_workers * selectedRun.query_batch_size),
        auto_approve: false,
      })
      setLastQueryGeneration(result)
      setError(null)
      setStatusMessage(`Добавлено запросов: ${result.items.length}; совпадений: ${result.collision_count}`)
      await Promise.all([
        loadRunData(selectedRun.run_id, undefined, true),
        loadQueryPlanEvidence(selectedRun.run_id),
      ])
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось сгенерировать запросы')
    } finally {
      setAction(null)
    }
  }

  const toggleCollisionQuerySelection = (queryId: string) => {
    setSelectedCollisionQueryIds((current) => current.includes(queryId)
      ? current.filter((value) => value !== queryId)
      : [...current, queryId])
  }

  const regenerateCollisions = async () => {
    if (!selectedRun || !selectedCollisionQueryIds.length) return
    setAction('regenerate-collisions')
    try {
      const result = await buyerSearchApi.regenerateQueryCollisions(selectedRun.run_id, {
        collision_query_ids: selectedCollisionQueryIds,
        mode: selectedRun.mode,
        brief: selectedRun.brief ?? undefined,
        category_scope: selectedRun.category_scope,
        filters: selectedRun.filters,
        requested_workers: selectedRun.requested_workers,
        query_batch_size: selectedRun.query_batch_size,
        minimum_count: Math.max(1, selectedRun.requested_workers * selectedRun.query_batch_size),
        auto_approve: false,
      })
      setLastQueryGeneration(result.generation)
      setQueryCollisionReport(result.collision_report)
      setSelectedCollisionQueryIds([])
      setError(null)
      setStatusMessage(`Пересоздано запросов: ${result.generation.items.length}; затронуто совпадений: ${result.collision_query_ids.length}`)
      await Promise.all([
        loadRunData(selectedRun.run_id, undefined, true),
        loadQueryPlanEvidence(selectedRun.run_id),
      ])
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось пересоздать пересекающиеся запросы')
    } finally {
      setAction(null)
    }
  }

  const toggleProjectSelection = (projectId: string) => {
    setSelectedProjectIds((current) => current.includes(projectId)
      ? current.filter((value) => value !== projectId)
      : [...current, projectId])
  }

  const toggleVisibleProjectSelection = () => {
    const visibleIds = projects.map((project) => project.project_id)
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((projectId) => selectedProjectIdSet.has(projectId))
    setSelectedProjectIds((current) => {
      const currentSet = new Set(current)
      if (allVisibleSelected) {
        visibleIds.forEach((projectId) => currentSet.delete(projectId))
      } else {
        visibleIds.forEach((projectId) => currentSet.add(projectId))
      }
      return [...currentSet]
    })
  }

  const applyBulkShortlist = async (bulkAction: 'shortlist' | 'unshortlist') => {
    if (!selectedRun || !selectedProjectIds.length) return
    setAction(bulkAction)
    try {
      const tags = shortlistTags
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean)
      const result = await buyerSearchApi.batchProjectAction(selectedRun.run_id, {
        action: bulkAction,
        project_ids: selectedProjectIds,
        tags: bulkAction === 'shortlist' ? tags : undefined,
        note: bulkAction === 'shortlist' && shortlistNote.trim() ? shortlistNote.trim() : undefined,
        selected_by: 'operator',
      })
      setError(null)
      setStatusMessage(`Обработано проектов: ${result.applied_count}`)
      setSelectedProjectIds([])
      if (bulkAction === 'shortlist') {
        setShortlistTags('')
        setShortlistNote('')
      }
      await loadRunData(selectedRun.run_id, undefined, true)
      setProjectRefreshVersion((current) => current + 1)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось обновить избранное')
    } finally {
      setAction(null)
    }
  }

  const rescoreProject = async () => {
    if (!selectedRun || !inspectorProject) return
    setProjectActionId('rescore')
    try {
      const result = await buyerSearchApi.rescoreProject(selectedRun.run_id, inspectorProject.project_id)
      const score = typeof result.total_score === 'number' ? result.total_score : null
      setError(null)
      setStatusMessage(score === null ? 'Оценка пересчитана' : `Оценка пересчитана: ${formatNumber(score)}`)
      setProjectRefreshVersion((current) => current + 1)
      await loadRunData(selectedRun.run_id, undefined, true)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось пересчитать оценку')
    } finally {
      setProjectActionId(null)
    }
  }

  const finalScoreProject = async () => {
    if (!selectedRun || !inspectorProject) return
    setProjectActionId('final-score')
    try {
      const result = await buyerSearchApi.finalScoreProject(selectedRun.run_id, inspectorProject.project_id, {
        profile_id: finalScoreProfileId.trim() || undefined,
        profile_version: finalScoreProfileVersion.trim() || '1',
      })
      const score = typeof result.total_score === 'number' ? result.total_score : null
      setError(null)
      setStatusMessage(score === null ? 'Итоговая оценка ИИ сохранена' : `Итоговая оценка ИИ: ${formatNumber(score)}`)
      setProjectRefreshVersion((current) => current + 1)
      await loadRunData(selectedRun.run_id, undefined, true)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось рассчитать итоговую оценку ИИ')
    } finally {
      setProjectActionId(null)
    }
  }

  const enrichProject = async () => {
    if (!selectedRun || !inspectorProject) return
    if (!accountRegistrationId.trim()) {
      setError('Укажите ID регистрации аккаунта для обогащения данных')
      return
    }
    setProjectActionId('enrich')
    try {
      const result = await buyerSearchApi.enrichProject(
        selectedRun.run_id,
        inspectorProject.project_id,
        accountRegistrationId.trim(),
      )
      setLastEnrichment(result)
      setError(null)
      setStatusMessage(`Обогащение: обработано ${result.parsed_count}, загружено ${formatBytes(result.bytes_downloaded)}`)
      setProjectRefreshVersion((current) => current + 1)
      await loadRunData(selectedRun.run_id, undefined, true)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось обогатить вложения')
    } finally {
      setProjectActionId(null)
    }
  }

  const startQueryEdit = (query: BuyerSearchQuery) => {
    setEditingQueryId(query.query_id)
    setEditingQueryText(query.text)
  }

  const saveQuery = async (query: BuyerSearchQuery) => {
    if (!selectedRun || !editingQueryText.trim()) return
    setQueryActionId(query.query_id)
    try {
      const result = await buyerSearchApi.patchQuery(selectedRun.run_id, query.query_id, {
        text: editingQueryText.trim(),
      } satisfies BuyerSearchQueryPatch)
      const updated = result.query
      setQueries((current) => current.map((item) => (item.query_id === updated.query_id ? updated : item)))
      setEditingQueryId(null)
      setEditingQueryText('')
      setError(null)
      setStatusMessage(`Запрос обновлен: ${updated.text}`)
      await loadQueryPlanEvidence(selectedRun.run_id)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось обновить запрос')
    } finally {
      setQueryActionId(null)
    }
  }

  const toggleQuery = async (query: BuyerSearchQuery) => {
    if (!selectedRun) return
    setQueryActionId(query.query_id)
    try {
      const result = await buyerSearchApi.patchQuery(selectedRun.run_id, query.query_id, {
        enabled: !query.enabled,
        approved: !query.enabled ? true : query.approved,
      } satisfies BuyerSearchQueryPatch)
      const updated = result.query
      setQueries((current) => current.map((item) => (item.query_id === updated.query_id ? updated : item)))
      setError(null)
      setStatusMessage(updated.enabled ? `Запрос включен: ${updated.text}` : `Запрос отключен: ${updated.text}`)
      await loadQueryPlanEvidence(selectedRun.run_id)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось изменить запрос')
    } finally {
      setQueryActionId(null)
    }
  }

  const distributeQueries = async () => {
    if (!selectedRun) return
    setAction('distribute')
    try {
      const result = await buyerSearchApi.distributeQueries(selectedRun.run_id)
      setError(null)
      setStatusMessage(`В очередь добавлено запросов: ${result.queued_count}`)
      await Promise.all([
        loadRunData(selectedRun.run_id, undefined, true),
        loadQueryPlanEvidence(selectedRun.run_id),
      ])
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось распределить запросы')
    } finally {
      setAction(null)
    }
  }

  const canStart = selectedRun?.state === 'ready' || selectedRun?.state === 'paused' || selectedRun?.state === 'blocked'
  const canPause = selectedRun?.state === 'running'
  const canStop = Boolean(selectedRun && !['stopped', 'completed', 'failed'].includes(selectedRun.state))
  const canRestart = Boolean(selectedRun && ['stopped', 'completed', 'failed', 'blocked'].includes(selectedRun.state))
  const canDelete = Boolean(selectedRun && ['draft', 'ready', 'paused', 'completed', 'stopped', 'blocked', 'failed'].includes(selectedRun.state))
  const activeQueries = queries.filter((query) => query.enabled)
  const activeBuyerProjectTotal = useMemo(() => {
    const categoryFeeds = queries.filter(
      (query) => query.enabled && query.origin === 'category_browse' && typeof query.predicted_total === 'number',
    )
    if (!categoryFeeds.length) return null
    return categoryFeeds.reduce((total, query) => total + (query.predicted_total ?? 0), 0)
  }, [queries])
  const visibleProjectCount = projects.length
  const currentPage = cursorHistoryRef.current.length + 1
  const runCounters = selectedRun?.counters as Record<string, unknown> | undefined
  const awaitingNewProjects = selectedRun?.state === 'running'
    && runCounters?.auto_project_refresh_mode === true
  const refreshTime = formatRefreshTime(runCounters?.auto_project_refresh_next_at)
  const discoveredProjectCount = typeof runCounters?.unique_projects === 'number'
    ? runCounters.unique_projects
    : projects.length
  const receivedProjectCardCount = typeof runCounters?.projects_seen === 'number'
    ? runCounters.projects_seen
    : discoveredProjectCount
  const repeatedProjectCardCount = Math.max(0, receivedProjectCardCount - discoveredProjectCount)
  const projectMetricHint = [
    activeBuyerProjectTotal === null ? null : `Активных заказов в текущей выдаче: ${formatNumber(activeBuyerProjectTotal)}`,
    receivedProjectCardCount > discoveredProjectCount
      ? `карточек получено: ${formatNumber(receivedProjectCardCount)}; повторов: ${formatNumber(repeatedProjectCardCount)}`
      : null,
  ].filter(Boolean).join('; ') || undefined
  const capacityReason = awaitingNewProjects
    ? refreshTime
      ? `Получено ${formatNumber(discoveredProjectCount)} из ${formatNumber(selectedRun?.target_unique_projects)}.${activeBuyerProjectTotal === null ? '' : ` Сейчас активно: ${formatNumber(activeBuyerProjectTotal)}.`} Следующая проверка в ${refreshTime}`
      : `Получено ${formatNumber(discoveredProjectCount)} из ${formatNumber(selectedRun?.target_unique_projects)}. Выполняется повторная проверка новых заказов`
    : fleetCapacityReason(selectedFleet)
  const capacityValue = selectedFleet
    ? `${selectedFleet.effective_workers} / ${selectedFleet.requested_workers}`
    : `— / ${selectedRun?.requested_workers ?? 0}`
  const executionWorkers = selectedFleet?.workers ?? []

  return (
    <main className="min-h-full bg-surface-950 text-zinc-100">
      <div className="sr-only" aria-live="polite">{statusMessage}</div>
      <header className="border-b border-surface-700 bg-surface-900 px-5 py-4 lg:px-7">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-white">Поиск заказов</h1>
            <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
              <ShieldCheck className="h-3.5 w-3.5 text-cyan-300" />
              <span>Только чтение каталога</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" className="btn btn-ghost h-9 w-9 justify-center px-0" title="Обновить" aria-label="Обновить" onClick={() => void loadRuns()}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button type="button" className="btn btn-primary h-9 gap-2" onClick={() => void createRun()} disabled={creating}>
              {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
              Новый запуск
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div role="alert" className="flex items-center justify-between gap-3 border-b border-rose-500/50 bg-rose-950/30 px-5 py-2 text-sm text-rose-100 lg:px-7">
          <span className="min-w-0 break-words">{error}</span>
          <button type="button" className="btn btn-ghost h-7 w-7 shrink-0 justify-center px-0" title="Закрыть" aria-label="Закрыть" onClick={() => setError(null)}>
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      <section className="border-b border-surface-700 px-5 py-4 lg:px-7">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_160px_160px_160px]">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-2" role="group" aria-label="Режим поиска">
              {(Object.keys(MODE_LABELS) as BuyerRunMode[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  aria-pressed={mode === item}
                  className={`h-8 border px-3 text-xs ${mode === item ? 'border-lime-500 bg-lime-400/10 text-lime-100' : 'border-surface-600 text-zinc-400 hover:text-zinc-100'}`}
                  onClick={() => setMode(item)}
                >
                  {MODE_LABELS[item]}
                </button>
              ))}
            </div>
            <input className="input mb-2 h-9" value={name} onChange={(event) => setName(event.target.value)} placeholder="Название поиска (необязательно)" aria-label="Название поиска (необязательно)" />
            {mode !== 'category' && (
              <textarea
                className="input min-h-20 resize-y py-2"
                value={mode === 'manual' ? manualQueries : brief}
                onChange={(event) => (mode === 'manual' ? setManualQueries(event.target.value) : setBrief(event.target.value))}
                placeholder={mode === 'manual' ? 'По одному запросу на строку' : 'Опишите, какие заказы искать'}
                aria-label={mode === 'manual' ? 'Запросы, по одному на строку' : 'Описание поиска'}
              />
            )}
          </div>
          <label className="block text-xs text-zinc-500">
            Воркеры
            <input
              className="input mt-2 h-9 font-mono"
              type="number"
              min={1}
              max={30}
              value={workers}
              onChange={(event) => setWorkers(Number(event.target.value) || 1)}
            />
          </label>
          <label className="block text-xs text-zinc-500">
            Цель проектов
            <input
              className="input mt-2 h-9 font-mono"
              type="number"
              min={1}
              value={targetProjects}
              onChange={(event) => setTargetProjects(Number(event.target.value) || 1)}
            />
          </label>
          <label className="block text-xs text-zinc-500">
            Макс. бюджет, ₽
            <input
              className="input mt-2 h-9 font-mono"
              type="number"
              min={0}
              value={maxProjectBudget}
              onChange={(event) => setMaxProjectBudget(event.target.value)}
            />
          </label>
        </div>
        {(mode === 'category' || mode === 'hybrid') && (
          <div className="mt-4 grid gap-3 border-t border-surface-800 pt-4 xl:grid-cols-[minmax(0,1fr)_260px]">
            <BuyerTaxonomyPlanner
              key={taxonomySelectionKey}
              disabled={creating}
              onSelect={selectTaxonomyCategories}
            />
            <div className="border border-surface-700 px-3 py-3 text-xs text-zinc-500">
              <div className="text-[10px] uppercase tracking-wide text-zinc-500">Выбранные рубрики</div>
              {selectedTaxonomySelections.length ? (
                <>
                  <div className="mt-2 space-y-2">
                    {selectedTaxonomySelections.map((selection) => (
                      <div key={selection.category.category_id} className="border-b border-surface-800 pb-2 last:border-b-0 last:pb-0">
                        <div className="break-words text-sm text-zinc-100">{selection.category.name}</div>
                        <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 font-mono text-[11px] text-cyan-200">
                          <span>№ {selection.category.category_id}</span>
                          <span>
                            {selection.kworksCount === undefined
                              ? 'Услуг в каталоге: считаем…'
                              : selection.kworksCount === null
                                ? 'Услуг в каталоге: нет данных'
                                : `Услуг в каталоге: ${formatNumber(selection.kworksCount)}`}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="mt-3 border-t border-surface-800 pt-2 text-xs text-cyan-100">
                    {selectedTaxonomyKworksTotal === null
                      ? selectedTaxonomySelections.some((selection) => selection.kworksCount === undefined)
                        ? 'Считаем количество услуг в каталоге…'
                        : 'Количество услуг в каталоге недоступно'
                      : `Услуг в каталоге: ${formatNumber(selectedTaxonomyKworksTotal)}`}
                  </div>
                </>
              ) : <div className="mt-2 leading-5">Отметьте одну или несколько рубрик в каталоге.</div>}
            </div>
          </div>
        )}
      </section>

      <div
        ref={paneLayoutContainerRef}
        className="grid min-h-0 xl:grid-cols-[var(--buyer-runs-pane-width)_8px_minmax(0,1fr)_8px_var(--buyer-inspector-pane-width)]"
        style={paneLayoutStyle}
      >
        <aside className="border-b border-surface-700 bg-surface-925 xl:min-h-[calc(100vh-19rem)] xl:border-b-0" aria-label="Список запусков">
          <div className="flex items-center justify-between border-b border-surface-700 px-4 py-3">
            <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">Запуски</span>
            <span className="font-mono text-xs text-zinc-500">{runs.length}</span>
          </div>
          <div className="max-h-72 overflow-y-auto xl:max-h-[calc(100vh-22rem)]">
            {loading && !runs.length && <div className="px-4 py-6 text-sm text-zinc-500">Загрузка...</div>}
            {!loading && !runs.length && <div className="px-4 py-6 text-sm text-zinc-500">Нет сохраненных запусков</div>}
            {runs.map((run) => (
              <button
                key={run.run_id}
                type="button"
                aria-pressed={selectedRunId === run.run_id}
                className={`block w-full border-b border-surface-800 px-4 py-3 text-left ${selectedRunId === run.run_id ? 'bg-lime-400/10' : 'hover:bg-surface-800'}`}
                onClick={() => selectRun(run.run_id)}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium text-zinc-100">{displayRunName(run.name)}</span>
                  <RunState state={run.state} />
                </div>
                <div className="mt-2 flex items-center justify-between font-mono text-[11px] text-zinc-500">
                  <span>{run.requested_workers} ворк.</span>
                  <span>{formatNumber(run.counters.unique_projects ?? 0)} проектов</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Изменить ширину списка запусков"
          tabIndex={0}
          title="Перетащите, чтобы изменить ширину списка запусков"
          className={`group relative hidden cursor-col-resize touch-none outline-none transition-colors xl:flex xl:w-2 xl:self-stretch xl:items-center xl:justify-center ${paneResizeSession?.side === 'runs' ? 'bg-cyan-400/10' : 'hover:bg-cyan-400/10 focus-visible:bg-cyan-400/10'}`}
          onPointerDown={(event) => beginPaneResize('runs', event)}
          onKeyDown={(event) => handlePaneResizeKeyDown('runs', event)}
        >
          <span className="h-full w-px bg-surface-700 transition-colors group-hover:bg-cyan-300 group-focus-visible:bg-cyan-300" aria-hidden="true" />
          <GripVertical className="absolute h-4 w-4 text-surface-600 transition-colors group-hover:text-cyan-300 group-focus-visible:text-cyan-300" aria-hidden="true" />
        </div>

        <section className="min-w-0 border-b border-surface-700 xl:border-b-0" aria-busy={runDataLoading}>
          {selectedRun ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-surface-700 px-4 py-3">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-white">{displayRunName(selectedRun.name)}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <RunState state={selectedRun.state} />
                      {selectedRunCategoryNames.length ? (
                        <span className="max-w-full truncate text-[11px] text-brand-200" title={selectedRunCategoryNames.join(', ')}>
                          Рубрика: {selectedRunCategoryNames.join(', ')}
                        </span>
                      ) : null}
                      <span className="font-mono text-[11px] text-zinc-500">v{selectedRun.config_version}</span>
                      <span className={`inline-flex items-center gap-1 text-[11px] ${streamState === 'connected' ? 'text-cyan-200' : 'text-zinc-500'}`}>
                        {streamState === 'connected' ? <ShieldCheck className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                        WS: {streamState === 'connected' ? 'подключено' : streamState === 'connecting' ? 'подключение' : 'нет связи'}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 max-lg:sticky max-lg:bottom-2 max-lg:z-10 max-lg:bg-surface-900/95 max-lg:p-1">
                  <button type="button" disabled={!canStart || action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title={selectedRun.state === 'blocked' ? 'Повторить запуск' : 'Запустить'} aria-label={selectedRun.state === 'blocked' ? 'Повторить запуск' : 'Запустить'} onClick={() => void patchState('running')}>
                    <Play className="h-4 w-4 text-lime-300" />
                  </button>
                  <button type="button" disabled={!canPause || action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title="Пауза" aria-label="Пауза" onClick={() => void patchState('paused')}>
                    <Pause className="h-4 w-4 text-amber-300" />
                  </button>
                  <button type="button" disabled={!canStop || action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title="Остановить" aria-label="Остановить" onClick={() => void patchState('stopped')}>
                    <Square className="h-3.5 w-3.5 text-rose-300" />
                  </button>
                  <button type="button" disabled={!canRestart || action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title="Подготовить повторный запуск" aria-label="Подготовить повторный запуск" onClick={() => void restartRun()}>
                    <RefreshCw className={`h-3.5 w-3.5 text-cyan-300 ${action === 'restart' ? 'animate-spin' : ''}`} />
                  </button>
                  <button type="button" disabled={action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title="Экспорт ZIP" aria-label="Экспорт ZIP" onClick={() => void createExport()}>
                    {action === 'export' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4 text-cyan-300" />}
                  </button>
                  <button type="button" disabled={!canDelete || action !== null} className="btn btn-ghost h-8 w-8 justify-center px-0" title="Удалить завершённый запуск" aria-label="Удалить завершённый запуск" onClick={() => void deleteRun()}>
                    {action === 'delete' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4 text-rose-300" />}
                  </button>
                </div>
              </div>

              {selectedRun.state === 'blocked' && selectedRun.last_error ? (
                <div className="border-b border-rose-500/30 bg-rose-950/20 px-4 py-2 text-xs text-rose-200">
                  Причина блокировки: {localizedFleetReason(selectedRun.last_error)}
                </div>
              ) : null}

              <div className="grid grid-cols-2 border-b border-surface-700 sm:grid-cols-4">
                <div className="border-l border-surface-700 px-3 first:border-l-0">
                  <div className="text-[10px] uppercase tracking-wide text-zinc-500">Мощность</div>
                  <div className="mt-1 font-mono text-sm tabular-nums text-cyan-200">{capacityValue}</div>
                  <div className="mt-1 min-h-3 truncate text-[10px] text-amber-200" title={capacityReason ?? undefined}>{capacityReason ?? ' '}</div>
                </div>
                <Metric label="Запросы" value={formatNumber(activeQueries.length)} />
                <Metric
                  label="Проекты"
                  value={formatNumber(selectedRun.counters.unique_projects ?? projects.length)}
                  tone="text-lime-200"
                  hint={projectMetricHint}
                />
                <Metric label="Ошибки" value={formatNumber(selectedRun.counters.errors ?? 0)} tone="text-rose-200" />
              </div>

              <section className="border-b border-surface-700" aria-labelledby="buyer-execution-grid-heading">
                <div className="flex items-center justify-between gap-3 px-4 py-2">
                  <div id="buyer-execution-grid-heading" className="text-xs font-medium uppercase tracking-wide text-zinc-500">Воркеры</div>
                  <span className="font-mono text-xs text-zinc-500">{executionWorkers.length} ворк.</span>
                </div>
                <div className="max-h-40 overflow-auto border-t border-surface-800">
                  {executionWorkers.length ? (
                    <table className="w-full min-w-[820px] text-left text-xs">
                      <caption className="sr-only">Активные воркеры и их текущий контекст выполнения</caption>
                      <thead className="sticky top-0 bg-surface-900 text-zinc-500">
                        <tr>
                          <th className="px-4 py-2 font-medium">Воркер</th>
                          <th className="px-3 py-2 font-medium">Аккаунт</th>
                          <th className="px-3 py-2 font-medium">Маршрут</th>
                          <th className="px-3 py-2 font-medium">Внешний IP</th>
                          <th className="px-3 py-2 font-medium">Состояние</th>
                          <th className="px-3 py-2 font-medium">Текущая задача</th>
                        </tr>
                      </thead>
                      <tbody>
                        {executionWorkers.map((worker) => {
                          const currentTask = fleetWorkerTask(worker)
                          return (
                            <tr key={worker.worker_id} className="border-t border-surface-800">
                              <td className="max-w-[170px] truncate px-4 py-2 font-mono text-zinc-300" title={worker.worker_id}>{worker.worker_id}</td>
                              <td className="max-w-[150px] truncate px-3 py-2 font-mono text-zinc-300" title={worker.account_registration_id}>{worker.account_registration_id}</td>
                              <td className="max-w-[150px] truncate px-3 py-2 font-mono text-zinc-300" title={worker.transport_id}>{worker.transport_id}</td>
                              <td className="px-3 py-2 font-mono text-cyan-200">{worker.egress_ip || '—'}</td>
                              <td className="px-3 py-2 text-zinc-300">{displayState(worker.state)}</td>
                              <td className="max-w-[200px] truncate px-3 py-2 font-mono text-zinc-400" title={currentTask}>{currentTask}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  ) : (
                    <div className="px-4 py-4 text-xs text-zinc-500">
                      {capacityReason || 'Для этого запуска нет активных привязок воркеров.'}
                    </div>
                  )}
                </div>
              </section>

              <div className="border-b border-surface-700">
                <div className="flex items-center justify-between px-4 py-2">
                  <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500"><Search className="h-3.5 w-3.5" /> План запросов</div>
                  <div className="flex items-center gap-2">
                    {lastQueryGeneration ? <span className="font-mono text-[10px] text-amber-200" title={`Генератор: ${lastQueryGeneration.generator}; резервный режим: ${lastQueryGeneration.used_fallback ? 'да' : 'нет'}`}>совпадений {lastQueryGeneration.collision_count}</span> : null}
                    <span className="font-mono text-xs text-zinc-500">{activeQueries.length}</span>
                    <button
                      type="button"
                      className="btn btn-ghost h-7 w-7 justify-center px-0"
                      title="Сгенерировать запросы для проверки"
                      aria-label="Сгенерировать запросы для проверки"
                      disabled={action !== null}
                      onClick={() => void generateQueries()}
                    >
                      {action === 'generate' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-cyan-300" />}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost h-7 w-7 justify-center px-0"
                      title="Распределить подтвержденные запросы"
                      aria-label="Распределить подтвержденные запросы"
                      disabled={action !== null}
                      onClick={() => void distributeQueries()}
                    >
                      {action === 'distribute' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5 text-cyan-300" />}
                    </button>
                  </div>
                </div>
                <div className="max-h-44 overflow-auto border-t border-surface-800">
                  <table className="w-full min-w-[720px] text-left text-xs">
                    <thead className="sticky top-0 bg-surface-900 text-zinc-500"><tr><th className="px-4 py-2 font-medium">Запрос</th><th className="px-3 py-2 font-medium">Состояние</th><th className="px-3 py-2 text-right font-medium">Задачи</th><th className="px-3 py-2 text-right font-medium">Уникальные</th><th className="w-20 px-2 py-2"><span className="sr-only">Действия</span></th></tr></thead>
                    <tbody>
                      {!runDataLoading && !activeQueries.length && <tr><td colSpan={5} className="px-4 py-5 text-center text-zinc-500">Запросы еще не сгенерированы</td></tr>}
                      {activeQueries.map((query) => {
                        const isEditing = editingQueryId === query.query_id
                        const isBusy = queryActionId === query.query_id
                        const categoryName = categoryNameForQuery(selectedRun, query.category_id)
                        return (
                          <tr key={query.query_id} className="border-t border-surface-800">
                            <td className="max-w-[380px] px-4 py-2 text-zinc-200">
                              {isEditing ? (
                                <div className="flex items-center gap-1">
                                  <input
                                    className="input h-7 min-w-0 flex-1 py-1 text-xs"
                                    value={editingQueryText}
                                    aria-label={`Редактировать запрос ${query.text}`}
                                    onChange={(event) => setEditingQueryText(event.target.value)}
                                    onKeyDown={(event) => {
                                      if (event.key === 'Enter') void saveQuery(query)
                                      if (event.key === 'Escape') setEditingQueryId(null)
                                    }}
                                  />
                                  <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Сохранить запрос" aria-label="Сохранить запрос" disabled={isBusy || !editingQueryText.trim()} onClick={() => void saveQuery(query)}><Check className="h-3.5 w-3.5 text-lime-300" /></button>
                                  <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Отменить редактирование" aria-label="Отменить редактирование" disabled={isBusy} onClick={() => setEditingQueryId(null)}><X className="h-3.5 w-3.5" /></button>
                                </div>
                              ) : (
                                <div className="min-w-0">
                                  <span className="block truncate" title={query.text}>{query.text}</span>
                                  {categoryName ? <span className="block truncate text-[10px] text-brand-300" title={categoryName}>Рубрика: {categoryName}</span> : null}
                                </div>
                              )}
                            </td>
                            <td className="px-3 py-2 text-zinc-400">{query.enabled ? displayState(query.state) : 'отключен'}</td>
                            <td
                              className="px-3 py-2 text-right font-mono text-zinc-400"
                              title="Выполнения запросов к источникам / уникальные задачи"
                            >
                              {query.pages_completed > query.pages_scheduled
                                ? `${query.pages_completed} выполн. / ${query.pages_scheduled} задач`
                                : `${query.pages_completed} из ${query.pages_scheduled}`}
                            </td>
                            <td className="px-3 py-2 text-right font-mono text-lime-200">{query.unique_projects}</td>
                            <td className="px-2 py-2">
                              <div className="flex items-center justify-end gap-1">
                                <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Редактировать запрос" aria-label={`Редактировать ${query.text}`} disabled={isBusy || isEditing} onClick={() => startQueryEdit(query)}><Pencil className="h-3.5 w-3.5" /></button>
                                <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title={query.enabled ? 'Отключить запрос' : 'Включить запрос'} aria-label={query.enabled ? `Отключить ${query.text}` : `Включить ${query.text}`} disabled={isBusy} onClick={() => void toggleQuery(query)}>{isBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Power className={`h-3.5 w-3.5 ${query.enabled ? 'text-cyan-300' : 'text-zinc-500'}`} />}</button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                {queryCollisionReport ? (
                  <details className="border-t border-surface-800 px-4 py-2">
                    <summary className="flex cursor-pointer items-center justify-between gap-3 text-xs text-amber-200"><span>Отчет о пересечениях запросов</span><span className="font-mono text-[10px]">{queryCollisionReport.items.length} событий</span></summary>
                    <div className="mt-3 space-y-2">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-zinc-500"><span>Выбрано ID запросов для пересоздания: {selectedCollisionQueryIds.length}</span><button type="button" className="btn btn-ghost h-7 text-xs" disabled={!selectedCollisionQueryIds.length || action !== null} onClick={() => void regenerateCollisions()}>{action === 'regenerate-collisions' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-amber-200" />} Пересоздать</button></div>
                      {!queryCollisionReport.items.length ? <div className="py-2 text-xs text-zinc-500">Пересечений в плане запросов нет.</div> : null}
                      <div className="max-h-48 overflow-auto border-t border-surface-800">
                        <table className="w-full min-w-[650px] text-left text-[11px]">
                          <thead className="sticky top-0 bg-surface-900 text-zinc-500"><tr><th className="px-2 py-2">Тип</th><th className="px-2 py-2">ID запросов</th><th className="px-2 py-2">Пара</th><th className="px-2 py-2 text-right">Оценка</th></tr></thead>
                          <tbody>{queryCollisionReport.items.map((collision) => <tr key={collision.seq} className="border-t border-surface-800"><td className="px-2 py-2 text-amber-200">{collision.kind || 'пересечение'}</td><td className="px-2 py-2"><div className="flex flex-wrap gap-1">{[collision.first_query_id, collision.second_query_id].filter((queryId): queryId is string => Boolean(queryId)).map((queryId) => <button key={queryId} type="button" className={`border px-1 py-0.5 font-mono text-[10px] ${selectedCollisionQueryIds.includes(queryId) ? 'border-amber-400/70 text-amber-100' : 'border-surface-700 text-zinc-500'}`} aria-pressed={selectedCollisionQueryIds.includes(queryId)} onClick={() => toggleCollisionQuerySelection(queryId)}>{queryId}</button>)}</div></td><td className="max-w-[280px] truncate px-2 py-2 text-zinc-400" title={`${collision.first_text || ''} / ${collision.second_text || ''}`}>{collision.first_text || '—'} / {collision.second_text || '—'}</td><td className="px-2 py-2 text-right font-mono text-zinc-400">{formatNumber(collision.score)}</td></tr>)}</tbody>
                        </table>
                      </div>
                    </div>
                  </details>
                ) : null}
                {queryBundles ? (
                  <details className="border-t border-surface-800 px-4 py-2">
                    <summary className="flex cursor-pointer items-center justify-between gap-3 text-xs text-cyan-200"><span>Пакеты запросов воркеров</span><span className="font-mono text-[10px]">{queryBundles.eligible_query_count} готовы / {queryBundles.requested_workers} ворк.</span></summary>
                    <div className="mt-3 max-h-52 overflow-auto border-t border-surface-800">
                      <table className="w-full min-w-[620px] text-left text-[11px]"><thead className="sticky top-0 bg-surface-900 text-zinc-500"><tr><th className="px-2 py-2">Воркер</th><th className="px-2 py-2">Запросы</th><th className="px-2 py-2 text-right">Задачи</th><th className="px-2 py-2">Источники</th></tr></thead><tbody>{queryBundles.bundles.map((bundle) => <tr key={bundle.worker_index} className="border-t border-surface-800"><td className="px-2 py-2 font-mono text-cyan-200">#{bundle.worker_index}</td><td className="max-w-[320px] px-2 py-2"><div className="flex flex-wrap gap-1">{bundle.queries.length ? bundle.queries.map((query) => <span key={query.query_id} className="max-w-48 truncate border border-surface-700 px-1 py-0.5 text-zinc-400" title={query.text || query.query_id}>{query.text || query.query_id}</span>) : <span className="text-zinc-600">—</span>}</div></td><td className="px-2 py-2 text-right font-mono text-zinc-400">{bundle.task_count}</td><td className="px-2 py-2 font-mono text-zinc-500">{bundle.sources.join(', ') || '—'}</td></tr>)}</tbody></table>
                    </div>
                    {queryBundles.excluded_query_ids.length ? <div className="mt-2 text-[10px] text-zinc-600">Исключены отключенные или неподтвержденные: {queryBundles.excluded_query_ids.length}</div> : null}
                  </details>
                ) : null}
              </div>

              <div className="min-w-0">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-surface-700 px-4 py-2">
                  <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-zinc-500"><FileSearch className="h-3.5 w-3.5" /> Проекты</div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs text-zinc-500">{visibleProjectCount}</span>
                    <label className="flex items-center gap-1 text-[10px] text-zinc-500" title="Включить список вложений в экспорт">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5"
                        checked={includeAttachmentsInExport}
                        onChange={(event) => setIncludeAttachmentsInExport(event.target.checked)}
                      />
                      файлы
                    </label>
                    <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title={selectedProjectCount ? `Экспортировать ${selectedProjectCount} выбранных проектов` : 'Экспортировать текущую выборку'} aria-label="Экспортировать проекты" disabled={action !== null} onClick={() => void createExport()}>
                      {action === 'export' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5 text-cyan-300" />}
                    </button>
                  </div>
                </div>
                <form
                  className="grid gap-2 border-b border-surface-800 px-4 py-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-2 2xl:grid-cols-3"
                  onSubmit={(event) => {
                    event.preventDefault()
                    applyProjectFilters()
                  }}
                >
                  <label className="text-[11px] text-zinc-500">Сортировка
                    <select className="input mt-1 h-8 py-1 text-xs" value={projectSort} onChange={(event) => changeSort(event.target.value as BuyerSearchProjectSort)}>
                      {PROJECT_SORT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label className="text-[11px] text-zinc-500">Минимальная оценка
                    <input className="input mt-1 h-8 py-1 font-mono text-xs" inputMode="decimal" value={filterDraft.minScore} onChange={(event) => setFilterDraft((current) => ({ ...current, minScore: event.target.value }))} />
                  </label>
                  <label className="text-[11px] text-zinc-500">Максимальный бюджет, ₽
                    <input className="input mt-1 h-8 py-1 font-mono text-xs" inputMode="numeric" value={filterDraft.maxBudget} onChange={(event) => setFilterDraft((current) => ({ ...current, maxBudget: event.target.value }))} />
                  </label>
                  <label className="text-[11px] text-zinc-500">Максимум откликов
                    <input className="input mt-1 h-8 py-1 font-mono text-xs" inputMode="numeric" value={filterDraft.maxOffers} onChange={(event) => setFilterDraft((current) => ({ ...current, maxOffers: event.target.value }))} />
                  </label>
                  <label className="text-[11px] text-zinc-500">Рубрика среди найденных проектов
                    <select className="input mt-1 h-8 py-1 text-xs" value={filterDraft.category} onChange={(event) => setFilterDraft((current) => ({ ...current, category: event.target.value }))}>
                      <option value="">Все найденные рубрики</option>
                      {projectCategoryOptions.map((category) => {
                        const categoryName = categoryNameForQuery(selectedRun, category.categoryId)
                        return <option key={category.categoryId} value={category.categoryId}>{categoryName ?? `Рубрика №${category.categoryId}`} ({category.count})</option>
                      })}
                    </select>
                  </label>
                  <label className="text-[11px] text-zinc-500">Избранное
                    <select className="input mt-1 h-8 py-1 text-xs" value={filterDraft.shortlist} onChange={(event) => setFilterDraft((current) => ({ ...current, shortlist: event.target.value }))}>
                      <option value="">Все</option>
                      <option value="shortlisted">В избранном</option>
                      <option value="removed">Убрано из избранного</option>
                    </select>
                  </label>
                  <div className="flex items-end gap-2">
                    <button type="submit" className="btn btn-ghost h-8 flex-1 justify-center text-xs"><SlidersHorizontal className="h-3.5 w-3.5" /> Применить</button>
                    <button type="button" className="btn btn-ghost h-8 w-8 justify-center px-0" title="Сбросить фильтры" aria-label="Сбросить фильтры" onClick={clearProjectFilters}><X className="h-3.5 w-3.5" /></button>
                  </div>
                </form>
                {facets ? (
                  <div className="flex flex-wrap items-center gap-2 border-b border-surface-800 px-4 py-2 text-[10px] text-zinc-500">
                    <span className="font-mono">Всего в запуске: {facets.total}</span>
                    {(facets.shortlist_states ?? []).filter((item) => String(item.value)).slice(0, 4).map((item) => {
                      const state = String(item.value)
                      return <button key={state} type="button" className="border border-surface-700 px-1.5 py-0.5 hover:border-cyan-500/60 hover:text-cyan-200" onClick={() => updateUrl({ shortlist: state, cursor: null }, false)}>{displayState(state)} {item.count}</button>
                    })}
                    {(facets.categories ?? []).slice(0, 3).map((item) => <span key={`category-${String(item.value)}`} className="font-mono text-zinc-600">кат. {String(item.value)}: {item.count}</span>)}
                  </div>
                ) : null}
                <div className="grid gap-2 border-b border-surface-800 px-4 py-3 sm:grid-cols-[auto_minmax(8rem,1fr)_minmax(10rem,1.3fr)_auto_auto] sm:items-end">
                  <span className="pb-2 font-mono text-[11px] text-zinc-500">Выбрано: {selectedProjectCount}</span>
                  <label className="text-[11px] text-zinc-500">Метки<input className="input mt-1 h-8 w-full py-1 text-xs" value={shortlistTags} onChange={(event) => setShortlistTags(event.target.value)} placeholder="приоритет, теплый" disabled={!selectedProjectCount || action !== null} /></label>
                  <label className="text-[11px] text-zinc-500">Заметка<input className="input mt-1 h-8 w-full py-1 text-xs" value={shortlistNote} onChange={(event) => setShortlistNote(event.target.value)} placeholder="Заметка оператора" disabled={!selectedProjectCount || action !== null} /></label>
                  <button type="button" className="btn btn-ghost h-8 justify-center text-xs" disabled={!selectedProjectCount || action !== null} onClick={() => void applyBulkShortlist('shortlist')}>
                    {action === 'shortlist' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5 text-lime-300" />} В избранное
                  </button>
                  <button type="button" className="btn btn-ghost h-8 justify-center text-xs" disabled={!selectedProjectCount || action !== null} onClick={() => void applyBulkShortlist('unshortlist')}>
                    {action === 'unshortlist' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5 text-zinc-400" />} Убрать
                  </button>
                </div>
                {exportRecord ? (
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-surface-800 px-4 py-2 text-[11px] text-zinc-500">
                    <span className="font-mono">Экспорт: {displayState(exportRecord.state)}{exportRecord.progress?.rows !== undefined ? ` / строк: ${String(exportRecord.progress.rows)}` : ''}</span>
                    {exportRecord.state === 'completed' ? <a className="text-cyan-200 hover:text-cyan-100" href={buyerSearchApi.exportDownloadUrl(selectedRun.run_id, exportRecord.export_id)}>Скачать {exportRecord.filename || 'экспорт'}</a> : null}
                  </div>
                ) : null}
                <div className="flex items-center justify-between gap-3 border-b border-surface-800 px-4 py-2">
                  <span className="text-[11px] text-zinc-500">Страница {currentPage}, до {PROJECT_PAGE_SIZE} строк</span>
                  <div className="flex items-center gap-1" aria-label="Пагинация проектов">
                    <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Предыдущая страница" aria-label="Предыдущая страница" disabled={!cursorHistoryRef.current.length || runDataLoading} onClick={goToPreviousProjectPage}><ChevronLeft className="h-4 w-4" /></button>
                    <button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Следующая страница" aria-label="Следующая страница" disabled={!nextProjectCursor || runDataLoading} onClick={goToNextProjectPage}><ChevronRight className="h-4 w-4" /></button>
                  </div>
                </div>
                <div className="max-h-[min(480px,calc(100vh-32rem))] overflow-auto">
                  <table className="w-full min-w-[1240px] text-left text-xs">
                    <thead className="sticky top-0 bg-surface-900 text-zinc-500"><tr><th className="w-9 px-2 py-2"><input type="checkbox" className="h-3.5 w-3.5" aria-label="Выбрать все проекты на странице" checked={projects.length > 0 && projects.every((project) => selectedProjectIdSet.has(project.project_id))} onChange={toggleVisibleProjectSelection} /></th><th className="px-4 py-2 font-medium">Проект</th><th className="px-3 py-2 text-right font-medium">Бюджет</th><th className="px-3 py-2 text-right font-medium">Отклики</th><th className="px-3 py-2 text-right font-medium">Просмотры</th><th className="px-3 py-2 text-right font-medium">Возраст</th><th className="px-3 py-2 text-right font-medium">Категория</th><th className="px-3 py-2 text-right font-medium">Оценка</th><th className="px-3 py-2 text-right font-medium">Совпадения</th><th className="px-3 py-2 text-right font-medium">Избранное</th><th className="px-3 py-2 text-right font-medium">Файлы</th><th className="w-10 px-2 py-2"><span className="sr-only">Открыть</span></th></tr></thead>
                    <tbody>
                      {runDataLoading && !projects.length && <tr><td colSpan={12} className="px-4 py-6 text-center text-zinc-500">Загрузка проектов...</td></tr>}
                      {!runDataLoading && !projects.length && <tr><td colSpan={12} className="px-4 py-6 text-center text-zinc-500">По текущим фильтрам проектов нет</td></tr>}
                      {projects.map((project) => (
                        <tr key={project.project_id} className={`border-t border-surface-800 ${selectedProjectId === project.project_id ? 'bg-cyan-400/10' : 'hover:bg-surface-800'}`}>
                          <td className="px-2 py-2"><input type="checkbox" className="h-3.5 w-3.5" aria-label={`Выбрать ${project.title}`} checked={selectedProjectIdSet.has(project.project_id)} onChange={() => toggleProjectSelection(project.project_id)} /></td>
                          <td className="max-w-[330px] px-4 py-2"><div className="truncate text-zinc-100">{project.title}</div><div className="mt-0.5 truncate text-zinc-500">{project.description_excerpt}</div></td>
                          <td className="px-3 py-2 text-right font-mono text-zinc-300">{formatNumber(project.budget_min)}-{formatNumber(project.budget_max)}</td><td className="px-3 py-2 text-right font-mono text-zinc-400">{formatNumber(project.offers)}</td><td className="px-3 py-2 text-right font-mono text-zinc-400">{formatNumber(project.views)}</td><td className="px-3 py-2 text-right font-mono text-zinc-400">{formatAge(project.age_seconds)}</td><td className="px-3 py-2 text-right font-mono text-zinc-400">{project.category_id ?? '—'}</td><td className="px-3 py-2 text-right font-mono text-lime-200">{formatNumber(project.final_score ?? project.preliminary_score)}</td><td className="px-3 py-2 text-right font-mono text-cyan-200">{formatNumber(project.matched_query_count)}</td><td className="px-3 py-2 text-right text-zinc-400">{displayState(project.shortlist_state)}</td><td className="px-3 py-2 text-right font-mono text-cyan-200">{formatNumber(project.attachment_count)}</td><td className="px-2 py-2"><button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Открыть проект" aria-label={`Открыть ${project.title}`} onClick={() => selectProject(project.project_id)}><Eye className="h-3.5 w-3.5" /></button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          ) : <div className="flex min-h-80 items-center justify-center px-4 text-center text-sm text-zinc-500">{loading ? 'Загрузка запусков...' : 'Выберите запуск'}</div>}
        </section>

        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Изменить ширину карточки проекта"
          tabIndex={0}
          title="Перетащите, чтобы изменить ширину карточки проекта"
          className={`group relative hidden cursor-col-resize touch-none outline-none transition-colors xl:flex xl:w-2 xl:self-stretch xl:items-center xl:justify-center ${paneResizeSession?.side === 'inspector' ? 'bg-cyan-400/10' : 'hover:bg-cyan-400/10 focus-visible:bg-cyan-400/10'}`}
          onPointerDown={(event) => beginPaneResize('inspector', event)}
          onKeyDown={(event) => handlePaneResizeKeyDown('inspector', event)}
        >
          <span className="h-full w-px bg-surface-700 transition-colors group-hover:bg-cyan-300 group-focus-visible:bg-cyan-300" aria-hidden="true" />
          <GripVertical className="absolute h-4 w-4 text-surface-600 transition-colors group-hover:text-cyan-300 group-focus-visible:text-cyan-300" aria-hidden="true" />
        </div>

        <aside className="min-w-0 bg-surface-925" aria-busy={projectDetailLoading || projectActionId !== null}>
          <div className="flex items-center justify-between border-b border-surface-700 px-4 py-3"><span className="text-xs font-medium uppercase tracking-wide text-zinc-500">Карточка проекта</span><Eye className="h-4 w-4 text-cyan-300" /></div>
          {inspectorProject ? (
            <div className="space-y-4 p-4">
              <div className="flex items-start justify-between gap-3"><div className="min-w-0"><h2 className="break-words text-sm font-semibold text-white">{inspectorProject.title}</h2><div className="mt-1 font-mono text-[11px] text-zinc-500">{inspectorProject.remote_project_id}</div></div><div className="flex shrink-0 items-center gap-1"><button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Обновить проект" aria-label="Обновить проект" onClick={() => setProjectRefreshVersion((current) => current + 1)}><RefreshCw className={`h-3.5 w-3.5 ${projectDetailLoading ? 'animate-spin' : ''}`} /></button><button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Пересчитать оценку" aria-label="Пересчитать оценку" disabled={projectActionId !== null} onClick={() => void rescoreProject()}>{projectActionId === 'rescore' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-lime-300" />}</button><button type="button" className="btn btn-ghost h-7 w-7 justify-center px-0" title="Обогатить вложения" aria-label="Обогатить вложения" disabled={projectActionId !== null || !accountRegistrationId.trim()} onClick={() => void enrichProject()}>{projectActionId === 'enrich' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileSearch className="h-3.5 w-3.5 text-cyan-300" />}</button></div></div>
              <p className="whitespace-pre-wrap break-words text-sm leading-6 text-zinc-300">{selectedProjectDetail?.description || inspectorProject.description_excerpt || 'Описание недоступно'}</p>
              <dl className="grid grid-cols-2 gap-x-3 gap-y-3 border-y border-surface-700 py-3 text-xs"><div><dt className="text-zinc-500">Бюджет</dt><dd className="mt-1 font-mono text-zinc-100">{formatNumber(inspectorProject.budget_min)}-{formatNumber(inspectorProject.budget_max)}</dd></div><div><dt className="text-zinc-500">Оценка</dt><dd className="mt-1 font-mono text-lime-200">{formatNumber(inspectorProject.final_score ?? inspectorProject.preliminary_score)}</dd></div><div><dt className="text-zinc-500">Вложения</dt><dd className="mt-1 font-mono text-cyan-200">{inspectorProject.attachment_count}</dd></div><div><dt className="text-zinc-500">Запросы</dt><dd className="mt-1 font-mono text-zinc-100">{inspectorProject.matched_query_count}</dd></div></dl>
              {selectedProjectDetail?.scores?.length ? (
                <details className="border-b border-surface-800 pb-3">
                  <summary className="cursor-pointer text-xs font-medium text-lime-200">Расшифровка оценки</summary>
                  <div className="mt-2 space-y-2 text-[11px] text-zinc-400">
                    {selectedProjectDetail.scores.map((score, index) => (
                      <div key={score.score_id || `${score.score_kind || 'score'}-${index}`} className="border-t border-surface-800 pt-2">
                        <div className="flex items-center justify-between gap-2"><span>{score.score_kind || 'оценка'} {score.score_profile_version || ''}</span><span className="font-mono text-lime-200">{formatNumber(score.total_score)}</span></div>
                        {score.rationale ? <p className="mt-1 break-words leading-4">{score.rationale}</p> : null}
                        {score.breakdown ? <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-1 font-mono text-[10px] text-zinc-500">{Object.entries(score.breakdown).map(([key, value]) => <span key={key} className="truncate" title={`${key}: ${formatDetailValue(value)}`}>{key}: {formatDetailValue(value)}</span>)}</div> : null}
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
              <details className="border-b border-surface-800 pb-3">
                <summary className="cursor-pointer text-xs font-medium text-cyan-200">Итоговая оценка ИИ</summary>
                <div className="mt-3 grid grid-cols-[minmax(0,1fr)_72px_auto] items-end gap-2">
                  <label className="text-[10px] text-zinc-500">Профиль<input className="input mt-1 h-8 w-full py-1 text-xs" value={finalScoreProfileId} onChange={(event) => setFinalScoreProfileId(event.target.value)} placeholder="по умолчанию" disabled={projectActionId !== null} /></label>
                  <label className="text-[10px] text-zinc-500">Версия<input className="input mt-1 h-8 w-full py-1 font-mono text-xs" value={finalScoreProfileVersion} onChange={(event) => setFinalScoreProfileVersion(event.target.value)} disabled={projectActionId !== null} /></label>
                  <button type="button" className="btn btn-ghost h-8 w-8 justify-center px-0" title="Сохранить итоговую оценку ИИ" aria-label="Сохранить итоговую оценку ИИ" disabled={projectActionId !== null} onClick={() => void finalScoreProject()}>{projectActionId === 'final-score' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5 text-cyan-300" />}</button>
                </div>
              </details>
              {selectedProjectDetail?.matches?.length ? (
                <div className="border-b border-surface-800 pb-3">
                  <div className="mb-2 text-[10px] uppercase tracking-wide text-zinc-500">Совпавшие запросы</div>
                  <div className="flex flex-wrap gap-1">{selectedProjectDetail.matches.map((match, index) => <span key={match.query_id || `${match.query_text || 'query'}-${index}`} className="max-w-full truncate border border-surface-700 px-1.5 py-0.5 text-[10px] text-cyan-200" title={match.query_text || match.normalized_query_text}>{match.query_text || match.normalized_query_text || match.query_id}</span>)}</div>
                </div>
              ) : null}
              {selectedProjectDetail?.attachments?.length ? (
                <div>
                  <div className="mb-2 text-[10px] uppercase tracking-wide text-zinc-500">Вложения</div>
                  <ul className="space-y-1 text-xs text-zinc-300">
                    {selectedProjectDetail.attachments.map((attachment) => (
                      <li key={attachment.attachment_id} className="border-t border-surface-800 py-2">
                        <div className="flex items-center justify-between gap-2"><span className="min-w-0 truncate">{attachment.filename || attachment.attachment_id}</span><span className="shrink-0 font-mono text-[10px] text-cyan-200">{displayState(attachment.state)}</span></div>
                        <div className="mt-1 flex flex-wrap gap-x-2 gap-y-1 font-mono text-[10px] text-zinc-500"><span>{attachment.detected_type || 'тип не определен'}</span><span>{formatBytes(attachment.size_bytes)}</span>{attachment.sha256 ? <span className="max-w-36 truncate" title={attachment.sha256}>{attachment.sha256}</span> : null}{selectedRun && attachment.object_ref ? <a className="text-cyan-200 hover:text-cyan-100" href={buyerSearchApi.attachmentPreviewUrl(selectedRun.run_id, inspectorProject.project_id, attachment.attachment_id)} target="_blank" rel="noreferrer">Просмотр</a> : null}</div>
                        {attachment.derivatives?.length ? <div className="mt-1 flex flex-wrap gap-1">{attachment.derivatives.map((derivative, index) => <span key={derivative.derivative_id || `${derivative.kind || 'производный файл'}-${index}`} className="border border-surface-700 px-1 py-0.5 font-mono text-[10px] text-zinc-400">{derivative.kind || derivative.parser_name || 'производный файл'}: {displayState(derivative.state)}</span>)}</div> : null}
                        {attachment.error ? <div className="mt-1 break-words text-[10px] text-rose-300">{attachment.error}</div> : null}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {lastEnrichment ? <div className="border-y border-surface-800 py-2 text-[11px] text-zinc-500"><span className="font-mono text-cyan-200">Обогащение</span>: обработано {lastEnrichment.parsed_count}, {formatBytes(lastEnrichment.bytes_downloaded)}{lastEnrichment.context?.context_hash ? <span className="ml-2 font-mono text-zinc-600" title={lastEnrichment.context.context_hash}>{lastEnrichment.context.context_hash}</span> : null}</div> : null}
              {selectedProjectDetail?.shortlist ? <div className="border-b border-surface-800 pb-3 text-[11px] text-zinc-500"><div className="mb-1 text-[10px] uppercase tracking-wide">Избранное</div><div>{displayState(selectedProjectDetail.shortlist.state)}{selectedProjectDetail.shortlist.tags?.length ? <span className="ml-2 text-cyan-200">{selectedProjectDetail.shortlist.tags.join(', ')}</span> : null}</div>{selectedProjectDetail.shortlist.notes?.length ? <div className="mt-1 space-y-1">{selectedProjectDetail.shortlist.notes.map((note, index) => <div key={`${note.created_at || 'note'}-${index}`} className="break-words">{note.body}</div>)}</div> : null}</div> : null}
              {selectedProjectDetail?.canonical_url ? <a className="block truncate text-xs text-cyan-200 hover:text-cyan-100" href={selectedProjectDetail.canonical_url} target="_blank" rel="noreferrer">Открыть на Kwork</a> : null}
              <label className="block border-t border-surface-700 pt-3 text-[11px] text-zinc-500">
                ID регистрации аккаунта
                <input
                  className="input mt-1 h-8 w-full py-1 font-mono text-xs"
                  value={accountRegistrationId}
                  onChange={(event) => setAccountRegistrationId(event.target.value)}
                  placeholder="ID аккаунта"
                />
              </label>
              {selectedRun ? (
                <details className="border-t border-surface-700 pt-3">
                  <summary className="cursor-pointer text-xs font-medium text-cyan-200">Рабочее место отклика</summary>
                  <div className="mt-3">
                    <BuyerOutreachWorkspace
                      runId={selectedRun.run_id}
                      projectId={inspectorProject.project_id}
                      senderAccountRegistrationId={accountRegistrationId}
                      operatorId="operator"
                    />
                  </div>
                </details>
              ) : null}
              <details className="border-t border-surface-700 pt-3">
                <summary className="cursor-pointer text-xs font-medium text-cyan-200">Диалоги аккаунта</summary>
                <div className="mt-3">
                  <BuyerConversationWorkspace
                    accountRegistrationId={accountRegistrationId}
                    projectId={inspectorProject.project_id}
                  />
                </div>
              </details>
            </div>
          ) : <div className="p-4 text-sm text-zinc-500">{projectDetailLoading ? 'Загрузка полной карточки...' : 'Проект не выбран'}</div>}
        </aside>
      </div>
    </main>
  )
}
