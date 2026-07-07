import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  BarChart3,
  ChevronLeft,
  ExternalLink,
  Image as ImageIcon,
  Info,
  Loader2,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Store,
  Tags,
  Wand2,
} from 'lucide-react'
import { useApi } from '../hooks/useApi'
import {
  api,
  type KworkAttributeFlat,
  type KworkCategoryNode,
  type KworkClassifier,
  type KworkCompetitor,
  type KworkDemandSnapshot,
  type KworkDraftResult,
  type KworkFormControl,
  type KworkFormManifest,
  type KworkPriceRules,
} from '../lib/api'
import { cn } from '../lib/utils'
import { openKworkVerificationWindow } from '../lib/kworkVerification'

interface FlatCategory {
  id: number
  name: string
  label: string
  depth: number
  rootId: number
}

interface FieldChangeNotice {
  control: string
  controlLabel: string
  before: string[]
  after: string[]
  changed: boolean
  reason: string
  source: string
  confidence?: number
  context: string[]
}

function flattenCategories(
  nodes: KworkCategoryNode[],
  depth = 0,
  parent = '',
  rootId?: number,
): FlatCategory[] {
  const result: FlatCategory[] = []
  for (const node of nodes) {
    if (!node?.id) continue
    const name = node.name || `#${node.id}`
    const label = parent ? `${parent} / ${name}` : name
    const currentRootId = rootId ?? node.id
    result.push({ id: node.id, name, label, depth, rootId: currentRootId })
    result.push(...flattenCategories(node.children || [], depth + 1, label, currentRootId))
  }
  return result
}

function preferredChild(root?: KworkCategoryNode) {
  if (!root) return undefined
  const children = root.children || []
  return children.find((item) => /скрипты|бот/i.test(item.name || '')) || children[0] || root
}

function formatNumber(value?: number | string | null) {
  const n = Number(value || 0)
  if (!Number.isFinite(n)) return '0'
  return new Intl.NumberFormat('ru-RU').format(n)
}

function formatPrice(value?: number | string | null) {
  const n = Number(value || 0)
  return Number.isFinite(n) && n > 0 ? `${formatNumber(n)} ₽` : '-'
}

function valuesToNumbers(value: unknown): number[] {
  if (Array.isArray(value)) return value.map(Number).filter((item) => Number.isFinite(item))
  if (value && typeof value === 'object') {
    return Object.values(value).map(Number).filter((item) => Number.isFinite(item))
  }
  return []
}

function getPriceSteps(prices: unknown): number[] {
  if (!prices || typeof prices !== 'object') return []
  const data = prices as { typicalPriceGradation?: unknown; priceGradation?: unknown }
  const typical = valuesToNumbers(data.typicalPriceGradation)
  if (typical.length) return typical

  const grad = data.priceGradation
  if (Array.isArray(grad)) return valuesToNumbers(grad)
  if (grad && typeof grad === 'object') {
    const grouped = grad as Record<string, unknown>
    return valuesToNumbers(grouped.priceStandard).length
      ? valuesToNumbers(grouped.priceStandard)
      : valuesToNumbers(Object.values(grouped)[0])
  }
  return []
}

function demandView(demand: KworkDemandSnapshot | undefined, enabled: boolean) {
  if (!enabled) return { value: 'выкл', sub: 'тумблер выше ищет свежие заказы на бирже Kwork' }
  if (!demand) return { value: '-', sub: '' }
  if (demand.status === 'ok') {
    return { value: formatNumber(demand.wants_count), sub: `${demand.sample_count || 0} свежих примеров` }
  }
  if (demand.status === 'needs_cookies') {
    return { value: 'куки', sub: 'для заказов нужен вход через Session Hub' }
  }
  if (demand.status === 'error') return { value: 'ошибка', sub: demand.detail || 'Kwork не отдал заказы' }
  return { value: demand.label || demand.status || '-', sub: demand.detail || '' }
}

function filterScopeSummary(scope: Record<string, unknown> | undefined) {
  const selected = Array.isArray(scope?.selected) ? (scope.selected as Array<Record<string, unknown>>) : []
  const labels = selected.flatMap((item) =>
    Array.isArray(item.labels) ? item.labels.map(String).filter(Boolean) : [],
  )
  const classifierIds = Array.isArray(scope?.effective_classifier_ids)
    ? (scope.effective_classifier_ids as unknown[]).map(String).filter(Boolean)
    : []
  const parts = []
  if (labels.length) parts.push(`фильтры: ${labels.join(', ')}`)
  if (classifierIds.length) parts.push(`classifierId: ${classifierIds.join(', ')}`)
  return parts.join('; ')
}

function demandExplanation(demand: KworkDemandSnapshot | undefined, enabled: boolean) {
  if (!enabled) {
    return 'Выключено: PSR не ходит на биржу заказов Kwork. Это быстрее и не влияет на черновик или публикацию.'
  }
  if (!demand) {
    return 'Данных пока нет: метрики ещё грузятся для выбранной рубрики или среза.'
  }
  if (demand.status === 'ok') {
    const scope = filterScopeSummary(demand.scope)
    return `PSR запросил биржу заказов Kwork по выбранной рубрике/срезу и нашёл ${formatNumber(
      demand.wants_count,
    )} заказов. Размер выборки: ${demand.sample_count || 0}.${scope ? ` Применено: ${scope}.` : ''} Это только оценка спроса, она не меняет поля кворка.`
  }
  if (demand.status === 'needs_cookies') {
    return 'Kwork не отдаёт заказы без рабочей сессии. Открой Session Hub или пройди ручную проверку Kwork, после этого спрос можно обновить.'
  }
  if (demand.status === 'error') {
    return `Kwork вернул ошибку при проверке спроса: ${demand.detail || 'без подробностей'}. Публикация от этого не блокируется.`
  }
  return demand.detail || 'Это вспомогательная проверка спроса по заказам. Она нужна для решения, есть ли живой спрос в выбранном срезе.'
}

function MetricTile({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="card scanline min-w-0">
      <div className="mono-label truncate">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      {sub && <div className="mt-1 truncate text-xs text-zinc-500">{sub}</div>}
    </div>
  )
}

function ClassifierButton({
  item,
  active,
  onClick,
}: {
  item: KworkClassifier
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors',
        active
          ? 'border-brand-500/50 bg-brand-600/15 text-white'
          : 'border-surface-600 bg-surface-900/40 text-zinc-400 hover:text-white',
      )}
    >
      <span className="truncate">{item.name}</span>
      <span className="shrink-0 text-zinc-500">{formatNumber(item.kworks_count)}</span>
    </button>
  )
}

function setSelectionValue(
  selection: Record<string, unknown>,
  control: KworkFormControl,
  optionId: number,
  checked: boolean,
) {
  if (control.multiple) {
    const current = Array.isArray(selection[control.name]) ? (selection[control.name] as unknown[]) : []
    const values = new Set(current.map((item) => Number(item)).filter((item) => Number.isFinite(item)))
    if (checked) values.add(optionId)
    else values.delete(optionId)
    return { ...selection, [control.name]: Array.from(values) }
  }
  if (!checked) {
    const next = { ...selection }
    delete next[control.name]
    return next
  }
  return { ...selection, [control.name]: optionId }
}

function isOptionSelected(selection: Record<string, unknown>, control: KworkFormControl, optionId: number) {
  const value = selection[control.name]
  if (Array.isArray(value)) return value.map(Number).includes(optionId)
  return Number(value) === optionId
}

function selectedOptionIds(selection: Record<string, unknown>, control: KworkFormControl) {
  const value = selection[control.name]
  const values = Array.isArray(value) ? value : value === undefined || value === null || value === '' ? [] : [value]
  return values.map(Number).filter((item) => Number.isFinite(item))
}

function optionLabels(control: KworkFormControl, ids: number[]) {
  const labels = new Map((control.options || []).map((option) => [option.id, option.label || `#${option.id}`]))
  return ids.map((id) => labels.get(id) || `#${id}`)
}

function findControlForOption(controls: KworkFormControl[], optionId: number) {
  return controls.find((control) => (control.options || []).some((option) => Number(option.id) === Number(optionId)))
}

function hasSelectableOptions(control: KworkFormControl) {
  return !control.disabled && !!control.options?.some((option) => !option.disabled)
}

function useDebouncedValue<T>(value: T, delayMs: number, resetKey?: unknown) {
  const [debounced, setDebounced] = useState(value)
  const resetKeyRef = useRef(resetKey)
  const resetNow = resetKeyRef.current !== resetKey
  if (resetNow) resetKeyRef.current = resetKey
  useEffect(() => {
    if (resetNow) {
      setDebounced(value)
      return
    }
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs, resetKey, resetNow])
  return resetNow ? value : debounced
}

function manualVerificationFromPublishResult(result: Record<string, any> | null | undefined) {
  if (!result) return null
  const sources = [
    result,
    result.web_state,
    result.save_result,
    result.verify_result,
    result.upload,
  ].filter(Boolean)
  const hit = sources.find((item) => item?.code === 'manual_verification_required')
  if (!hit) return null
  return {
    url: hit.final_url || result.web_state?.final_url || result.save_result?.final_url || 'https://kwork.ru/',
    detail: hit.detail || result.detail || 'Kwork просит ручную проверку.',
  }
}

function coverContextNumber(context: Record<string, unknown> | undefined, key: string) {
  const value = Number(context?.[key] || 0)
  return Number.isFinite(value) ? value : 0
}

function sameSelection(left: number[], right: number[]) {
  if (left.length !== right.length) return false
  const rightSet = new Set(right)
  return left.every((item) => rightSet.has(item))
}

function controlLabel(control: KworkFormControl) {
  return control.question || control.label || control.custom_name || control.name
}

function CompetitorCard({ item }: { item: KworkCompetitor }) {
  return (
    <article className="overflow-hidden rounded-md border border-surface-700 bg-surface-900/40">
      <div className="aspect-[3/2] bg-surface-950">
        {item.image_url ? (
          <img src={item.image_url} alt="" className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="flex h-full items-center justify-center text-zinc-700">
            <ImageIcon className="h-8 w-8" />
          </div>
        )}
      </div>
      <div className="space-y-2 p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="line-clamp-2 text-sm font-medium text-zinc-100">{item.title || `#${item.id || '-'}`}</div>
            <div className="mt-1 truncate text-[11px] text-zinc-500">
              {item.worker || 'seller'} {item.seller_level ? `· ${item.seller_level}` : ''}
            </div>
          </div>
          {item.share_url && (
            <a
              href={item.share_url}
              target="_blank"
              rel="noreferrer"
              className="rounded-md border border-surface-600 p-1.5 text-zinc-500 hover:text-white"
              title="Открыть кворк"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
        </div>
        <div className="flex flex-wrap gap-2 text-[11px] text-zinc-500">
          <span className="text-brand-300">{formatPrice(item.price)}</span>
          {item.rating && <span>рейтинг {item.rating}</span>}
          {item.reviews && <span>{formatNumber(item.reviews)} отзывов</span>}
          {item.is_best && <span>топ</span>}
        </div>
        {item.service_size && <div className="text-xs text-zinc-300">{item.service_size}</div>}
        {item.description ? (
          <div className="max-h-28 overflow-hidden text-xs leading-relaxed text-zinc-400">{item.description}</div>
        ) : (
          <div className="text-xs text-zinc-600">
            {item.detail_status === 'error' ? 'Описание не удалось забрать' : 'Описание загружается из карточки'}
          </div>
        )}
      </div>
    </article>
  )
}

export default function KworkMarket() {
  const [selectedRootId, setSelectedRootId] = useState<number | null>(null)
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(null)
  const [selectedClassifierId, setSelectedClassifierId] = useState<number | undefined>()
  const [includeDemand, setIncludeDemand] = useState(false)
  const [showDemandHelp, setShowDemandHelp] = useState(false)
  const [serviceSummary, setServiceSummary] = useState('телеграм бота или автоматизацию')
  const [audience, setAudience] = useState('малый бизнес, эксперты и команды')
  const [price, setPrice] = useState(1000)
  const [workTime, setWorkTime] = useState(3)
  const [useLlm, setUseLlm] = useState(true)
  const [generateImage, setGenerateImage] = useState(true)
  const [draftLoading, setDraftLoading] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [draftResult, setDraftResult] = useState<KworkDraftResult | null>(null)
  const [publishResult, setPublishResult] = useState<Record<string, any> | null>(null)
  const [autoClassifierKey, setAutoClassifierKey] = useState('')
  const [formManifest, setFormManifest] = useState<KworkFormManifest | null>(null)
  const [attributeSelection, setAttributeSelection] = useState<Record<string, unknown>>({})
  const [manifestLoading, setManifestLoading] = useState(false)
  const [manifestError, setManifestError] = useState<string | null>(null)
  const [verificationOpening, setVerificationOpening] = useState(false)
  const [fieldNotice, setFieldNotice] = useState<FieldChangeNotice | null>(null)
  const [suggestingControl, setSuggestingControl] = useState<string | null>(null)
  const [classifierTrail, setClassifierTrail] = useState<KworkClassifier[]>([])
  const manifestTimerRef = useRef<number | undefined>()
  const manifestRequestRef = useRef(0)

  const {
    data: categoriesData,
    loading: categoriesLoading,
    error: categoriesError,
    refetch: refetchCategories,
  } = useApi(() => api.getKworkMarketCategories(), [], { categories: [] })

  const topCategories = categoriesData?.categories || []
  const flatCategories = useMemo(() => flattenCategories(topCategories), [topCategories])

  useEffect(() => {
    if (selectedRootId || topCategories.length === 0) return
    const root = topCategories.find((item) => /разработка/i.test(item.name || '')) || topCategories[0]
    setSelectedRootId(root.id)
    setSelectedCategoryId(preferredChild(root)?.id || root.id)
  }, [selectedRootId, topCategories])

  useEffect(() => {
    setSelectedClassifierId(undefined)
    setClassifierTrail([])
    setAttributeSelection({})
    setFormManifest(null)
    setFieldNotice(null)
  }, [selectedCategoryId])

  const selectedRoot = topCategories.find((item) => item.id === selectedRootId)
  const rootChildren = selectedRoot?.children || []
  const selectedCategory = flatCategories.find((item) => item.id === selectedCategoryId)

  const {
    data: attributesData,
    loading: attributesLoading,
    error: attributesError,
    refetch: refetchAttributes,
  } = useApi(
    () =>
      selectedCategoryId
        ? api.getKworkCategoryAttributes(selectedCategoryId)
        : Promise.resolve({ category_id: 0, attributes: [], flat: [] }),
    [selectedCategoryId],
  )

  const {
    data: pricesData,
    loading: pricesLoading,
    error: pricesError,
    refetch: refetchPrices,
  } = useApi<KworkPriceRules>(
    () => (selectedCategoryId ? api.getKworkCategoryPrices(selectedCategoryId) : Promise.resolve({})),
    [selectedCategoryId],
  )

  const manifestControls = useMemo(() => formManifest?.controls || [], [formManifest])
  const sliceControls = useMemo(
    () => manifestControls.filter((control) => hasSelectableOptions(control)).slice(0, 8),
    [manifestControls],
  )
  const debouncedAttributeSelection = useDebouncedValue(attributeSelection, 900, selectedCategoryId)
  const debouncedManifestControls = useDebouncedValue(manifestControls, 900, selectedCategoryId)
  const attributeSelectionSignature = useMemo(() => JSON.stringify(debouncedAttributeSelection), [debouncedAttributeSelection])
  const manifestControlsSignature = useMemo(
    () =>
      JSON.stringify(
        debouncedManifestControls.map((control) => ({
          name: control.name,
          question: control.question || control.label || '',
          options: (control.options || []).map((option) => ({
            id: option.id,
            label: option.label,
            has_child: option.has_child,
          })),
        })),
      ),
    [debouncedManifestControls],
  )

  const {
    data: metrics,
    loading: metricsLoading,
    error: metricsError,
    refetch: refetchMetrics,
  } = useApi(
    () =>
      selectedCategoryId
        ? api.getKworkMarketMetrics({
            category_id: selectedCategoryId,
            classifier_id: selectedClassifierId,
            include_demand: includeDemand,
            include_competitor_details: true,
            competitor_detail_limit: 2,
            attribute_selection: debouncedAttributeSelection,
            attribute_controls: debouncedManifestControls,
          })
        : Promise.resolve({
            page: 1,
            kworks_count: 0,
            classifiers: [],
            competitors: [],
            raw_keys: [],
            filter_scope: { params: {}, selected: [], count: 0, effective_classifier_ids: [] },
            filter_requests: [],
            demand: { status: 'skipped' },
          }),
    [selectedCategoryId, selectedClassifierId, includeDemand, attributeSelectionSignature, manifestControlsSignature],
  )

  const requiredAttributes: KworkAttributeFlat[] = useMemo(
    () => (attributesData?.flat || []).filter((item) => item.required),
    [attributesData],
  )

  const priceGradation = useMemo(() => getPriceSteps(pricesData?.prices), [pricesData])
  const metricsMatchesSelection =
    !!metrics &&
    metrics.category_id === selectedCategoryId &&
    (metrics.classifier_id || undefined) === (selectedClassifierId || undefined)
  const visibleMetrics = metricsMatchesSelection ? metrics : undefined
  const selectedClassifier = visibleMetrics?.classifiers?.find((item) => item.id === selectedClassifierId)
  const demand = demandView(visibleMetrics?.demand, includeDemand)
  const competitors = visibleMetrics?.competitors || []
  const loading = categoriesLoading || attributesLoading || pricesLoading || metricsLoading
  const anyError = categoriesError || attributesError || pricesError || metricsError
  const coverVisionImagesSeen = Number(draftResult?.image?.competitor_images_seen || 0)
  const hasCoverVisionAnalysis =
    draftResult?.image?.visual_analysis_status === 'analyzed' && coverVisionImagesSeen > 0
  const coverPromptContext = draftResult?.image?.cover_prompt_context
  const coverPromptRoute =
    typeof coverPromptContext?.prompt_writer_route === 'string' ? coverPromptContext.prompt_writer_route : ''
  const publishManualVerification = manualVerificationFromPublishResult(publishResult)

  const loadFormManifest = async (
    selection: Record<string, unknown> = attributeSelection,
    classifierId: number | undefined = selectedClassifierId,
  ) => {
    if (!selectedCategoryId) return
    const requestId = manifestRequestRef.current + 1
    manifestRequestRef.current = requestId
    setManifestLoading(true)
    setManifestError(null)
    try {
      const manifest = await api.getKworkFormManifest(selectedCategoryId, {
        classifier_id: classifierId,
        selection,
        lang: 'ru',
      })
      if (manifestRequestRef.current !== requestId) return
      if (manifest.code === 'manual_verification_required') {
        setManifestError(manifest.detail || 'Kwork просит ручную проверку. Открой проверку Kwork в уведомлении сверху.')
      }
      setFormManifest(manifest)
      setAttributeSelection({ ...(manifest.selected || {}), ...(selection || {}) })
    } catch (e) {
      if (manifestRequestRef.current !== requestId) return
      setManifestError(e instanceof Error ? e.message : String(e))
    } finally {
      if (manifestRequestRef.current === requestId) setManifestLoading(false)
    }
  }

  const scheduleFormManifest = (
    selection: Record<string, unknown>,
    classifierId: number | undefined = selectedClassifierId,
    delayMs = 800,
  ) => {
    if (manifestTimerRef.current) window.clearTimeout(manifestTimerRef.current)
    setManifestLoading(true)
    manifestTimerRef.current = window.setTimeout(() => {
      manifestTimerRef.current = undefined
      loadFormManifest(selection, classifierId)
    }, delayMs)
  }

  const openManifestVerification = async () => {
    setVerificationOpening(true)
    try {
      await openKworkVerificationWindow(formManifest?.final_url || undefined)
    } finally {
      setVerificationOpening(false)
    }
  }

  const openPublishVerification = async () => {
    setVerificationOpening(true)
    try {
      await openKworkVerificationWindow(publishManualVerification?.url || undefined)
    } finally {
      setVerificationOpening(false)
    }
  }

  useEffect(() => {
    if (!selectedCategoryId) return
    loadFormManifest({}, undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCategoryId])

  useEffect(() => {
    return () => {
      if (manifestTimerRef.current) window.clearTimeout(manifestTimerRef.current)
    }
  }, [])

  useEffect(() => {
    if (!visibleMetrics?.classifiers?.length || selectedClassifierId || !selectedCategory) return
    if (!/скрипты|бот/i.test(selectedCategory.name)) return
    if (!/бот|telegram|телеграм|mini app|mini apps/i.test(serviceSummary)) return
    const key = `${selectedCategoryId}:${serviceSummary}`
    if (autoClassifierKey === key) return
    const preferred =
      visibleMetrics.classifiers.find((item) => /чат-бот/i.test(item.name || '')) ||
      visibleMetrics.classifiers.find((item) => /telegram/i.test(item.name || '')) ||
      visibleMetrics.classifiers.find((item) => /ии-бот/i.test(item.name || ''))
    if (preferred?.id) {
      setAutoClassifierKey(key)
      setSelectedClassifierId(preferred.id)
      setClassifierTrail([preferred])
      const optionControl = findControlForOption(manifestControls, preferred.id)
      const nextSelection = optionControl
        ? setSelectionValue(attributeSelection, optionControl, preferred.id, true)
        : attributeSelection
      setAttributeSelection(nextSelection)
      loadFormManifest(nextSelection, preferred.id)
    }
  }, [autoClassifierKey, selectedCategory, selectedCategoryId, selectedClassifierId, serviceSummary, visibleMetrics])

  const chooseCategory = (categoryId: number) => {
    if (manifestTimerRef.current) window.clearTimeout(manifestTimerRef.current)
    setSelectedCategoryId(categoryId)
    setSelectedClassifierId(undefined)
    setClassifierTrail([])
    setAttributeSelection({})
    setFormManifest(null)
    setFieldNotice(null)
  }

  const chooseRoot = (root: KworkCategoryNode) => {
    setSelectedRootId(root.id)
    chooseCategory(preferredChild(root)?.id || root.id)
  }

  const refreshAll = () => {
    refetchCategories()
    refetchAttributes()
    refetchPrices()
    refetchMetrics()
  }

  const resetClassifier = () => {
    const nextSelection: Record<string, unknown> = {}
    setClassifierTrail([])
    setSelectedClassifierId(undefined)
    setAttributeSelection(nextSelection)
    loadFormManifest(nextSelection, undefined)
  }

  const chooseClassifier = (item: KworkClassifier) => {
    if (!item.id) {
      resetClassifier()
      return
    }
    const optionControl = findControlForOption(manifestControls, item.id)
    const nextSelection = optionControl
      ? setSelectionValue(attributeSelection, optionControl, item.id, true)
      : attributeSelection
    const existingIndex = classifierTrail.findIndex((entry) => entry.id === item.id)
    const nextTrail = existingIndex >= 0 ? classifierTrail.slice(0, existingIndex + 1) : [...classifierTrail, item]
    setClassifierTrail(nextTrail)
    setSelectedClassifierId(item.id)
    setAttributeSelection(nextSelection)
    loadFormManifest(nextSelection, item.id)
  }

  const goToClassifierTrail = (index: number) => {
    const item = classifierTrail[index]
    if (!item?.id) return
    const nextTrail = classifierTrail.slice(0, index + 1)
    setClassifierTrail(nextTrail)
    setSelectedClassifierId(item.id)
    loadFormManifest(attributeSelection, item.id)
  }

  const goBackClassifier = () => {
    const removed = classifierTrail[classifierTrail.length - 1]
    const nextTrail = classifierTrail.slice(0, -1)
    const nextClassifierId = nextTrail.length ? nextTrail[nextTrail.length - 1].id : undefined
    const optionControl = removed?.id ? findControlForOption(manifestControls, removed.id) : undefined
    const nextSelection = optionControl
      ? setSelectionValue(attributeSelection, optionControl, removed.id, false)
      : attributeSelection
    setClassifierTrail(nextTrail)
    setSelectedClassifierId(nextClassifierId)
    setAttributeSelection(nextSelection)
    loadFormManifest(nextSelection, nextClassifierId)
  }

  const competitorExamples = competitors.slice(0, 6).map((item) => ({
    title: item.title,
    price: item.price,
    worker: item.worker,
    rating: item.rating,
    reviews: item.reviews,
    description: item.description,
    image_url: item.image_url,
    service_size: item.service_size,
    instruction: item.instruction,
    url: item.share_url,
  }))

  const marketContext = {
    kworks_count: visibleMetrics?.kworks_count,
    demand: visibleMetrics?.demand,
    classifiers: visibleMetrics?.classifiers?.slice(0, 8),
    competitors: competitorExamples,
    practice_context: visibleMetrics?.practice_context,
    filter_scope: visibleMetrics?.filter_scope,
    filter_requests: visibleMetrics?.filter_requests,
    price_steps: priceGradation.slice(0, 12),
  }

  const suggestControl = async (control: KworkFormControl) => {
    if (!selectedCategoryId || suggestingControl) return
    const beforeIds = selectedOptionIds(attributeSelection, control)
    setSuggestingControl(control.name)
    setManifestError(null)
    setFieldNotice(null)
    try {
      const result = await api.suggestKworkAttribute(selectedCategoryId, {
        classifier_id: selectedClassifierId,
        category_name: selectedCategory?.label,
        classifier_name: selectedClassifier?.name,
        service_summary: serviceSummary,
        audience,
        control,
        controls: (formManifest?.controls || []).slice(0, 12),
        selection: attributeSelection,
        market_context: marketContext,
        mode: classifierTrail.map((item) => item.name).join(' / ') || selectedCategory?.label || '',
        use_llm: useLlm,
        lang: 'ru',
      })
      const afterIds = selectedOptionIds(result.selection, control)
      const nextIds = afterIds.length ? afterIds : result.selected_ids
      const context = [
        selectedCategory?.label ? `рубрика: ${selectedCategory.label}` : '',
        classifierTrail.length ? `срез: ${classifierTrail.map((item) => item.name).join(' / ')}` : '',
        visibleMetrics?.kworks_count ? `конкурентов в выдаче: ${formatNumber(visibleMetrics.kworks_count)}` : '',
        competitorExamples.length ? `учтено примеров: ${competitorExamples.length}` : '',
        includeDemand ? `спрос: ${demand.value}` : '',
      ].filter(Boolean)
      setAttributeSelection(result.selection)
      setFieldNotice({
        control: control.name,
        controlLabel: controlLabel(control),
        before: optionLabels(control, beforeIds),
        after: optionLabels(control, nextIds),
        changed: !sameSelection(beforeIds, nextIds),
        reason:
          result.reason ||
          'Выбор синхронизирован с текущей услугой, рубрикой, срезом и доступными вариантами Kwork.',
        source: result.source === 'llm' ? 'ИИ' : 'эвристика',
        confidence: result.confidence,
        context,
      })
      await loadFormManifest(result.selection)
    } catch (e) {
      setManifestError(e instanceof Error ? e.message : String(e))
    } finally {
      setSuggestingControl(null)
    }
  }

  const createDraft = async () => {
    if (!selectedCategoryId) return
    setDraftLoading(true)
    setDraftError(null)
    setPublishResult(null)
    try {
      const result = await api.createKworkDraft({
        category_id: selectedCategoryId,
        category_name: selectedCategory?.label,
        classifier_id: selectedClassifierId,
        classifier_name: selectedClassifier?.name,
        service_summary: serviceSummary,
        audience,
        price,
        work_time: workTime,
        use_llm: useLlm,
        generate_image: generateImage,
        cover_text: serviceSummary,
        cover_subtitle: audience,
        use_cover_prompt_llm: true,
        use_competitor_image_analysis: generateImage,
        market_context: marketContext,
        attribute_manifest: formManifest || {},
        attribute_selection: attributeSelection,
      })
      setDraftResult(result)
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : String(e))
    } finally {
      setDraftLoading(false)
    }
  }

  const currentPublishDraft = () => ({
    ...(draftResult?.draft || {}),
    attribute_manifest: formManifest || {},
    attribute_selection: attributeSelection,
  })

  const dryRunPublish = async () => {
    if (!draftResult?.draft) return
    setDraftLoading(true)
    setDraftError(null)
    try {
      const result = await api.publishKworkDraft(currentPublishDraft(), true)
      setPublishResult(result)
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : String(e))
    } finally {
      setDraftLoading(false)
    }
  }

  const publishLive = async () => {
    if (!draftResult?.draft) return
    const missingControls = (formManifest?.controls || []).filter((control) => {
      if (control.disabled) return false
      if (!control.required) return false
      const value = attributeSelection[control.name]
      return value === undefined || value === null || value === '' || (Array.isArray(value) && value.length === 0)
    })
    if (missingControls.length > 0) {
      setDraftError(`Заполните поля Kwork: ${missingControls.map((item) => item.name).join(', ')}`)
      return
    }
    if (!draftResult.image?.path && !draftResult.draft.cover_upload && !draftResult.draft.cover_image_path) {
      setDraftError('Для live публикации нужна обложка. Сгенерируйте черновик с включенной обложкой.')
      return
    }
    setDraftLoading(true)
    setDraftError(null)
    try {
      const liveDraft = currentPublishDraft()
      const preflight = await api.preflightKworkPublish(liveDraft)
      setPublishResult(preflight)
      if (!preflight.ok || !preflight.token) {
        setDraftError(preflight.preflight?.detail || 'Live preflight failed')
        return
      }
      const phrase = preflight.confirmation_phrase || 'ОПУБЛИКОВАТЬ'
      const confirmation = window.prompt(`Введите ${phrase}, чтобы опубликовать кворк на Kwork`)
      if (confirmation !== phrase) {
        setDraftError('Публикация отменена: фраза подтверждения не совпала.')
        return
      }
      const result = await api.publishKworkDraft(liveDraft, false, {
        confirm_token: preflight.token,
        confirmation,
      })
      setPublishResult(result)
      if (!result.ok) {
        const manual = manualVerificationFromPublishResult(result)
        setDraftError(manual?.detail || result.detail || 'Kwork не принял публикацию')
      }
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : String(e))
    } finally {
      setDraftLoading(false)
    }
  }

  return (
    <div className="space-y-5 p-6 max-lg:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="page-kicker">kwork market</div>
          <h1 className="text-xl font-semibold text-white">Кворки: категории, конкуренты, черновик</h1>
        </div>
        <div className="flex items-center gap-2">
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Запрашивает биржу заказов Kwork для выбранной рубрики/среза. Нужно только для оценки спроса, на публикацию не влияет."
          >
            <input
              type="checkbox"
              checked={includeDemand}
              onChange={(event) => setIncludeDemand(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            спрос по заказам
            <Info className="h-3.5 w-3.5 text-zinc-600" />
          </label>
          <button
            type="button"
            onClick={() => setShowDemandHelp((value) => !value)}
            className={cn(
              'btn btn-ghost py-1.5 text-xs',
              showDemandHelp && 'border-brand-500/40 bg-brand-600/15 text-white',
            )}
            title="Пояснить, что делает проверка спроса"
          >
            <Info className="h-3.5 w-3.5" />
            что это
          </button>
          <button onClick={refreshAll} className="btn btn-ghost py-1.5" title="Обновить">
            <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
          </button>
        </div>
      </div>

      {showDemandHelp && (
        <div className="flex items-start gap-3 rounded-md border border-brand-500/30 bg-brand-600/10 px-3 py-2 text-xs text-brand-100">
          <BarChart3 className="mt-0.5 h-4 w-4 shrink-0 text-brand-300" />
          <div className="space-y-1">
            <div className="font-medium text-white">Что меняет “спрос по заказам”</div>
            <div className="text-brand-100/80">{demandExplanation(visibleMetrics?.demand, includeDemand)}</div>
            <div className="text-brand-100/55">
              Текущий срез: {selectedCategory?.label || 'рубрика не выбрана'}
              {selectedClassifierId ? ` / ${classifierTrail.map((item) => item.name).join(' / ') || selectedClassifier?.name || selectedClassifierId}` : ''}.
            </div>
          </div>
        </div>
      )}

      {anyError && (
        <div className="flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{anyError}</span>
        </div>
      )}

      <section className="space-y-3">
        <div className="flex gap-2 overflow-x-auto pb-1">
          {topCategories.map((root) => (
            <button
              key={root.id}
              onClick={() => chooseRoot(root)}
              className={cn(
                'shrink-0 rounded-md border px-3 py-2 text-sm transition-colors',
                selectedRootId === root.id
                  ? 'border-brand-500/60 bg-brand-600/15 text-white'
                  : 'border-surface-600 bg-surface-900/40 text-zinc-400 hover:text-white',
              )}
            >
              {root.name}
            </button>
          ))}
          {categoriesLoading && <Loader2 className="mt-2 h-4 w-4 animate-spin text-zinc-600" />}
        </div>

        <div className="grid grid-cols-[360px_1fr] gap-4 max-xl:grid-cols-1">
          <div className="space-y-4">
            <div className="card">
              <div className="mb-3 flex items-center gap-2 text-sm font-medium text-zinc-200">
                <Tags className="h-4 w-4 text-brand-400" />
                Рубрика
              </div>
              <div className="grid gap-2">
                {(rootChildren.length ? rootChildren : selectedRoot ? [selectedRoot] : []).map((item) => (
                  <button
                    key={item.id}
                    onClick={() => chooseCategory(item.id)}
                    className={cn(
                      'flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors',
                      selectedCategoryId === item.id
                        ? 'border-brand-500/50 bg-brand-600/15 text-white'
                        : 'border-surface-600 bg-surface-900/40 text-zinc-400 hover:text-white',
                    )}
                  >
                    <span>{item.name}</span>
                    {!!item.kworks_count && <span className="text-zinc-500">{formatNumber(item.kworks_count)}</span>}
                  </button>
                ))}
              </div>
              <div className="mt-3 rounded-md border border-surface-700 bg-surface-950/50 px-3 py-2 text-xs text-zinc-500">
                {selectedCategory?.label || 'Категории загружаются'}
              </div>
            </div>

            <div className="card">
              <div className="mb-3 flex items-center gap-2 text-sm font-medium text-zinc-200">
                <BarChart3 className="h-4 w-4 text-brand-400" />
                Срез внутри рубрики
              </div>
              {classifierTrail.length > 0 && (
                <div className="mb-3 space-y-2 rounded-md border border-surface-700 bg-surface-950/40 p-2">
                  <div className="flex flex-wrap gap-1.5">
                    {classifierTrail.map((item, index) => (
                      <button
                        key={`${item.id}-${index}`}
                        onClick={() => goToClassifierTrail(index)}
                        className="rounded border border-brand-500/30 bg-brand-600/10 px-2 py-1 text-[11px] text-brand-200"
                        title="Вернуться к этому срезу"
                      >
                        {item.name}
                      </button>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={goBackClassifier}
                      className="flex items-center gap-1 rounded-md border border-surface-600 px-2 py-1 text-[11px] text-zinc-400 hover:text-white"
                    >
                      <ChevronLeft className="h-3.5 w-3.5" />
                      назад
                    </button>
                    <button
                      onClick={resetClassifier}
                      className="flex items-center gap-1 rounded-md border border-surface-600 px-2 py-1 text-[11px] text-zinc-400 hover:text-white"
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      вся рубрика
                    </button>
                  </div>
                </div>
              )}
              <div className="grid gap-2">
                <ClassifierButton
                  item={{ id: 0, name: 'Вся рубрика', kworks_count: visibleMetrics?.kworks_count || 0 }}
                  active={!selectedClassifierId}
                  onClick={resetClassifier}
                />
                {(visibleMetrics?.classifiers || []).map((item) => (
                  <ClassifierButton
                    key={item.id}
                    item={item}
                    active={selectedClassifierId === item.id}
                    onClick={() => chooseClassifier(item)}
                  />
                ))}
                {selectedClassifierId && !metricsLoading && !(visibleMetrics?.classifiers || []).length && sliceControls.length === 0 && (
                  <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    Это финальный срез: дальше Kwork уже не отдаёт подварианты. Можно вернуться назад или сбросить до всей рубрики.
                  </div>
                )}
                {selectedClassifierId && !metricsLoading && !(visibleMetrics?.classifiers || []).length && sliceControls.length > 0 && (
                  <div className="rounded-md border border-brand-500/30 bg-brand-600/10 px-3 py-2 text-xs text-brand-100">
                    Дальше у Kwork идут не подрубрики, а фильтры формы. Выбери их ниже, чтобы конкуренты и спрос считались по более точному срезу.
                  </div>
                )}
              </div>
              {sliceControls.length > 0 && (
                <div className="mt-4 rounded-md border border-surface-700 bg-surface-950/35 p-3">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <div>
                      <div className="text-xs font-medium text-zinc-300">Фильтры Kwork для точного среза</div>
                      <div className="mt-0.5 text-[11px] text-zinc-600">
                        Эти пункты тоже сужают конкурентов, обложки и контекст ИИ.
                      </div>
                    </div>
                    {manifestLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-zinc-600" />}
                  </div>
                  <div className="max-h-[520px] space-y-3 overflow-auto pr-1">
                    {sliceControls.map((control) => (
                      <div key={`slice-${control.name}`} className="space-y-2">
                        <div className="flex items-center justify-between gap-2 text-[11px] text-zinc-500">
                          <span className="truncate">
                            {controlLabel(control)}
                            {control.required ? ' *' : ''}
                          </span>
                          <div className="flex shrink-0 items-center gap-1.5">
                            {!!control.options?.length && (
                              <button
                                onClick={() => suggestControl(control)}
                                disabled={!!suggestingControl || !useLlm}
                                className="rounded-md border border-brand-500/40 bg-brand-600/15 p-1 text-brand-200 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                                title="ИИ выберет пункт по услуге, аудитории, срезу и конкурентам"
                              >
                                {suggestingControl === control.name ? (
                                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  <Sparkles className="h-3.5 w-3.5" />
                                )}
                              </button>
                            )}
                            <span>{control.multiple ? 'checkbox' : control.type}</span>
                          </div>
                        </div>
                        <div className="grid gap-1.5">
                          {(control.options || []).map((option) => {
                            const selected = isOptionSelected(attributeSelection, control, option.id)
                            return (
                              <label
                                key={`slice-${control.name}-${option.id}`}
                                className={cn(
                                  'flex items-center gap-2 rounded-md border px-2.5 py-2 text-xs transition-colors',
                                  selected
                                    ? 'border-brand-500/50 bg-brand-600/15 text-white'
                                    : 'border-surface-700 bg-surface-900/30 text-zinc-400',
                                  option.disabled && 'opacity-50',
                                )}
                              >
                                <input
                                  type={control.multiple ? 'checkbox' : 'radio'}
                                  name={`slice-${control.name}`}
                                  checked={selected}
                                  disabled={option.disabled}
                                  onChange={(event) => {
                                    const next = setSelectionValue(
                                      attributeSelection,
                                      control,
                                      option.id,
                                      event.target.checked,
                                    )
                                    setAttributeSelection(next)
                                    scheduleFormManifest(next, selectedClassifierId)
                                  }}
                                  className="h-3.5 w-3.5 accent-brand-500"
                                />
                                <span className="min-w-0 flex-1 truncate">{option.label || `#${option.id}`}</span>
                                <span className="text-[10px] text-zinc-600">#{option.id}</span>
                              </label>
                            )
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className="space-y-4">
            <div className="grid grid-cols-4 gap-3 max-2xl:grid-cols-2 max-md:grid-cols-1">
              <MetricTile label="конкурентов" value={formatNumber(visibleMetrics?.kworks_count)} sub="по выбранному срезу" />
              <MetricTile label="заказы" value={demand.value} sub={demand.sub} />
              <MetricTile label="твоя цена" value={formatPrice(price)} sub="настраивается вручную" />
              <MetricTile label="примеров для ИИ" value={competitors.filter((item) => item.description).length} sub="описания конкурентов" />
            </div>

            <div className="grid grid-cols-[1fr_420px] gap-4 max-2xl:grid-cols-1">
              <div className="card">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-sm font-medium text-zinc-200">
                    <Store className="h-4 w-4 text-brand-400" />
                    Конкуренты с главной выдачи
                  </div>
                  {metricsLoading && <Loader2 className="h-4 w-4 animate-spin text-zinc-600" />}
                </div>
                {competitors.length === 0 ? (
                  <div className="py-10 text-center text-xs text-zinc-600">Нет данных по конкурентам</div>
                ) : (
                  <div className="grid grid-cols-2 gap-3 max-lg:grid-cols-1">
                    {competitors.slice(0, 6).map((item, index) => (
                      <CompetitorCard key={`${item.id || index}`} item={item} />
                    ))}
                  </div>
                )}
              </div>

              <div className="card">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium text-zinc-200">
                  <Wand2 className="h-4 w-4 text-brand-400" />
                  Черновик кворка
                </div>
                <div className="space-y-3">
                  <label className="block text-xs text-zinc-500">
                    Услуга
                    <textarea
                      value={serviceSummary}
                      onChange={(event) => setServiceSummary(event.target.value)}
                      className="input mt-1 min-h-[72px] w-full resize-y"
                    />
                  </label>
                  <label className="block text-xs text-zinc-500">
                    Для кого
                    <input value={audience} onChange={(event) => setAudience(event.target.value)} className="input mt-1 w-full" />
                  </label>
                  <div className="grid grid-cols-2 gap-3">
                    <label className="block text-xs text-zinc-500">
                      Цена
                      <input
                        type="number"
                        min={1}
                        value={price}
                        onChange={(event) => setPrice(Number(event.target.value))}
                        className="input mt-1 w-full"
                      />
                    </label>
                    <label className="block text-xs text-zinc-500">
                      Дней
                      <input
                        type="number"
                        min={1}
                        value={workTime}
                        onChange={(event) => setWorkTime(Number(event.target.value))}
                        className="input mt-1 w-full"
                      />
                    </label>
                  </div>
                  {priceGradation.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {priceGradation.slice(0, 10).map((step) => (
                        <button
                          key={step}
                          onClick={() => setPrice(step)}
                          className={cn(
                            'rounded-md border px-2.5 py-1 text-xs',
                            price === step
                              ? 'border-brand-500/50 bg-brand-600/15 text-white'
                              : 'border-surface-600 text-zinc-400 hover:text-white',
                          )}
                        >
                          {formatPrice(step)}
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="rounded-md border border-surface-700 bg-surface-950/40 p-3">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <div className="text-xs font-medium text-zinc-300">Поля Kwork</div>
                      <button
                        onClick={() => loadFormManifest(attributeSelection)}
                        disabled={!selectedCategoryId || manifestLoading}
                        className="rounded-md border border-surface-600 px-2 py-1 text-[11px] text-zinc-400 hover:text-white"
                      >
                        {manifestLoading ? 'загрузка' : 'обновить'}
                      </button>
                    </div>
                    {manifestError && (
                      <div className="mb-2 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-200">
                        <div>{manifestError}</div>
                        {formManifest?.code === 'manual_verification_required' && (
                          <button
                            onClick={openManifestVerification}
                            disabled={verificationOpening}
                            className="mt-2 inline-flex items-center gap-1 rounded-md border border-red-400/40 px-2 py-1 text-[11px] text-red-100 hover:border-red-300 disabled:opacity-60"
                          >
                            {verificationOpening ? <Loader2 className="h-3 w-3 animate-spin" /> : <ExternalLink className="h-3 w-3" />}
                            открыть проверку в приложении
                          </button>
                        )}
                      </div>
                    )}
                    {fieldNotice && (
                      <div className="mb-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2 text-[11px] text-emerald-100">
                        <div className="mb-2 flex items-start justify-between gap-2">
                          <div>
                            <div className="font-medium">ИИ-разбор поля: {fieldNotice.controlLabel}</div>
                            <div className="text-emerald-200/70">
                              {fieldNotice.changed ? 'значение обновлено' : 'текущий выбор оставлен'} · источник:{' '}
                              {fieldNotice.source}
                              {fieldNotice.confidence ? ` · уверенность ${Math.round(fieldNotice.confidence * 100)}%` : ''}
                            </div>
                          </div>
                          <button
                            onClick={() => setFieldNotice(null)}
                            className="rounded border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-200/80 hover:text-white"
                          >
                            скрыть
                          </button>
                        </div>
                        <div className="grid grid-cols-2 gap-2 max-sm:grid-cols-1">
                          <div className="rounded border border-surface-700/70 bg-surface-950/40 p-2">
                            <div className="mb-1 text-[10px] uppercase tracking-[0.08em] text-zinc-500">было</div>
                            <div className="flex flex-wrap gap-1">
                              {(fieldNotice.before.length ? fieldNotice.before : ['не выбрано']).map((item) => (
                                <span key={`before-${item}`} className="rounded bg-surface-800 px-1.5 py-0.5 text-zinc-300">
                                  {item}
                                </span>
                              ))}
                            </div>
                          </div>
                          <div className="rounded border border-emerald-500/20 bg-emerald-500/5 p-2">
                            <div className="mb-1 text-[10px] uppercase tracking-[0.08em] text-emerald-300/70">стало</div>
                            <div className="flex flex-wrap gap-1">
                              {(fieldNotice.after.length ? fieldNotice.after : ['не выбрано']).map((item) => (
                                <span key={`after-${item}`} className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-100">
                                  {item}
                                </span>
                              ))}
                            </div>
                          </div>
                        </div>
                        <div className="mt-2 rounded border border-surface-700/70 bg-surface-950/35 p-2 text-zinc-300">
                          <span className="text-emerald-300">Почему лучше:</span> {fieldNotice.reason}
                        </div>
                        {fieldNotice.context.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-1">
                            {fieldNotice.context.map((item) => (
                              <span key={item} className="rounded border border-surface-700 px-1.5 py-0.5 text-zinc-500">
                                {item}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {manifestLoading && !formManifest ? (
                      <div className="flex justify-center py-6">
                        <Loader2 className="h-4 w-4 animate-spin text-zinc-600" />
                      </div>
                    ) : !formManifest?.controls?.length ? (
                      <div className="py-4 text-center text-xs text-zinc-600">Нет динамических полей формы</div>
                    ) : (
                      <div className="space-y-3">
                        {formManifest.controls.map((control) => (
                          <div key={control.name} className={cn('space-y-2', control.disabled && 'opacity-50')}>
                            <div className="flex items-center justify-between gap-2 text-[11px] text-zinc-500">
                              <span className="truncate">
                                {controlLabel(control)}
                                {control.required ? ' *' : ''}
                              </span>
                              <div className="flex shrink-0 items-center gap-1.5">
                                {!control.disabled && !!control.options?.length && (
                                  <button
                                    onClick={() => suggestControl(control)}
                                    disabled={!!suggestingControl || !useLlm}
                                    className="rounded-md border border-brand-500/40 bg-brand-600/15 p-1 text-brand-200 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                                    title="ИИ выберет или пересмотрит пункт по услуге, аудитории, срезу и конкурентам"
                                  >
                                    {suggestingControl === control.name ? (
                                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                    ) : (
                                      <Sparkles className="h-3.5 w-3.5" />
                                    )}
                                  </button>
                                )}
                                <span>{control.disabled ? 'disabled' : control.multiple ? 'checkbox' : control.type}</span>
                              </div>
                            </div>
                            {control.options?.length ? (
                              <div className="grid gap-1.5">
                                {control.options.map((option) => {
                                  const selected = isOptionSelected(attributeSelection, control, option.id)
                                  return (
                                    <label
                                      key={`${control.name}-${option.id}`}
                                      className={cn(
                                        'flex items-center gap-2 rounded-md border px-2.5 py-2 text-xs transition-colors',
                                        selected
                                          ? 'border-brand-500/50 bg-brand-600/15 text-white'
                                          : 'border-surface-700 bg-surface-900/30 text-zinc-400',
                                        option.disabled && 'opacity-50',
                                      )}
                                    >
                                      <input
                                        type={control.multiple ? 'checkbox' : 'radio'}
                                        name={control.name}
                                        checked={selected}
                                        disabled={control.disabled || option.disabled}
                                        onChange={(event) => {
                                          const next = setSelectionValue(
                                            attributeSelection,
                                            control,
                                            option.id,
                                            event.target.checked,
                                          )
                                          setAttributeSelection(next)
                                          scheduleFormManifest(next, selectedClassifierId)
                                        }}
                                        className="h-3.5 w-3.5 accent-brand-500"
                                      />
                                      <span className="min-w-0 flex-1 truncate">{option.label || `#${option.id}`}</span>
                                      <span className="text-[10px] text-zinc-600">#{option.id}</span>
                                    </label>
                                  )
                                })}
                              </div>
                            ) : (
                              <input
                                value={String(attributeSelection[control.name] ?? control.value ?? '')}
                                placeholder={control.placeholder || control.name}
                                disabled={control.disabled}
                                onChange={(event) => {
                                  setAttributeSelection({ ...attributeSelection, [control.name]: event.target.value })
                                }}
                                onBlur={(event) => {
                                  scheduleFormManifest({ ...attributeSelection, [control.name]: event.target.value }, selectedClassifierId, 400)
                                }}
                                className="w-full rounded-md border border-surface-700 bg-surface-950 px-2.5 py-2 text-xs text-zinc-200 outline-none focus:border-brand-500/60"
                              />
                            )}
                          </div>
                        ))}
                        {!!formManifest.unresolved_required?.length && (
                          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-200">
                            Не заполнено: {formManifest.unresolved_required.join(', ')}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <label className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400">
                      <input
                        type="checkbox"
                        checked={useLlm}
                        onChange={(event) => setUseLlm(event.target.checked)}
                        className="h-3.5 w-3.5 accent-brand-500"
                      />
                      ИИ
                    </label>
                    <label className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400">
                      <input
                        type="checkbox"
                        checked={generateImage}
                        onChange={(event) => setGenerateImage(event.target.checked)}
                        className="h-3.5 w-3.5 accent-brand-500"
                      />
                      обложка
                    </label>
                  </div>
                  {draftError && (
                    <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                      {draftError}
                      {publishManualVerification && (
                        <button
                          onClick={openPublishVerification}
                          disabled={verificationOpening}
                          className="mt-2 inline-flex items-center gap-1 rounded-md border border-red-400/40 px-2 py-1 text-[11px] text-red-100 hover:border-red-300 disabled:opacity-60"
                        >
                          {verificationOpening ? <Loader2 className="h-3 w-3 animate-spin" /> : <ExternalLink className="h-3 w-3" />}
                          открыть проверку Kwork
                        </button>
                      )}
                    </div>
                  )}
                  <div className="flex gap-2">
                    <button onClick={createDraft} disabled={!selectedCategoryId || draftLoading} className="btn btn-primary">
                      {draftLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
                      Сгенерировать
                    </button>
                    <button onClick={dryRunPublish} disabled={!draftResult?.draft || draftLoading} className="btn btn-ghost">
                      Dry-run
                    </button>
                    <button onClick={publishLive} disabled={!draftResult?.draft || draftLoading} className="btn btn-ghost">
                      Опубликовать
                    </button>
                  </div>
                </div>
              </div>
            </div>

            {draftResult?.draft && (
              <div className="grid grid-cols-[360px_1fr] gap-4 max-xl:grid-cols-1">
                <div className="card">
                  <div className="mb-3 text-sm font-medium text-zinc-200">Обложка</div>
                  {draftResult.image?.asset_url ? (
                    <div className="space-y-2">
                      <img src={draftResult.image.asset_url} alt="" className="aspect-[3/2] w-full rounded-md object-cover" />
                      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-zinc-500">
                        <span>
                          {draftResult.image.status === 'generated_local_fallback' ? 'локальная fallback-обложка' : draftResult.image.status}
                          {draftResult.image.prompt_source ? ` · prompt: ${draftResult.image.prompt_source}` : ''}
                          {draftResult.image.text_overlay ? ' · текст наложен' : ''}
                        </span>
                        {hasCoverVisionAnalysis && (
                          <span
                            className="rounded border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] text-emerald-300"
                            title={draftResult.image.visual_style_brief || draftResult.image.visual_analysis_detail || undefined}
                          >
                            анализ: {formatNumber(coverVisionImagesSeen)} обложек конкурентов
                          </span>
                        )}
                        {coverPromptRoute && (
                          <span className="rounded border border-brand-500/30 bg-brand-600/10 px-1.5 py-0.5 text-[11px] text-brand-200">
                            writer: {coverPromptRoute}
                          </span>
                        )}
                        {coverPromptContext && (
                          <span className="rounded border border-surface-600 px-1.5 py-0.5 text-[11px] text-zinc-400">
                            в prompt: {formatNumber(coverContextNumber(coverPromptContext, 'competitor_images_sent'))} конкурент. ·{' '}
                            {formatNumber(coverContextNumber(coverPromptContext, 'history_images_sent'))} история
                          </span>
                        )}
                      </div>
                      {draftResult.image.detail && (
                        <div className="rounded-md border border-surface-700 bg-surface-950/60 px-3 py-2 text-xs text-zinc-500">
                          {draftResult.image.detail}
                        </div>
                      )}
                      {draftResult.image.prompt && (
                        <details className="rounded-md border border-surface-700 bg-surface-950/60 p-3 text-xs text-zinc-500">
                          <summary className="cursor-pointer text-zinc-400">prompt обложки</summary>
                          <div className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap">{draftResult.image.prompt}</div>
                        </details>
                      )}
                      {coverPromptContext && (
                        <details className="rounded-md border border-surface-700 bg-surface-950/60 p-3 text-xs text-zinc-500">
                          <summary className="cursor-pointer text-zinc-400">диагностика обложки</summary>
                          <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] max-sm:grid-cols-1">
                            <div>route: {coverPromptRoute || '-'}</div>
                            <div>prompt images: {formatNumber(coverContextNumber(coverPromptContext, 'prompt_images_sent'))}</div>
                            <div>competitors sent: {formatNumber(coverContextNumber(coverPromptContext, 'competitor_images_sent'))}</div>
                            <div>history sent: {formatNumber(coverContextNumber(coverPromptContext, 'history_images_sent'))}</div>
                            <div>history known: {formatNumber(coverContextNumber(coverPromptContext, 'recent_history_count'))}</div>
                            <div>sidecar: {draftResult.image.sidecar_path || '-'}</div>
                          </div>
                          {typeof coverPromptContext.warning === 'string' && coverPromptContext.warning && (
                            <div className="mt-2 rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-amber-200/80">
                              {coverPromptContext.warning}
                            </div>
                          )}
                        </details>
                      )}
                    </div>
                  ) : (
                    <div className="flex aspect-[3/2] items-center justify-center rounded-md border border-surface-700 text-zinc-700">
                      <ImageIcon className="h-8 w-8" />
                    </div>
                  )}
                </div>

                <div className="card">
                  <div className="mb-3 text-sm font-medium text-zinc-200">Предпросмотр</div>
                  <div className="space-y-3 text-xs">
                    <div>
                      <div className="mono-label">title</div>
                      <div className="mt-1 text-base font-medium text-white">{draftResult.draft.title}</div>
                    </div>
                    <div>
                      <div className="mono-label">description</div>
                      <div className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap text-zinc-300">
                        {draftResult.draft.description}
                      </div>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-zinc-500">
                      <span>цена: {formatPrice(draftResult.draft.price)}</span>
                      <span>срок: {draftResult.draft.work_time} дн.</span>
                      <span>cat: {draftResult.draft.category_id}</span>
                    </div>
                    {publishResult?.payload && (
                      <details className="rounded-md border border-surface-700 bg-surface-950/60 p-3">
                        <summary className="cursor-pointer text-zinc-400">
                          {publishResult.dry_run ? 'payload dry-run' : publishResult.ok ? 'опубликовано' : 'публикация не прошла'}
                        </summary>
                        {publishResult.detail && <div className="mt-2 text-xs text-zinc-400">{publishResult.detail}</div>}
                        <pre className="mt-2 max-h-56 overflow-auto text-[11px] text-zinc-500">
                          {JSON.stringify(publishResult.payload, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                </div>
              </div>
            )}

            <details className="card">
              <summary className="cursor-pointer text-sm font-medium text-zinc-300">
                Диагностика categoryAttributes ({requiredAttributes.length})
              </summary>
              {attributesLoading ? (
                <div className="flex justify-center py-8">
                  <Loader2 className="h-5 w-5 animate-spin text-zinc-600" />
                </div>
              ) : requiredAttributes.length === 0 ? (
                <div className="py-8 text-center text-xs text-zinc-600">Нет обязательных полей</div>
              ) : (
                <div className="mt-3 grid grid-cols-2 gap-2 max-lg:grid-cols-1">
                  {requiredAttributes.map((item) => (
                    <div key={`${item.id}-${item.path}`} className="rounded-md border border-surface-700 px-3 py-2">
                      <div className="text-xs text-zinc-300">{item.path}</div>
                      <div className="mt-1 flex gap-2 text-[11px] text-zinc-600">
                        <span>id={item.id}</span>
                        {item.kworks_count > 0 && <span>{formatNumber(item.kworks_count)} кворков</span>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </details>
          </div>
        </div>
      </section>
    </div>
  )
}
