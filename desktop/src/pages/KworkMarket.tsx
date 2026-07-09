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
  type KworkBuyerScout,
  type KworkMarketIntelligenceHistory,
  type KworkMarketIntelligenceSnapshot,
  type KworkMarketMetrics,
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

const MARKET_SCAN_DEFAULT_SEEDS: Array<Record<string, unknown>> = [
  { name: 'Программирование широко', category_id: 41 },
  { name: 'Telegram', category_id: 46, classifier_id: 281 },
  { name: 'Ссылки', category_id: 59 },
  { name: 'Логотипы', category_id: 25, classifier_id: 401928 },
]

const KWORK_MARKET_STATE_KEY = 'psr:kwork-market:v3'

function readKworkMarketState(): Record<string, any> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(KWORK_MARKET_STATE_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeKworkMarketState(state: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  const compactState = {
    ...state,
    buyerScoutResult: compactBuyerScoutResult(state.buyerScoutResult),
    marketScanResult: compactMarketScanResult(state.marketScanResult),
  }
  try {
    window.localStorage.setItem(KWORK_MARKET_STATE_KEY, JSON.stringify(compactState))
  } catch {
    try {
      window.localStorage.setItem(
        KWORK_MARKET_STATE_KEY,
        JSON.stringify({
          ...compactState,
          buyerScoutResult: null,
          marketScanResult: null,
          formManifest: compactFormManifest(state.formManifest),
        }),
      )
    } catch {
      // localStorage can be full or disabled; losing this cache must not break the screen.
    }
  }
}

function compactFormManifest(value: unknown) {
  if (!value || typeof value !== 'object') return null
  const manifest = value as Record<string, unknown>
  return {
    category_id: manifest.category_id,
    classifier_id: manifest.classifier_id,
    success: manifest.success,
    code: manifest.code,
    selected: manifest.selected,
    controls: Array.isArray(manifest.controls) ? manifest.controls : [],
    unresolved_required: Array.isArray(manifest.unresolved_required) ? manifest.unresolved_required : [],
  }
}

function compactMarketScanResult(value: unknown) {
  if (!value || typeof value !== 'object') return null
  const result = value as Record<string, any>
  const supply = Array.isArray(result.supply) ? result.supply.slice(0, 10) : []
  const sellerIntelligence = Array.isArray(result.seller_intelligence) ? result.seller_intelligence.slice(0, 8) : []
  return {
    generated_at: result.generated_at,
    source: result.source,
    config: result.config,
    seeds: Array.isArray(result.seeds) ? result.seeds.slice(0, 12) : [],
    supply,
    query_demand: result.query_demand,
    seller_intelligence: sellerIntelligence,
    market_rankings: Array.isArray(result.market_rankings) ? result.market_rankings.slice(0, 12) : [],
    aggregate: result.aggregate,
    file_path: result.file_path,
    latest_path: result.latest_path,
    index_path: result.index_path,
    account_context: result.account_context,
    timings_ms: result.timings_ms,
  }
}

function compactBuyerScoutResult(value: unknown) {
  if (!value || typeof value !== 'object') return null
  const result = value as Record<string, any>
  const top = Array.isArray(result.top) ? result.top.slice(0, 12) : []
  const probes = Array.isArray(result.probes) ? result.probes.slice(0, 12) : []
  return {
    generated_at: result.generated_at,
    status: result.status,
    aggregate: result.aggregate,
    buyer_summary: result.aggregate?.buyer_summary || null,
    market_signals: Array.isArray(result.aggregate?.market_signals) ? result.aggregate.market_signals.slice(0, 8) : [],
    probes: probes.map((item: Record<string, unknown>) => ({
      name: item.name,
      query: item.query,
      status: item.status,
      count: item.count,
      sample_count: item.sample_count,
      filters: item.filters,
      meta: item.meta,
    })),
    top: top.map((item: Record<string, unknown>) => ({
      id: item.id,
      score: item.score,
      score_reasons: item.score_reasons,
      matched_probe: item.matched_probe,
      title: item.title,
      offers: item.offers,
      price: item.price,
      possible_price_limit: item.possible_price_limit,
      allow_higher_price: item.allow_higher_price,
      views: item.views,
      orders: item.orders,
      username: item.username,
      user_id: item.user_id,
      user_projects_count: item.user_projects_count,
      user_active_projects_count: item.user_active_projects_count,
      user_hired_percent: item.user_hired_percent,
      buyer_projects_url: item.buyer_projects_url,
      description: item.description,
      project_detail: item.project_detail,
      want_detail: item.want_detail,
      buyer_history: item.buyer_history,
    })),
    endpoint_errors: result.endpoint_errors,
    timings_ms: result.timings_ms,
    file_path: result.file_path,
  }
}

function numberOrNull(value: unknown) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : null
}

function numberOrUndefined(value: unknown) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : undefined
}

function stringOrDefault(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function boolOrDefault(value: unknown, fallback = false) {
  return typeof value === 'boolean' ? value : fallback
}

function objectOrDefault<T>(value: unknown, fallback: T): T {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as T) : fallback
}

function arrayOrDefault<T>(value: unknown, fallback: T[]): T[] {
  return Array.isArray(value) ? (value as T[]) : fallback
}

function buildMarketScanSeeds(primary?: Record<string, unknown>) {
  const result: Array<Record<string, unknown>> = []
  if (primary?.category_id) result.push(primary)
  result.push(...MARKET_SCAN_DEFAULT_SEEDS)

  const seen = new Set<string>()
  return result.filter((seed) => {
    const key = `${String(seed.category_id || '')}:${String(seed.classifier_id || '')}`
    if (!seed.category_id || seen.has(key)) return false
    seen.add(key)
    return true
  })
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

function formatPercent(value?: number | string | null) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return `${Math.round((n <= 1 ? n * 100 : n))}%`
}

function finiteNumber(value: unknown) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function hasMeaningfulMarketOpportunity(row: Record<string, unknown>) {
  const score = finiteNumber(row.opportunity_score)
  const demand = finiteNumber(row.demand_per_1000_kworks)
  const wants = finiteNumber(row.demand_wants_count)
  return Boolean(row.seed_name || row.category_id) && [score, demand, wants].some((value) => value !== null && value > 0)
}

function marketOpportunityLabel(row: Record<string, unknown>) {
  const name = String(row.seed_name || row.category_name || row.category_id || 'срез')
  const score = finiteNumber(row.opportunity_score)
  const demand = finiteNumber(row.demand_per_1000_kworks)
  const supply = finiteNumber(row.supply_kworks_count)
  const parts = [
    score !== null ? `балл ${formatNumber(score)}` : '',
    demand !== null ? `спрос ${formatNumber(demand)}/1000` : '',
    supply !== null ? `конкурентов ${formatNumber(supply)}` : '',
  ].filter(Boolean)
  return parts.length ? `${name}: ${parts.join(' · ')}` : name
}

function marketTermLabel(item: { term?: string; count?: number }) {
  const term = String(item.term || '').trim()
  return item.count ? `${term} ${formatNumber(item.count)}` : term
}

function buyerProjectBudget(project: Record<string, unknown>) {
  const direct = finiteNumber(project.price)
  const possible = finiteNumber(project.possible_price_limit)
  if (direct !== null && possible !== null && possible > direct) return `${formatPrice(direct)} -> ${formatPrice(possible)}`
  if (direct !== null) return formatPrice(direct)
  if (possible !== null) return formatPrice(possible)
  return '-'
}

function buyerProjectDescription(project: Record<string, unknown>) {
  const projectDetail = objectOrDefault(project.project_detail, {} as Record<string, unknown>)
  const wantDetail = objectOrDefault(project.want_detail, {} as Record<string, unknown>)
  return cleanCompetitorText(project.description || projectDetail.description || wantDetail.description || '')
}

function buyerScoutSignals(result: KworkBuyerScout | null | undefined) {
  const aggregate = objectOrDefault(result?.aggregate, {} as Record<string, unknown>)
  return Array.isArray(aggregate.market_signals) ? (aggregate.market_signals as Array<Record<string, unknown>>) : []
}

function buyerQuerySuggestions(result: KworkBuyerScout | null | undefined) {
  const aggregate = objectOrDefault(result?.aggregate, {} as Record<string, unknown>)
  return Array.isArray(aggregate.query_suggestions)
    ? (aggregate.query_suggestions as Array<Record<string, unknown>>)
    : []
}

function buyerLotsNeedTokenMode(result: KworkBuyerScout | null | undefined) {
  const probes = Array.isArray(result?.probes) ? (result?.probes as Array<Record<string, unknown>>) : []
  return probes.some((probe) => objectOrDefault(probe.meta, {} as Record<string, unknown>).token_required === true)
}

function buyerProjectViews(project: Record<string, unknown>) {
  const wantDetail = objectOrDefault(project.want_detail, {} as Record<string, unknown>)
  return finiteNumber(project.views) ?? finiteNumber(wantDetail.views)
}

function buyerProjectHistory(project: Record<string, unknown>) {
  const history = objectOrDefault(project.buyer_history, {} as Record<string, unknown>)
  const projectDetail = objectOrDefault(project.project_detail, {} as Record<string, unknown>)
  const wantDetail = objectOrDefault(project.want_detail, {} as Record<string, unknown>)
  const username = String(
    project.username || projectDetail.username || wantDetail.username || history.username || '',
  ).trim()
  const total =
    finiteNumber(history.total) ??
    finiteNumber(history.projects_count) ??
    finiteNumber(project.user_projects_count) ??
    finiteNumber(projectDetail.user_projects_count)
  const active =
    finiteNumber(history.active_count) ??
    finiteNumber(project.user_active_projects_count) ??
    finiteNumber(projectDetail.user_active_projects_count)
  const hired =
    finiteNumber(project.user_hired_percent) ??
    finiteNumber(projectDetail.user_hired_percent) ??
    finiteNumber(wantDetail.user_hired_percent)
  const projects = Array.isArray(history.projects)
    ? (history.projects as Array<Record<string, unknown>>).slice(0, 3)
    : []
  const url = String(project.buyer_projects_url || projectDetail.buyer_projects_url || history.url || '').trim()
  return { username, total, active, hired, projects, url, status: String(history.status || '') }
}

function snapshotDemandCount(value: unknown) {
  const demand = objectOrDefault(value, {} as Record<string, unknown>)
  return finiteNumber(demand.wants_count) ?? finiteNumber(demand.count) ?? 0
}

function snapshotSampleCount(value: unknown) {
  const demand = objectOrDefault(value, {} as Record<string, unknown>)
  return finiteNumber(demand.sample_count) ?? (Array.isArray(demand.sample) ? demand.sample.length : 0)
}

function priceRulesSummary(value: unknown) {
  const priceRules = objectOrDefault(value, {} as Record<string, unknown>)
  if (!priceRules || priceRules.status === 'skipped') return ''
  const prices = objectOrDefault(priceRules.prices, priceRules as Record<string, unknown>)
  const min = finiteNumber(prices.minPrice ?? prices.min_price ?? priceRules.minPrice)
  const max = finiteNumber(prices.maxPrice ?? prices.max_price ?? priceRules.maxPrice)
  if (min !== null || max !== null) return `${formatPrice(min)} - ${formatPrice(max)}`
  return String(priceRules.status || '')
}

function snapshotSeedName(seed: unknown) {
  const value = objectOrDefault(seed, {} as Record<string, unknown>)
  return String(value.name || value.title || value.category_name || value.category_id || 'slice')
}

function buyerProbeSummary(probe: Record<string, unknown>) {
  const filters = objectOrDefault(probe.filters, {} as Record<string, unknown>)
  const bits = [
    probe.query ? `query=${String(probe.query)}` : '',
    filters.kworks_filter_to !== undefined ? `offers<=${String(filters.kworks_filter_to)}` : '',
    filters.kworks_filter_from !== undefined ? `offers>=${String(filters.kworks_filter_from)}` : '',
    filters.price_from !== undefined ? `price>=${formatNumber(String(filters.price_from))}` : '',
    filters.price_to !== undefined ? `price<=${formatNumber(String(filters.price_to))}` : '',
  ].filter(Boolean)
  return bits.join(' · ')
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

function selectionTouchesDynamicChildren(control: KworkFormControl, beforeIds: number[], afterIds: number[]) {
  const childOptionIds = new Set(
    (control.options || [])
      .filter((option) => option.has_child)
      .map((option) => Number(option.id))
      .filter((optionId) => Number.isFinite(optionId)),
  )
  if (!childOptionIds.size) return false
  return [...beforeIds, ...afterIds].some((optionId) => childOptionIds.has(Number(optionId)))
}

function findControlForOption(controls: KworkFormControl[], optionId: number) {
  return controls.find((control) => (control.options || []).some((option) => Number(option.id) === Number(optionId)))
}

function hasSelectableOptions(control: KworkFormControl) {
  return !control.disabled && !!control.options?.some((option) => !option.disabled)
}

function controlTypeLabel(control: KworkFormControl) {
  if (control.disabled) return 'недоступно'
  if (control.multiple) return 'несколько'
  if (control.type === 'radio') return 'один вариант'
  if (control.type === 'select') return 'список'
  if (control.type === 'checkbox') return 'галочки'
  return control.type || 'поле'
}

function cleanCompetitorText(value: unknown) {
  if (value === null || value === undefined) return ''
  if (Array.isArray(value)) return value.map(cleanCompetitorText).filter(Boolean).join(' ')
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    for (const key of [
      'volume_service_in_kwork',
      'unit_and_quantity',
      'service_size',
      'description',
      'title',
      'name',
      'label',
      'text',
      'value',
    ]) {
      const nested = cleanCompetitorText(record[key])
      if (nested) return nested
    }
    return ''
  }
  let text = String(value || '')
  if (/volume_type_id|base_volume|volume_types/i.test(text)) {
    const serviceSizeMatch = text.match(/['"]volume_service_in_kwork['"]\s*:\s*['"]([^'"]*)['"]/i)
    text = serviceSizeMatch?.[1] || ''
  }
  if (!text.trim()) return ''
  if (text.includes('&')) {
    const container = document.createElement('div')
    container.innerHTML = text
    text = container.textContent || container.innerText || text
  }
  if (/<[a-z][\s\S]*>/i.test(text)) {
    const container = document.createElement('div')
    container.innerHTML = text
    text = container.textContent || container.innerText || text
  }
  return text
    .replace(/&nbsp;/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function competitorServiceSize(item: KworkCompetitor) {
  const raw = item.raw && typeof item.raw === 'object' ? (item.raw as Record<string, unknown>) : {}
  return cleanCompetitorText(
    item.service_size ||
      raw.volume_service_in_kwork ||
      raw.unit_and_quantity ||
      raw.service_size ||
      raw.volume ||
      raw.gwork ||
      '',
  )
}

function competitorDescription(item: KworkCompetitor) {
  const raw = item.raw && typeof item.raw === 'object' ? (item.raw as Record<string, unknown>) : {}
  return cleanCompetitorText(
    item.description ||
      raw.kwork_description ||
      raw.description ||
      raw.short_description ||
      raw.shortDescription ||
      raw.preview_description ||
      raw.gdesc ||
      '',
  )
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
  const description = competitorDescription(item)
  const serviceSize = competitorServiceSize(item)
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
              {item.worker || 'продавец'} {item.seller_level ? `· ${item.seller_level}` : ''}
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
        {serviceSize && <div className="text-xs text-zinc-300">{serviceSize}</div>}
        {description ? (
          <div className="max-h-28 overflow-hidden text-xs leading-relaxed text-zinc-400">{description}</div>
        ) : (
          <div className="text-xs text-zinc-600">
            {item.detail_status === 'error'
              ? 'Описание не удалось забрать'
              : item.detail_status === 'details_off'
                ? 'Описание выключено для скорости'
                : 'Включи «описания», чтобы докачать описание через API'}
          </div>
        )}
      </div>
    </article>
  )
}

export default function KworkMarket() {
  const persisted = useMemo(() => readKworkMarketState(), [])
  const [selectedRootId, setSelectedRootId] = useState<number | null>(() => numberOrNull(persisted.selectedRootId))
  const [selectedCategoryId, setSelectedCategoryId] = useState<number | null>(() => numberOrNull(persisted.selectedCategoryId))
  const [selectedClassifierId, setSelectedClassifierId] = useState<number | undefined>(() => numberOrUndefined(persisted.selectedClassifierId))
  const [includeDemand, setIncludeDemand] = useState(() => boolOrDefault(persisted.includeDemand, false))
  const [includeCompetitorDetails, setIncludeCompetitorDetails] = useState(() => boolOrDefault(persisted.includeCompetitorDetails, false))
  const [scanWantDetails, setScanWantDetails] = useState(() => boolOrDefault(persisted.scanWantDetails, false))
  const [scanPriceRules, setScanPriceRules] = useState(() => boolOrDefault(persisted.scanPriceRules, false))
  const [scanSellerDetails, setScanSellerDetails] = useState(() => boolOrDefault(persisted.scanSellerDetails, false))
  const [scanAccountContext, setScanAccountContext] = useState(() => boolOrDefault(persisted.scanAccountContext, false))
  const [buyerBudgetMax, setBuyerBudgetMax] = useState(() => Number(persisted.buyerBudgetMax || 5000))
  const [showDemandHelp, setShowDemandHelp] = useState(false)
  const [marketScanLoading, setMarketScanLoading] = useState(false)
  const [marketScanError, setMarketScanError] = useState<string | null>(null)
  const [marketScanResult, setMarketScanResult] = useState<KworkMarketIntelligenceSnapshot | null>(() =>
    objectOrDefault(persisted.marketScanResult, null as unknown as KworkMarketIntelligenceSnapshot | null),
  )
  const [buyerScoutResult, setBuyerScoutResult] = useState<KworkBuyerScout | null>(() =>
    objectOrDefault(persisted.buyerScoutResult, null as unknown as KworkBuyerScout | null),
  )
  const [marketHistoryLoading, setMarketHistoryLoading] = useState(false)
  const [marketHistory, setMarketHistory] = useState<KworkMarketIntelligenceHistory | null>(null)
  const [serviceSummary, setServiceSummary] = useState(() => stringOrDefault(persisted.serviceSummary, ''))
  const [audience, setAudience] = useState(() => stringOrDefault(persisted.audience, ''))
  const [price, setPrice] = useState(() => Number(persisted.price || 1000))
  const [workTime, setWorkTime] = useState(() => Number(persisted.workTime || 3))
  const [useLlm, setUseLlm] = useState(() => boolOrDefault(persisted.useLlm, true))
  const [generateImage, setGenerateImage] = useState(() => boolOrDefault(persisted.generateImage, true))
  const [draftLoading, setDraftLoading] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [draftResult, setDraftResult] = useState<KworkDraftResult | null>(() =>
    objectOrDefault(persisted.draftResult, null as unknown as KworkDraftResult | null),
  )
  const [publishResult, setPublishResult] = useState<Record<string, any> | null>(() =>
    objectOrDefault(persisted.publishResult, null as unknown as Record<string, any> | null),
  )
  const [autoClassifierKey, setAutoClassifierKey] = useState(() => stringOrDefault(persisted.autoClassifierKey, ''))
  const [formManifest, setFormManifest] = useState<KworkFormManifest | null>(() =>
    objectOrDefault(persisted.formManifest, null as unknown as KworkFormManifest | null),
  )
  const [attributeSelection, setAttributeSelection] = useState<Record<string, unknown>>(() =>
    objectOrDefault(persisted.attributeSelection, {}),
  )
  const [manifestLoading, setManifestLoading] = useState(false)
  const [manifestError, setManifestError] = useState<string | null>(null)
  const [verificationOpening, setVerificationOpening] = useState(false)
  const [fieldNotice, setFieldNotice] = useState<FieldChangeNotice | null>(() =>
    objectOrDefault(persisted.fieldNotice, null as unknown as FieldChangeNotice | null),
  )
  const [suggestingControl, setSuggestingControl] = useState<string | null>(null)
  const [classifierTrail, setClassifierTrail] = useState<KworkClassifier[]>(() =>
    arrayOrDefault<KworkClassifier>(persisted.classifierTrail, []),
  )
  const [publishConfirmation, setPublishConfirmation] = useState<Record<string, any> | null>(null)
  const [publishConfirmInput, setPublishConfirmInput] = useState('')
  const manifestTimerRef = useRef<number | undefined>()
  const manifestRequestRef = useRef(0)
  const categoryResetRef = useRef(selectedCategoryId)
  const skipInitialManifestReloadRef = useRef(
    Boolean(
      persisted.formManifest ||
        (persisted.attributeSelection && Object.keys(objectOrDefault(persisted.attributeSelection, {})).length) ||
        persisted.draftResult,
    ),
  )

  const {
    data: categoriesData,
    loading: categoriesLoading,
    error: categoriesError,
    refetch: refetchCategories,
  } = useApi(() => api.getKworkMarketCategories(), [], { categories: [] })

  const topCategories = categoriesData?.categories || []
  const flatCategories = useMemo(() => flattenCategories(topCategories), [topCategories])

  useEffect(() => {
    if (selectedRootId || selectedCategoryId || topCategories.length === 0) return
    const root = topCategories.find((item) => /разработка/i.test(item.name || '')) || topCategories[0]
    setSelectedRootId(root.id)
    setSelectedCategoryId(preferredChild(root)?.id || root.id)
  }, [selectedRootId, selectedCategoryId, topCategories])

  useEffect(() => {
    if (categoryResetRef.current === selectedCategoryId) return
    categoryResetRef.current = selectedCategoryId
    setSelectedClassifierId(undefined)
    setClassifierTrail([])
    setAttributeSelection({})
    setFormManifest(null)
    setFieldNotice(null)
  }, [selectedCategoryId])

  useEffect(() => {
    writeKworkMarketState({
      selectedRootId,
      selectedCategoryId,
      selectedClassifierId,
      includeDemand,
      includeCompetitorDetails,
      scanWantDetails,
      scanPriceRules,
      scanSellerDetails,
      scanAccountContext,
      buyerBudgetMax,
      serviceSummary,
      audience,
      price,
      workTime,
      useLlm,
      generateImage,
      autoClassifierKey,
      formManifest,
      attributeSelection,
      fieldNotice,
      classifierTrail,
      draftResult,
      publishResult,
      buyerScoutResult,
      marketScanResult,
    })
  }, [
    selectedRootId,
    selectedCategoryId,
    selectedClassifierId,
    includeDemand,
    includeCompetitorDetails,
    scanWantDetails,
    scanPriceRules,
    scanSellerDetails,
    scanAccountContext,
    buyerBudgetMax,
    serviceSummary,
    audience,
    price,
    workTime,
    useLlm,
    generateImage,
    autoClassifierKey,
    formManifest,
    attributeSelection,
    fieldNotice,
    classifierTrail,
    draftResult,
    publishResult,
    buyerScoutResult,
    marketScanResult,
  ])

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
            include_demand: false,
            include_competitor_details: includeCompetitorDetails,
            competitor_detail_limit: includeCompetitorDetails ? 6 : 0,
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
    [
      selectedCategoryId,
      selectedClassifierId,
      includeCompetitorDetails,
      attributeSelectionSignature,
      manifestControlsSignature,
    ],
    objectOrDefault(persisted.metrics, undefined as unknown as KworkMarketMetrics | undefined),
  )

  useEffect(() => {
    if (!metrics) return
    writeKworkMarketState({
      ...readKworkMarketState(),
      metrics,
    })
  }, [metrics])

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
  const marketScanSeeds = useMemo(
    () =>
      buildMarketScanSeeds(
        selectedCategoryId
          ? {
              name:
                selectedClassifier?.name ||
                classifierTrail[classifierTrail.length - 1]?.name ||
                selectedCategory?.name ||
                `Category ${selectedCategoryId}`,
              category_id: selectedCategoryId,
              ...(selectedClassifierId ? { classifier_id: selectedClassifierId } : {}),
            }
          : undefined,
      ),
    [selectedCategoryId, selectedCategory?.name, selectedClassifierId, selectedClassifier?.name, classifierTrail],
  )
  const demand = demandView(visibleMetrics?.demand, includeDemand)
  const competitors = visibleMetrics?.competitors || []
  const marketInsights = visibleMetrics?.market_insights
  const marketInsightBullets = (marketInsights?.bullets || []).filter(Boolean).slice(0, 6)
  const marketInsightRecommendations = (marketInsights?.recommendations || []).filter(Boolean).slice(0, 5)
  const marketInsightClassifiers = useMemo(
    () =>
      ((marketInsights?.top_classifiers?.length ? marketInsights.top_classifiers : visibleMetrics?.classifiers) || [])
        .map((item) => ({
          id: String(item.id || ''),
          name: String(item.name || item.id || '').trim(),
          kworks_count: Number(item.kworks_count || 0),
        }))
        .filter((item) => item.name && item.kworks_count > 0)
        .sort((a, b) => b.kworks_count - a.kworks_count),
    [marketInsights?.top_classifiers, visibleMetrics?.classifiers],
  )
  const marketInsightTerms = useMemo(() => {
    const seen = new Set<string>()
    return [...(marketInsights?.title_terms || []), ...(marketInsights?.description_terms || [])]
      .filter((item) => {
        const term = String(item.term || '').trim().toLowerCase()
        if (!term || seen.has(term)) return false
        seen.add(term)
        return true
      })
      .slice(0, 10)
  }, [marketInsights?.title_terms, marketInsights?.description_terms])
  const wideClassifierRows = marketInsightClassifiers.slice(0, 3)
  const narrowClassifierRows = [...marketInsightClassifiers].reverse().slice(0, 3)
  const loading = categoriesLoading || attributesLoading || pricesLoading || metricsLoading
  const anyError = categoriesError || attributesError || pricesError || metricsError
  const coverVisionImagesSeen = Number(draftResult?.image?.competitor_images_seen || 0)
  const hasCoverVisionAnalysis =
    draftResult?.image?.visual_analysis_status === 'analyzed' && coverVisionImagesSeen > 0
  const coverPromptContext = draftResult?.image?.cover_prompt_context
  const coverPromptRoute =
    typeof coverPromptContext?.prompt_writer_route === 'string' ? coverPromptContext.prompt_writer_route : ''
  const publishManualVerification = manualVerificationFromPublishResult(publishResult)
  const marketOpportunityRows = useMemo(
    () => (marketScanResult?.aggregate.top_opportunities || []).filter(hasMeaningfulMarketOpportunity).slice(0, 6),
    [marketScanResult],
  )
  const marketSupplyRows = useMemo(
    () => (Array.isArray(marketScanResult?.supply) ? marketScanResult.supply : []).slice(0, 8),
    [marketScanResult],
  )
  const marketQueryDemandRows = useMemo(
    () =>
      Object.entries(objectOrDefault(marketScanResult?.query_demand, {} as Record<string, unknown>))
        .map(([query, value]) => ({ query, value: objectOrDefault(value, {} as Record<string, unknown>) }))
        .slice(0, 8),
    [marketScanResult],
  )
  const marketSellerRows = useMemo(
    () => (Array.isArray(marketScanResult?.seller_intelligence) ? marketScanResult.seller_intelligence : []).slice(0, 6),
    [marketScanResult],
  )
  const buyerSignals = useMemo(() => buyerScoutSignals(buyerScoutResult).slice(0, 5), [buyerScoutResult])
  const buyerSuggestions = useMemo(() => buyerQuerySuggestions(buyerScoutResult).slice(0, 5), [buyerScoutResult])
  const buyerSummary = useMemo(
    () => objectOrDefault(buyerScoutResult?.aggregate?.buyer_summary, {} as Record<string, unknown>),
    [buyerScoutResult],
  )
  const buyerNextActions = useMemo(
    () => (Array.isArray(buyerSummary.next_actions) ? (buyerSummary.next_actions as string[]) : []).slice(0, 5),
    [buyerSummary],
  )
  const buyerBestWindows = useMemo(
    () =>
      (Array.isArray(buyerSummary.best_windows) ? (buyerSummary.best_windows as Array<Record<string, unknown>>) : [])
        .slice(0, 6),
    [buyerSummary],
  )
  const buyerNeedsTokenMode = useMemo(() => buyerLotsNeedTokenMode(buyerScoutResult), [buyerScoutResult])

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
    if (skipInitialManifestReloadRef.current) {
      skipInitialManifestReloadRef.current = false
      return
    }
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

  const runMarketScan = async () => {
    if (marketScanLoading) return
    setMarketScanLoading(true)
    setMarketScanError(null)
    try {
      const buyerScout = await api.getKworkBuyerScout({
        max_probes: 10,
        project_page_limit: 2,
        per_probe_limit: 12,
        top_limit: 24,
        include_project_details: scanWantDetails,
        include_want_details: scanWantDetails,
        include_buyer_history: scanWantDetails,
        detail_limit: scanWantDetails ? 8 : 4,
        buyer_history_limit: scanWantDetails ? 6 : 0,
        budget_max: Math.max(0, Math.min(Number(buyerBudgetMax) || 5000, 150000)),
        include_query_suggestions: true,
        query_suggestion_limit: 5,
        write_file: true,
      })
      setBuyerScoutResult(buyerScout)

      const result = await api.getKworkMarketIntelligenceSnapshot({
        seeds: marketScanSeeds,
        max_seeds: marketScanSeeds.length,
        pages: 1,
        include_demand: true,
        include_competitor_details: includeCompetitorDetails,
        competitor_detail_limit: includeCompetitorDetails ? 6 : 0,
        include_seller_details: scanSellerDetails,
        seller_detail_limit: scanSellerDetails ? 4 : 0,
        include_want_details: scanWantDetails,
        want_detail_limit: scanWantDetails ? 4 : 0,
        include_price_rules: scanPriceRules,
        include_account_context: scanAccountContext,
        write_file: true,
      })
      setMarketScanResult(result)
    } catch (e) {
      setMarketScanError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketScanLoading(false)
    }
  }

  const loadMarketHistory = async () => {
    if (marketHistoryLoading) return
    setMarketHistoryLoading(true)
    setMarketScanError(null)
    try {
      const result = await api.getKworkMarketIntelligenceHistory(20)
      setMarketHistory(result)
    } catch (e) {
      setMarketScanError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketHistoryLoading(false)
    }
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
    description: competitorDescription(item),
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
      const nextIds = afterIds.length ? afterIds : result.selected_ids || []
      const context = [
        selectedCategory?.label ? `рубрика: ${selectedCategory.label}` : '',
        classifierTrail.length ? `срез: ${classifierTrail.map((item) => item.name).join(' / ')}` : '',
        visibleMetrics?.kworks_count ? `конкурентов в выдаче: ${formatNumber(visibleMetrics.kworks_count)}` : '',
        competitorExamples.length ? `учтено примеров: ${competitorExamples.length}` : '',
        includeDemand ? `спрос: ${demand.value}` : '',
      ].filter(Boolean)
      const nextSelection = result.selection || attributeSelection
      setAttributeSelection(nextSelection)
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
      if (selectionTouchesDynamicChildren(control, beforeIds, nextIds)) {
        await loadFormManifest(nextSelection)
      }
    } catch (e) {
      setManifestError(e instanceof Error ? e.message : String(e))
    } finally {
      setSuggestingControl(null)
    }
  }

  const createDraft = async () => {
    if (!selectedCategoryId) return
    const cleanAudience = audience.trim()
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
        audience: cleanAudience,
        price,
        work_time: workTime,
        use_llm: useLlm,
        generate_image: generateImage,
        cover_text: serviceSummary,
        cover_subtitle: cleanAudience,
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
      setPublishConfirmation({ phrase, token: preflight.token, draft: liveDraft })
      setPublishConfirmInput('')
      setDraftError(null)
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : String(e))
    } finally {
      setDraftLoading(false)
    }
  }

  const confirmPublishLive = async () => {
    if (!publishConfirmation?.draft || !publishConfirmation?.token) return
    const phrase = String(publishConfirmation.phrase || 'ОПУБЛИКОВАТЬ')
    if (publishConfirmInput.trim() !== phrase) {
      setDraftError('Фраза подтверждения не совпала.')
      return
    }
    setDraftLoading(true)
    setDraftError(null)
    try {
      const result = await api.publishKworkDraft(publishConfirmation.draft, false, {
        confirm_token: String(publishConfirmation.token),
        confirmation: phrase,
      })
      setPublishResult(result)
      setPublishConfirmation(null)
      setPublishConfirmInput('')
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

  const cancelPublishLive = () => {
    setPublishConfirmation(null)
    setPublishConfirmInput('')
    setDraftError('Публикация отменена.')
  }

  return (
    <div className="space-y-5 p-6 max-lg:p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="page-kicker">kwork рынок</div>
          <h1 className="text-xl font-semibold text-white">Кворки: категории, конкуренты, черновик</h1>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
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
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Докачивает описания конкурентов через быстрый API getKworkDetails. Включайте, когда нужны примеры для ИИ."
          >
            <input
              type="checkbox"
              checked={includeCompetitorDetails}
              onChange={(event) => setIncludeCompetitorDetails(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            описания
          </label>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Добавляет правила цен Kwork к широкому снимку рынка. Медленнее обычного scan."
          >
            <input
              type="checkbox"
              checked={scanPriceRules}
              onChange={(event) => setScanPriceRules(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            цены
          </label>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Докачивает подробности заказов с биржи Kwork. Это тяжелая опция."
          >
            <input
              type="checkbox"
              checked={scanWantDetails}
              onChange={(event) => setScanWantDetails(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            заказы
          </label>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Докачивает профили продавцов, портфолио и отзывы для глубокого анализа. Это тяжелая опция."
          >
            <input
              type="checkbox"
              checked={scanSellerDetails}
              onChange={(event) => setScanSellerDetails(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            продавцы
          </label>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Добавляет краткий контекст твоего аккаунта к анализу рынка."
          >
            <input
              type="checkbox"
              checked={scanAccountContext}
              onChange={(event) => setScanAccountContext(event.target.checked)}
              className="h-3.5 w-3.5 accent-brand-500"
            />
            мой аккаунт
          </label>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Buyer scout budget cap. For a fresh account keep it near 5000."
          >
            до ₽
            <input
              type="number"
              min={0}
              max={150000}
              step={500}
              value={buyerBudgetMax}
              onChange={(event) => setBuyerBudgetMax(Math.max(0, Math.min(Number(event.target.value) || 0, 150000)))}
              className="w-20 bg-transparent text-right text-zinc-200 outline-none"
            />
          </label>
          <button
            type="button"
            onClick={runMarketScan}
            disabled={marketScanLoading}
            className="btn btn-ghost py-1.5 text-xs"
            title="Собрать широкий снимок рынка через API и сохранить JSON в docs/kwork_market_snapshots"
          >
            {marketScanLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BarChart3 className="h-3.5 w-3.5" />}
            Скан рынка
          </button>
          <button
            type="button"
            onClick={loadMarketHistory}
            disabled={marketHistoryLoading}
            className="btn btn-ghost py-1.5 text-xs"
            title="Показать историю сканов рынка из index.jsonl"
          >
            {marketHistoryLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            История
          </button>
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

      {marketScanError && (
        <div className="flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{marketScanError}</span>
        </div>
      )}

      {buyerScoutResult && (
        <div className="rounded-md border border-brand-500/30 bg-brand-600/10 px-3 py-2 text-xs text-brand-100">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="font-medium text-white">Живые лоты покупателей</div>
              <div className="text-[11px] text-brand-100/60">
                Ранжирование по /projects и /getWantsCount: мало откликов, бюджет, история покупателя
              </div>
            </div>
            <div className="text-brand-100/60">{buyerScoutResult.generated_at}</div>
          </div>

          <div className="grid grid-cols-4 gap-2 max-lg:grid-cols-2 max-sm:grid-cols-1">
            {[
              ['проверок', formatNumber(buyerScoutResult.aggregate.probe_count), 'наборов фильтров'],
              ['лотов', formatNumber(buyerScoutResult.aggregate.unique_projects), 'уникальных заказов'],
              ['без откликов', formatNumber(buyerScoutResult.aggregate.zero_offer_count), 'самое раннее окно'],
              ['мало откликов', formatNumber(buyerScoutResult.aggregate.low_offer_count), '0..5 предложений'],
            ].map(([label, value, sub]) => (
              <div key={label} className="min-w-0 rounded-md border border-brand-500/20 bg-surface-950/35 p-2">
                <div className="mono-label truncate">{label}</div>
                <div className="mt-1 text-lg font-semibold text-white">{value}</div>
                <div className="mt-1 truncate text-[11px] text-brand-100/60">{sub}</div>
              </div>
            ))}
          </div>

          {(!!buyerNextActions.length || !!buyerBestWindows.length) && (
            <div className="mt-3 grid grid-cols-[1.1fr_1fr] gap-2 max-xl:grid-cols-1">
              {!!buyerNextActions.length && (
                <div className="rounded-md border border-brand-500/20 bg-surface-950/35 p-3">
                  <div className="mb-2 text-xs font-medium text-white">Next actions</div>
                  <div className="space-y-1.5">
                    {buyerNextActions.map((item) => (
                      <div key={item} className="flex items-start gap-2 text-[11px] leading-relaxed text-brand-100/75">
                        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-400" />
                        <span>{item}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {!!buyerBestWindows.length && (
                <div className="rounded-md border border-brand-500/20 bg-surface-950/35 p-3">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <div className="text-xs font-medium text-white">Best search windows</div>
                    <div className="text-[11px] text-brand-100/50">
                      budget {formatNumber(String(buyerSummary.budget_max || buyerBudgetMax))}
                    </div>
                  </div>
                  <div className="grid gap-1.5">
                    {buyerBestWindows.map((windowRow) => (
                      <div
                        key={`${String(windowRow.name || windowRow.query)}-${String(windowRow.categories || '')}`}
                        className="grid grid-cols-[1fr_auto_auto] items-center gap-2 rounded border border-surface-700 bg-surface-950/40 px-2 py-1.5 text-[11px]"
                      >
                        <div className="min-w-0">
                          <div className="truncate text-brand-100">{String(windowRow.name || windowRow.query || 'probe')}</div>
                          <div className="truncate text-zinc-600">
                            c={String(windowRow.categories || 'all')}
                            {windowRow.query ? ` · ${String(windowRow.query)}` : ''}
                            {windowRow.source ? ` · ${String(windowRow.source)}` : ''}
                          </div>
                        </div>
                        <div className="text-right text-white">{formatNumber(String(windowRow.count || 0))}</div>
                        <div className="text-right text-zinc-500">{formatNumber(String(windowRow.sample_count || 0))} seen</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {!!buyerSignals.length && (
            <div className="mt-3 grid grid-cols-5 gap-2 max-2xl:grid-cols-3 max-lg:grid-cols-2 max-sm:grid-cols-1">
              {buyerSignals.map((signal) => {
                const projects = Array.isArray(signal.projects) ? (signal.projects as Array<Record<string, unknown>>) : []
                const probes = Array.isArray(signal.probes) ? (signal.probes as Array<Record<string, unknown>>) : []
                return (
                  <div
                    key={String(signal.kind || signal.label)}
                    className="min-w-0 rounded-md border border-brand-500/20 bg-surface-950/35 p-2"
                  >
                    <div className="mono-label truncate">{String(signal.label || signal.kind || 'сигнал')}</div>
                    <div className="mt-1 text-lg font-semibold text-white">{formatNumber(String(signal.value ?? 0))}</div>
                    <div className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-brand-100/65">
                      {String(signal.detail || '')}
                    </div>
                    {!!projects.length && (
                      <div className="mt-2 space-y-1">
                        {projects.slice(0, 2).map((project) => (
                          <div key={String(project.id || project.title)} className="truncate text-[11px] text-zinc-400">
                            {String(project.title || project.id || '-')}
                          </div>
                        ))}
                      </div>
                    )}
                    {!!probes.length && (
                      <div className="mt-2 space-y-1">
                        {probes.slice(0, 2).map((probe) => (
                          <div key={String(probe.name || probe.query)} className="truncate text-[11px] text-zinc-400">
                            {String(probe.name || probe.query || 'probe')} · {formatNumber(String(probe.count || 0))}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {!!buyerSuggestions.length && (
            <div className="mt-3 rounded-md border border-brand-500/20 bg-surface-950/35 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-white">Buyer keyword hints</div>
                <div className="text-[11px] text-brand-100/60">/want-search/suggest</div>
              </div>
              <div className="grid grid-cols-5 gap-2 max-2xl:grid-cols-3 max-lg:grid-cols-2 max-sm:grid-cols-1">
                {buyerSuggestions.map((group) => {
                  const suggestions = Array.isArray(group.suggestions)
                    ? (group.suggestions as Array<Record<string, unknown>>)
                    : []
                  return (
                    <div key={String(group.query)} className="min-w-0 rounded border border-surface-700 bg-surface-950/40 p-2">
                      <div className="mono-label truncate">{String(group.query || 'query')}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {suggestions.slice(0, 5).map((item) => (
                          <span
                            key={String(item.suggestion || item.excerpt)}
                            className="rounded border border-brand-500/20 bg-brand-600/10 px-1.5 py-0.5 text-[11px] text-brand-100"
                          >
                            {String(item.suggestion || item.excerpt || '')}
                          </span>
                        ))}
                        {!suggestions.length && <span className="text-[11px] text-zinc-600">нет подсказок</span>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {!!buyerScoutResult.top?.length && (
            <div className="mt-3 grid grid-cols-2 gap-2 max-xl:grid-cols-1">
              {buyerScoutResult.top.slice(0, 8).map((project) => {
                const description = buyerProjectDescription(project)
                const views = buyerProjectViews(project)
                const history = buyerProjectHistory(project)
                return (
                  <article key={String(project.id)} className="min-w-0 rounded-md border border-brand-500/20 bg-surface-950/35 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="line-clamp-2 text-sm font-semibold text-white">
                          {String(project.title || `#${String(project.id || '-')}`)}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-brand-100/70">
                          <span>балл {formatNumber(String(project.score || 0))}</span>
                          <span>откликов {formatNumber(String(project.offers ?? 0))}</span>
                          <span>бюджет {buyerProjectBudget(project)}</span>
                          {views !== null && <span>просмотров {formatNumber(views)}</span>}
                          {Boolean(project.matched_probe) && <span>{String(project.matched_probe)}</span>}
                        </div>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        {history.url && (
                          <a
                            href={history.url}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-md border border-surface-600 px-2 py-1 text-[11px] text-zinc-400 transition hover:text-white"
                            title="История лотов покупателя"
                          >
                            история
                          </a>
                        )}
                        {Boolean(project.id) && (
                          <a
                            href={`https://kwork.ru/new_offer?project=${encodeURIComponent(String(project.id))}`}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-md border border-surface-600 p-1 text-zinc-400 transition hover:text-white"
                            title="Предложить услугу"
                          >
                            <ExternalLink className="h-3.5 w-3.5" />
                          </a>
                        )}
                      </div>
                    </div>
                    {(history.username || history.total !== null || history.active !== null || history.hired !== null) && (
                      <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-zinc-400">
                        {history.username && <span className="text-brand-100">покупатель {history.username}</span>}
                        {history.total !== null && <span>лотов всего {formatNumber(history.total)}</span>}
                        {history.active !== null && <span>активных {formatNumber(history.active)}</span>}
                        {history.hired !== null && <span>нанимает {formatNumber(history.hired)}%</span>}
                      </div>
                    )}
                    {!!history.projects.length && (
                      <div className="mt-2 space-y-1 rounded border border-surface-700/70 bg-surface-950/35 p-2">
                        <div className="text-[10px] uppercase tracking-[0.18em] text-zinc-600">ещё лоты покупателя</div>
                        {history.projects.map((historyProject) => (
                          <div key={String(historyProject.id)} className="flex items-center justify-between gap-2 text-[11px] text-zinc-400">
                            <span className="min-w-0 truncate">{String(historyProject.title || `#${historyProject.id || '-'}`)}</span>
                            <span className="shrink-0 text-zinc-500">
                              {formatNumber(String(historyProject.offers ?? 0))} откл.
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {description && <p className="mt-2 line-clamp-4 text-[11px] leading-relaxed text-zinc-400">{description}</p>}
                  </article>
                )
              })}
            </div>
          )}

          {!!buyerScoutResult.probes?.length && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {buyerScoutResult.probes.slice(0, 10).map((probe) => (
                <span
                  key={String(probe.name || probe.query || JSON.stringify(probe.filters || {}))}
                  className="rounded border border-brand-500/25 bg-surface-950/35 px-2 py-1 text-[11px] text-brand-100"
                  title={buyerProbeSummary(probe)}
                >
                  {String(probe.name || probe.query || 'probe')}: {formatNumber(String(probe.count || 0))}
                </span>
              ))}
            </div>
          )}

          {buyerNeedsTokenMode && (
            <div className="mt-2 rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-100">
              Buyer lots need token-mode auth. Current Session Hub is cookie-only, so PSR skipped slow /projects calls and kept keyword hints.
            </div>
          )}

          {!!buyerScoutResult.endpoint_errors?.length && !buyerNeedsTokenMode && (
            <div className="mt-2 rounded border border-red-500/25 bg-red-500/10 px-2 py-1 text-[11px] text-red-200">
              Buyer API errors: {buyerScoutResult.endpoint_errors.length}. Details are saved in JSON.
            </div>
          )}

          {buyerScoutResult.file_path && (
            <div className="mt-2 break-all rounded border border-brand-500/20 bg-surface-950/40 px-2 py-1 text-[11px] text-brand-100/70">
              snapshot: {buyerScoutResult.file_path}
            </div>
          )}
        </div>
      )}

      {marketScanResult && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-100">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium text-white">Снимок рынка</div>
            <div className="text-emerald-200/70">{marketScanResult.generated_at}</div>
          </div>
          <div className="grid grid-cols-4 gap-2 max-lg:grid-cols-2 max-sm:grid-cols-1">
            {[
              ['срезы', formatNumber(marketScanResult.aggregate.seed_count), 'сколько рубрик проверено'],
              ['кворки', formatNumber(marketScanResult.aggregate.cards_seen), 'карточек просмотрено'],
              ['продавцы', formatNumber(marketScanResult.aggregate.unique_sellers_seen), 'уникальных исполнителей'],
              ['профили', formatNumber(marketScanResult.aggregate.seller_profiles_collected), 'профилей докачано'],
            ].map(([label, value, sub]) => (
              <div key={label} className="min-w-0 rounded-md border border-emerald-500/20 bg-surface-950/35 p-2">
                <div className="mono-label truncate">{label}</div>
                <div className="mt-1 text-lg font-semibold text-white">{value}</div>
                <div className="mt-1 truncate text-[11px] text-emerald-200/60">{sub}</div>
              </div>
            ))}
          </div>
          {!!marketSupplyRows.length && (
            <div className="mt-3 rounded-md border border-emerald-500/20 bg-surface-950/30 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-white">Supply snapshot</div>
                <div className="text-[11px] text-emerald-200/60">/kworks by seed</div>
              </div>
              <div className="grid grid-cols-2 gap-2 max-xl:grid-cols-1">
                {marketSupplyRows.map((row, index) => {
                  const seed = objectOrDefault(row.seed, {} as Record<string, unknown>)
                  const cards = Array.isArray(row.cards) ? (row.cards as Array<Record<string, unknown>>) : []
                  const demandRow = objectOrDefault(row.demand, {} as Record<string, unknown>)
                  const classifiers = Array.isArray(row.classifiers) ? (row.classifiers as Array<Record<string, unknown>>) : []
                  const priceText = priceRulesSummary(row.price_rules)
                  return (
                    <div
                      key={`${String(seed.category_id || index)}-${String(seed.classifier_id || '')}`}
                      className="min-w-0 rounded border border-surface-700 bg-surface-950/35 p-2"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-xs font-medium text-emerald-50">{snapshotSeedName(seed)}</div>
                          <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-emerald-100/65">
                            <span>c={String(seed.category_id || '-')}</span>
                            {seed.classifier_id ? <span>classifier={String(seed.classifier_id)}</span> : null}
                            <span>kworks {formatNumber(String(row.kworks_count || 0))}</span>
                            <span>wants {formatNumber(snapshotDemandCount(demandRow))}</span>
                            <span>sample {formatNumber(cards.length)}</span>
                          </div>
                        </div>
                        {row.timings_ms && (
                          <div className="shrink-0 text-[11px] text-zinc-500">
                            {formatNumber(String(objectOrDefault(row.timings_ms, {} as Record<string, unknown>).total || 0))}ms
                          </div>
                        )}
                      </div>
                      {!!classifiers.length && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {classifiers.slice(0, 5).map((classifier) => (
                            <span key={String(classifier.id || classifier.name)} className="rounded border border-surface-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                              {String(classifier.name || classifier.id)}: {formatNumber(String(classifier.kworks_count || 0))}
                            </span>
                          ))}
                        </div>
                      )}
                      {!!cards.length && (
                        <div className="mt-2 space-y-1">
                          {cards.slice(0, 3).map((card) => (
                            <div key={String(card.id || card.title)} className="flex items-center justify-between gap-2 text-[11px] text-zinc-400">
                              <span className="min-w-0 truncate">{String(card.title || card.id || '-')}</span>
                              <span className="shrink-0 text-emerald-100/70">{formatPrice(card.price as number | string | null)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                      {(snapshotSampleCount(demandRow) > 0 || priceText) && (
                        <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-zinc-500">
                          {snapshotSampleCount(demandRow) > 0 && <span>demand sample {formatNumber(snapshotSampleCount(demandRow))}</span>}
                          {priceText && <span>price rules {priceText}</span>}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          {!!marketQueryDemandRows.length && (
            <div className="mt-3 rounded-md border border-sky-500/20 bg-sky-500/10 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-white">Demand snapshot</div>
                <div className="text-[11px] text-sky-100/60">/projects and /getWantsCount</div>
              </div>
              <div className="grid grid-cols-4 gap-2 max-xl:grid-cols-2 max-sm:grid-cols-1">
                {marketQueryDemandRows.map(({ query, value }) => {
                  const sample = Array.isArray(value.sample) ? (value.sample as Array<Record<string, unknown>>) : []
                  return (
                    <div key={query} className="min-w-0 rounded border border-sky-500/20 bg-surface-950/35 p-2">
                      <div className="truncate text-xs font-medium text-sky-50">{query || 'all'}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-sky-100/70">
                        <span>wants {formatNumber(snapshotDemandCount(value))}</span>
                        <span>sample {formatNumber(snapshotSampleCount(value))}</span>
                        <span>{String(value.status || 'unknown')}</span>
                      </div>
                      {!!sample.length && (
                        <div className="mt-2 space-y-1">
                          {sample.slice(0, 2).map((project) => (
                            <div key={String(project.id || project.title)} className="truncate text-[11px] text-zinc-400">
                              {String(project.title || project.id || '-')} · {formatNumber(String(project.offers ?? 0))} откл.
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          {!!marketOpportunityRows.length && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {marketOpportunityRows.map((row) => (
                <span
                  key={`${String(row.seed_name || row.category_id)}-${String(row.classifier_id || '')}`}
                  className="rounded border border-emerald-500/25 bg-surface-950/35 px-2 py-1 text-[11px] text-emerald-100"
                  title={`спрос=${String(row.demand_wants_count ?? '-')} конкуренты=${String(row.supply_kworks_count ?? '-')}`}
                >
                  {marketOpportunityLabel(row)}
                </span>
              ))}
            </div>
          )}
          {!!marketSellerRows.length && (
            <div className="mt-3 rounded-md border border-purple-500/20 bg-purple-500/10 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-white">Seller intelligence</div>
                <div className="text-[11px] text-purple-100/60">profile, portfolio, reviews</div>
              </div>
              <div className="grid grid-cols-2 gap-2 max-xl:grid-cols-1">
                {marketSellerRows.map((seller) => {
                  const portfolio = objectOrDefault(seller.portfolio, {} as Record<string, unknown>)
                  const reviews = objectOrDefault(seller.reviews, {} as Record<string, unknown>)
                  const allReviews = objectOrDefault(reviews.all, {} as Record<string, unknown>)
                  const negativeReviews = objectOrDefault(reviews.negative, {} as Record<string, unknown>)
                  const sampleKworks = Array.isArray(seller.sample_kworks) ? (seller.sample_kworks as Array<Record<string, unknown>>) : []
                  return (
                    <div key={String(seller.username || seller.id)} className="min-w-0 rounded border border-purple-500/20 bg-surface-950/35 p-2">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-xs font-medium text-purple-50">{String(seller.username || seller.display_name || seller.id || '-')}</div>
                          <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-purple-100/70">
                            {seller.level ? <span>{String(seller.level)}</span> : null}
                            {seller.rating ? <span>rating {String(seller.rating)}</span> : null}
                            {seller.reviews_count ? <span>reviews {formatNumber(String(seller.reviews_count))}</span> : null}
                            {seller.active_kworks_count ? <span>active {formatNumber(String(seller.active_kworks_count))}</span> : null}
                          </div>
                        </div>
                        <span className="shrink-0 text-[11px] text-zinc-500">{String(seller.status || '')}</span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-zinc-400">
                        <span>portfolio {formatNumber(String(portfolio.total ?? portfolio.sample_count ?? 0))}</span>
                        <span>reviews {formatNumber(String(allReviews.total ?? allReviews.sample_count ?? 0))}</span>
                        <span>negative {formatNumber(String(negativeReviews.total ?? negativeReviews.sample_count ?? 0))}</span>
                      </div>
                      {!!sampleKworks.length && (
                        <div className="mt-2 space-y-1">
                          {sampleKworks.slice(0, 2).map((kwork) => (
                            <div key={String(kwork.id || kwork.title)} className="truncate text-[11px] text-zinc-500">
                              {String(kwork.title || kwork.id || '-')}
                            </div>
                          ))}
                        </div>
                      )}
                      {Array.isArray(seller.errors) && seller.errors.length > 0 && (
                        <div className="mt-2 line-clamp-2 text-[11px] text-amber-200/80">{String(seller.errors[0])}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          {Array.isArray(marketScanResult.aggregate.top_sellers) && marketScanResult.aggregate.top_sellers.length > 0 && (
            <div className="mt-2 rounded border border-surface-700 bg-surface-950/30 px-2 py-1 text-[11px] text-zinc-500">
              <span className="mr-2 text-zinc-400">sample seller concentration:</span>
              {marketScanResult.aggregate.top_sellers.slice(0, 8).map((item) => {
                const tuple = Array.isArray(item) ? item : []
                return (
                  <span key={String(tuple[0])} className="mr-2 inline-block">
                    {String(tuple[0] || '-')}: {formatNumber(String(tuple[1] || 0))}
                  </span>
                )
              })}
            </div>
          )}
          {marketScanResult.account_context && marketScanResult.account_context.status !== 'skipped' && (
            <div className="mt-2 rounded border border-brand-500/25 bg-brand-600/10 px-2 py-1 text-[11px] text-brand-100">
              <span className="text-brand-200/70">аккаунт:</span>{' '}
              {String(marketScanResult.account_context.username || marketScanResult.account_context.status || 'unknown')}
              {marketScanResult.account_context.active_kworks_count !== undefined
                ? ` · активных ${String(marketScanResult.account_context.active_kworks_count)}`
                : ''}
              {marketScanResult.account_context.offers_count !== undefined
                ? ` · откликов ${String(marketScanResult.account_context.offers_count)}`
                : ''}
            </div>
          )}
          {(marketScanResult.file_path || marketScanResult.latest_path || marketScanResult.index_path) && (
            <div className="mt-2 space-y-1 rounded border border-emerald-500/20 bg-surface-950/40 px-2 py-1 text-[11px] text-emerald-200/80">
              {[
                ['снимок', marketScanResult.file_path],
                ['последний', marketScanResult.latest_path],
                ['история', marketScanResult.index_path],
              ]
                .filter(([, value]) => value)
                .map(([label, value]) => (
                  <div key={label} className="break-all">
                    <span className="text-emerald-300/80">{label}:</span> {value}
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {marketHistory && (
        <div className="rounded-md border border-brand-500/30 bg-brand-600/10 px-3 py-2 text-xs text-brand-100">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium text-white">История сканов</div>
            <div className="text-brand-100/60">
              {marketHistory.entry_count} записей · {String(marketHistory.latest?.generated_at || 'нет последнего')}
            </div>
          </div>
          <div className="grid gap-1.5">
            {marketHistory.entries.slice(-5).reverse().map((entry, index) => {
              const aggregate = (entry.aggregate || {}) as Record<string, unknown>
              const top = Array.isArray(entry.top_opportunities) ? (entry.top_opportunities[0] as Record<string, unknown>) : {}
              return (
                <div
                  key={`${String(entry.generated_at || index)}-${String(entry.file_path || '')}`}
                  className="grid grid-cols-[170px_1fr] gap-2 rounded border border-brand-500/20 bg-surface-950/35 px-2 py-1.5 max-md:grid-cols-1"
                >
                  <div className="min-w-0 truncate text-brand-100/70">{String(entry.generated_at || '-')}</div>
                  <div className="min-w-0 truncate text-brand-50">
                    кворки {formatNumber(Number(aggregate.cards_seen || 0))} · продавцы{' '}
                    {formatNumber(Number(aggregate.unique_sellers_seen || 0))} · лучший срез{' '}
                    {String(top.seed_name || top.category_id || '-')}:{' '}
                    {String(top.opportunity_score ?? top.demand_per_1000_kworks ?? '-')}
                  </div>
                </div>
              )
            })}
          </div>
          <div className="mt-2 break-all rounded border border-brand-500/20 bg-surface-950/35 px-2 py-1 text-[11px] text-brand-100/70">
            {marketHistory.index_path}
          </div>
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
                                disabled={!!suggestingControl}
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
                            <span>{controlTypeLabel(control)}</span>
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
                                  const beforeIds = selectedOptionIds(attributeSelection, control)
                                  const next = setSelectionValue(
                                    attributeSelection,
                                    control,
                                    option.id,
                                    event.target.checked,
                                  )
                                  const afterIds = selectedOptionIds(next, control)
                                  setAttributeSelection(next)
                                  if (selectionTouchesDynamicChildren(control, beforeIds, afterIds)) {
                                    scheduleFormManifest(next, selectedClassifierId)
                                  }
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

            {marketInsights && (
              <div className="grid grid-cols-[1.15fr_0.85fr] gap-3 max-xl:grid-cols-1">
                <div className="rounded-md border border-brand-500/25 bg-brand-600/10 p-3">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 text-sm font-medium text-white">
                      <BarChart3 className="h-4 w-4 text-brand-300" />
                      Что видно по рынку
                    </div>
                    <div className="text-[11px] text-brand-100/60">
                      выборка {formatNumber(marketInsights.sample_size)} из {formatNumber(marketInsights.kworks_count || visibleMetrics?.kworks_count)}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-2 max-md:grid-cols-1">
                    <div className="rounded-md border border-surface-700 bg-surface-950/35 p-2">
                      <div className="mono-label">цена в топе</div>
                      <div className="mt-1 text-sm font-semibold text-white">
                        {formatPrice(marketInsights.price?.min)} - {formatPrice(marketInsights.price?.max)}
                      </div>
                      <div className="mt-1 text-[11px] text-zinc-500">медиана {formatPrice(marketInsights.price?.median)}</div>
                    </div>
                    <div className="rounded-md border border-surface-700 bg-surface-950/35 p-2">
                      <div className="mono-label">доверие</div>
                      <div className="mt-1 text-sm font-semibold text-white">
                        {formatNumber(marketInsights.trust?.reviews_100_plus)}/{formatNumber(marketInsights.trust?.sample_size)}
                      </div>
                      <div className="mt-1 text-[11px] text-zinc-500">карточек с 100+ отзывами</div>
                    </div>
                    <div className="rounded-md border border-surface-700 bg-surface-950/35 p-2">
                      <div className="mono-label">концентрация</div>
                      <div className="mt-1 text-sm font-semibold text-white">
                        {formatPercent(marketInsights.concentration?.repeat_share)}
                      </div>
                      <div className="mt-1 text-[11px] text-zinc-500">мест у повторяющихся продавцов</div>
                    </div>
                  </div>
                  {!!marketInsightBullets.length && (
                    <div className="mt-3 grid gap-1.5 text-xs text-zinc-300">
                      {marketInsightBullets.map((item) => (
                        <div key={item} className="rounded border border-brand-500/15 bg-surface-950/25 px-2 py-1.5">
                          {item}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="rounded-md border border-emerald-500/25 bg-emerald-500/10 p-3">
                  <div className="mb-3 flex items-center gap-2 text-sm font-medium text-white">
                    <Sparkles className="h-4 w-4 text-emerald-300" />
                    Что делать с кворком
                  </div>
                  {!!marketInsightRecommendations.length && (
                    <div className="grid gap-1.5 text-xs text-emerald-50/90">
                      {marketInsightRecommendations.map((item) => (
                        <div key={item} className="rounded border border-emerald-500/15 bg-surface-950/25 px-2 py-1.5">
                          {item}
                        </div>
                      ))}
                    </div>
                  )}
                  {!!marketInsightTerms.length && (
                    <div className="mt-3">
                      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-emerald-100/70">
                        <Tags className="h-3.5 w-3.5" />
                        частые слова
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {marketInsightTerms.map((item) => (
                          <span key={String(item.term)} className="rounded border border-emerald-500/20 bg-surface-950/35 px-2 py-1 text-[11px] text-emerald-100">
                            {marketTermLabel(item)}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                  {!!marketInsightClassifiers.length && (
                    <div className="mt-3 grid gap-2 text-[11px] text-zinc-400">
                      <div>
                        <div className="mb-1 text-zinc-500">крупные срезы</div>
                        <div className="flex flex-wrap gap-1.5">
                          {wideClassifierRows.map((item) => (
                            <span key={`wide-${item.id}`} className="rounded border border-surface-700 bg-surface-950/30 px-2 py-1">
                              {item.name}: {formatNumber(item.kworks_count)}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div>
                        <div className="mb-1 text-zinc-500">узкие срезы из ответа API</div>
                        <div className="flex flex-wrap gap-1.5">
                          {narrowClassifierRows.map((item) => (
                            <span key={`narrow-${item.id}`} className="rounded border border-surface-700 bg-surface-950/30 px-2 py-1">
                              {item.name}: {formatNumber(item.kworks_count)}
                            </span>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

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
                    Для кого, если нужно
                    <input
                      value={audience}
                      onChange={(event) => setAudience(event.target.value)}
                      className="input mt-1 w-full"
                      placeholder="можно оставить пустым"
                    />
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
                                    disabled={!!suggestingControl}
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
                                <span>{controlTypeLabel(control)}</span>
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
                                          const beforeIds = selectedOptionIds(attributeSelection, control)
                                          const next = setSelectionValue(
                                            attributeSelection,
                                            control,
                                            option.id,
                                            event.target.checked,
                                          )
                                          const afterIds = selectedOptionIds(next, control)
                                          setAttributeSelection(next)
                                          if (selectionTouchesDynamicChildren(control, beforeIds, afterIds)) {
                                            scheduleFormManifest(next, selectedClassifierId)
                                          }
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
                  {publishConfirmation && (
                    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                      <div className="font-medium text-white">Подтвердить публикацию на Kwork</div>
                      <div className="mt-1 text-amber-100/75">
                        Введите фразу <span className="font-semibold text-amber-50">{String(publishConfirmation.phrase)}</span>,
                        чтобы отправить кворк в Kwork.
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <input
                          value={publishConfirmInput}
                          onChange={(event) => setPublishConfirmInput(event.target.value)}
                          className="input min-w-[220px] flex-1"
                          placeholder={String(publishConfirmation.phrase)}
                        />
                        <button onClick={confirmPublishLive} disabled={draftLoading} className="btn btn-primary py-1.5 text-xs">
                          {draftLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                          Отправить
                        </button>
                        <button onClick={cancelPublishLive} disabled={draftLoading} className="btn btn-ghost py-1.5 text-xs">
                          Отмена
                        </button>
                      </div>
                    </div>
                  )}
                  <div className="flex gap-2">
                    <button onClick={createDraft} disabled={!selectedCategoryId || draftLoading} className="btn btn-primary">
                      {draftLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
                      Сгенерировать
                    </button>
                    <button onClick={dryRunPublish} disabled={!draftResult?.draft || draftLoading} className="btn btn-ghost">
                      Проверить
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
                          {draftResult.image.prompt_source ? ` · промпт: ${draftResult.image.prompt_source}` : ''}
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
                            автор промпта: {coverPromptRoute}
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
                          <summary className="cursor-pointer text-zinc-400">промпт обложки</summary>
                          <div className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap">{draftResult.image.prompt}</div>
                        </details>
                      )}
                      {coverPromptContext && (
                        <details className="rounded-md border border-surface-700 bg-surface-950/60 p-3 text-xs text-zinc-500">
                          <summary className="cursor-pointer text-zinc-400">диагностика обложки</summary>
                          <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] max-sm:grid-cols-1">
                            <div>маршрут: {coverPromptRoute || '-'}</div>
                            <div>картинок в промпте: {formatNumber(coverContextNumber(coverPromptContext, 'prompt_images_sent'))}</div>
                            <div>конкурентов отправлено: {formatNumber(coverContextNumber(coverPromptContext, 'competitor_images_sent'))}</div>
                            <div>истории отправлено: {formatNumber(coverContextNumber(coverPromptContext, 'history_images_sent'))}</div>
                            <div>истории найдено: {formatNumber(coverContextNumber(coverPromptContext, 'recent_history_count'))}</div>
                            <div>метаданные: {draftResult.image.sidecar_path || '-'}</div>
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
                      <div className="mono-label">заголовок</div>
                      <div className="mt-1 text-base font-medium text-white">{draftResult.draft.title}</div>
                    </div>
                    <div>
                      <div className="mono-label">описание</div>
                      <div className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap text-zinc-300">
                        {draftResult.draft.description}
                      </div>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-zinc-500">
                      <span>цена: {formatPrice(draftResult.draft.price)}</span>
                      <span>срок: {draftResult.draft.work_time} дн.</span>
                      <span>рубрика: {draftResult.draft.category_id}</span>
                    </div>
                    {publishResult?.payload && (
                      <details className="rounded-md border border-surface-700 bg-surface-950/60 p-3">
                        <summary className="cursor-pointer text-zinc-400">
                          {publishResult.dry_run ? 'проверочный payload' : publishResult.ok ? 'опубликовано' : 'публикация не прошла'}
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
