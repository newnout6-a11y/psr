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
  type KworkMarketAssistantResponse,
  type KworkMarketMetrics,
  type KworkPriceRules,
  type KworkSupplyScan,
  type KworkSupplyRequestDiagnostic,
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

const KWORK_MARKET_STATE_KEY = 'psr:kwork-market:v5'
const LEGACY_REQUEST_DIAGNOSTIC_LIMIT = 24

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
    supplyScanResult: compactSupplyScanResult(state.supplyScanResult),
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
          supplyScanResult: null,
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

function compactSupplyScanResult(value: unknown) {
  if (!value || typeof value !== 'object') return null
  const result = value as Record<string, any>
  return {
    source: result.source,
    generated_at: result.generated_at,
    transport: result.transport,
    scope: result.scope,
    coverage: result.coverage,
    sample: result.sample,
    slices: Array.isArray(result.slices) ? result.slices.slice(0, 48) : [],
    raw_listings: Array.isArray(result.raw_listings) ? result.raw_listings.slice(0, 12) : [],
    requests: Array.isArray(result.requests) ? result.requests.slice(-LEGACY_REQUEST_DIAGNOSTIC_LIMIT) : [],
    analysis: result.analysis,
    assistant_context_id: result.assistant_context_id,
    file_path: result.file_path,
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

const CP1251_EXTRA_BYTES: Record<string, number> = {
  Ђ: 0x80,
  Ѓ: 0x81,
  '‚': 0x82,
  ѓ: 0x83,
  '„': 0x84,
  '…': 0x85,
  '†': 0x86,
  '‡': 0x87,
  '€': 0x88,
  '‰': 0x89,
  Љ: 0x8a,
  '‹': 0x8b,
  Њ: 0x8c,
  Ќ: 0x8d,
  Ћ: 0x8e,
  Џ: 0x8f,
  ђ: 0x90,
  '‘': 0x91,
  '’': 0x92,
  '“': 0x93,
  '”': 0x94,
  '•': 0x95,
  '–': 0x96,
  '—': 0x97,
  '™': 0x99,
  љ: 0x9a,
  '›': 0x9b,
  њ: 0x9c,
  ќ: 0x9d,
  ћ: 0x9e,
  џ: 0x9f,
  '\u00a0': 0xa0,
  Ў: 0xa1,
  ў: 0xa2,
  Ј: 0xa3,
  '¤': 0xa4,
  Ґ: 0xa5,
  '¦': 0xa6,
  '§': 0xa7,
  Ё: 0xa8,
  '©': 0xa9,
  Є: 0xaa,
  '«': 0xab,
  '¬': 0xac,
  '\u00ad': 0xad,
  '®': 0xae,
  Ї: 0xaf,
  '°': 0xb0,
  '±': 0xb1,
  І: 0xb2,
  і: 0xb3,
  ґ: 0xb4,
  µ: 0xb5,
  '¶': 0xb6,
  '·': 0xb7,
  ё: 0xb8,
  '№': 0xb9,
  є: 0xba,
  '»': 0xbb,
  ј: 0xbc,
  Ѕ: 0xbd,
  ѕ: 0xbe,
  ї: 0xbf,
}

const MOJIBAKE_MARKERS = [
  'Р’',
  'Рџ',
  'Р ',
  'РЎ',
  'Р°',
  'Р±',
  'Рµ',
  'Рє',
  'Р»',
  'РЅ',
  'Рѕ',
  'Рґ',
  'СЃ',
  'С‚',
  'СЊ',
  'С‹',
  'СЏ',
  'С‡',
  'С†',
  'С€',
  'С‰',
  'в‚Ѕ',
  'Ð',
  'Ñ',
]

function cp1251ByteForChar(char: string) {
  const code = char.charCodeAt(0)
  if (code <= 0x7f) return code
  if (code >= 0x0410 && code <= 0x044f) return code - 0x0410 + 0xc0
  return CP1251_EXTRA_BYTES[char]
}

function repairMojibake(value: string) {
  if (!value || !MOJIBAKE_MARKERS.some((marker) => value.includes(marker)) || typeof TextDecoder === 'undefined') return value
  const bytes: number[] = []
  for (const char of value) {
    const byte = cp1251ByteForChar(char)
    if (byte === undefined) return value
    bytes.push(byte)
  }
  try {
    const decoded = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes))
    return decoded && !MOJIBAKE_MARKERS.some((marker) => decoded.includes(marker)) ? decoded : value
  } catch {
    return value
  }
}

function uiText(value: unknown, fallback = '') {
  if (typeof value === 'string') return repairMojibake(value).trim()
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return String(value)
  return fallback
}

function hasStructuredValue(value: unknown, depth = 0): boolean {
  if (value === null || value === undefined || value === '') return false
  if (typeof value === 'string') return uiText(value).length > 0
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return true
  if (typeof value !== 'object') return false
  if (depth >= 4) return true
  if (Array.isArray(value)) return value.some((item) => hasStructuredValue(item, depth + 1))
  return Object.values(value).some((item) => hasStructuredValue(item, depth + 1))
}

function structuredLabel(value: string) {
  return repairMojibake(value.replace(/_/g, ' ')).trim()
}

function StructuredValue({ value, fallback = '—', depth = 0 }: { value: unknown; fallback?: string; depth?: number }) {
  if (!hasStructuredValue(value, depth)) return <span>{fallback}</span>
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return <>{uiText(value, fallback)}</>
  }
  if (depth >= 4) return <span>вложенные данные</span>
  if (Array.isArray(value)) {
    return (
      <ul className="space-y-1">
        {value
          .filter((item) => hasStructuredValue(item, depth + 1))
          .map((item, index) => (
            <li key={index} className="border-l border-surface-700 pl-2">
              <StructuredValue value={item} depth={depth + 1} />
            </li>
          ))}
      </ul>
    )
  }
  const entries = Object.entries(value as Record<string, unknown>).filter(([, item]) => hasStructuredValue(item, depth + 1))
  return (
    <dl className="space-y-1">
      {entries.map(([key, item]) => (
        <div key={key} className="grid grid-cols-[minmax(88px,0.35fr)_minmax(0,1fr)] gap-2 max-sm:grid-cols-1 max-sm:gap-0.5">
          <dt className="text-zinc-500">{structuredLabel(key)}</dt>
          <dd className="min-w-0 text-zinc-300">
            <StructuredValue value={item} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function humanProbeName(value: unknown) {
  return uiText(value)
}

function statusLabel(value: unknown) {
  const raw = uiText(value || '')
  const labels: Record<string, string> = {
    ok: 'готово',
    empty: 'пусто',
    skipped: 'пропущено',
    error: 'ошибка',
    timeout: 'тайм-аут',
    unknown: 'неизвестно',
    partial: 'частично',
    http_error: 'ошибка HTTP',
  }
  return labels[raw] || raw
}

function humanizeInlineIds(value: unknown) {
  return uiText(value)
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

function workingRunStatus(coverage: KworkSupplyScan['coverage']) {
  if (coverage.stopped_after_protection_signal) return 'остановлен по сигналу защиты'
  if (coverage.aborted_after_upstream_errors) return 'остановлен после ошибок API'
  if (coverage.minimum_cards_target_met) return 'цель рабочей выборки достигнута'
  return 'рабочая выборка завершена'
}

function workingRunDetail(coverage: KworkSupplyScan['coverage']) {
  if (coverage.stopped_after_protection_signal || coverage.aborted_after_upstream_errors) return 'продолжение требует проверки источника'
  if (coverage.minimum_cards_target_met) return 'минимальный объём фактической выборки получен'
  return 'для большей выборки углубите анализ'
}

function diagnosticText(...values: unknown[]) {
  for (const value of values) {
    const text = uiText(value)
    if (text) return text
  }
  return '—'
}

function diagnosticCursor(value: unknown, ...fallbacks: unknown[]) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const cursor = value as Record<string, unknown>
    const page = finiteNumber(cursor.page)
    const kind = uiText(cursor.kind)
    if (page !== null) return kind ? `${kind} ${formatNumber(page)}` : formatNumber(page)
  }
  return diagnosticText(value, ...fallbacks)
}

function diagnosticNumber(...values: unknown[]) {
  for (const value of values) {
    const number = finiteNumber(value)
    if (number !== null) return formatNumber(number)
  }
  return '—'
}

function diagnosticStatus(diagnostic: KworkSupplyRequestDiagnostic) {
  if (
    diagnostic.contract_state === 'contract_violation' ||
    diagnostic.contract_violation === true ||
    (typeof diagnostic.contract_violation === 'string' && diagnostic.contract_violation !== 'false')
  ) {
    return 'нарушение контракта'
  }
  return statusLabel(diagnostic.status) || '—'
}

function hasMeaningfulMarketOpportunity(row: Record<string, unknown>) {
  const score = finiteNumber(row.opportunity_score)
  const demand = finiteNumber(row.demand_per_1000_kworks)
  const wants = finiteNumber(row.demand_wants_count)
  return Boolean(row.seed_name || row.category_id) && [score, demand, wants].some((value) => value !== null && value > 0)
}

function marketOpportunityLabel(row: Record<string, unknown>) {
  const name = uiText(row.seed_name || row.category_name || row.category_id || 'срез')
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
  const term = uiText(item.term)
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
  return statusLabel(priceRules.status)
}

function snapshotSeedName(seed: unknown) {
  const value = objectOrDefault(seed, {} as Record<string, unknown>)
  return uiText(value.name || value.title || value.category_name || value.category_id || 'срез')
}

function buyerProbeSummary(probe: Record<string, unknown>) {
  const filters = objectOrDefault(probe.filters, {} as Record<string, unknown>)
  const bits = [
    probe.query ? `запрос: ${uiText(probe.query)}` : '',
    filters.kworks_filter_to !== undefined ? `откликов до ${uiText(filters.kworks_filter_to)}` : '',
    filters.kworks_filter_from !== undefined ? `откликов от ${uiText(filters.kworks_filter_from)}` : '',
    filters.price_from !== undefined ? `бюджет от ${formatNumber(String(filters.price_from))}` : '',
    filters.price_to !== undefined ? `бюджет до ${formatNumber(String(filters.price_to))}` : '',
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
  return uiText(text.replace(/&nbsp;/gi, ' ').replace(/\s+/g, ' ').trim())
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
  const [imageFailed, setImageFailed] = useState(false)
  return (
    <article className="overflow-hidden rounded-md border border-surface-700 bg-surface-900/40">
      <div className="aspect-[3/2] bg-surface-950">
        {item.image_url && !imageFailed ? (
          <img
            src={item.image_url}
            alt=""
            className="h-full w-full object-cover"
            loading="lazy"
            onError={() => setImageFailed(true)}
          />
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
  const [supplyCoverage, setSupplyCoverage] = useState<'quick' | 'balanced' | 'deep'>(
    () => (['quick', 'balanced', 'deep'].includes(String(persisted.supplyCoverage)) ? persisted.supplyCoverage : 'balanced') as 'quick' | 'balanced' | 'deep',
  )
  const [showDemandHelp, setShowDemandHelp] = useState(false)
  const [marketScanLoading, setMarketScanLoading] = useState(false)
  const [marketScanError, setMarketScanError] = useState<string | null>(null)
  const [supplyScanResult, setSupplyScanResult] = useState<KworkSupplyScan | null>(() => {
    const stored = objectOrDefault(persisted.supplyScanResult, null as unknown as KworkSupplyScan | null)
    return stored?.source === 'psr.kwork_supply_scan' ? stored : null
  })
  const [marketAssistantQuestion, setMarketAssistantQuestion] = useState('')
  const [marketAssistantLoading, setMarketAssistantLoading] = useState(false)
  const [marketAssistantResult, setMarketAssistantResult] = useState<KworkMarketAssistantResponse | null>(null)
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
      supplyCoverage,
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
      supplyScanResult,
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
    supplyCoverage,
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
    supplyScanResult,
  ])

  const selectedRoot = topCategories.find((item) => item.id === selectedRootId)
  const rootChildren = selectedRoot?.children || []
  const selectedCategory = flatCategories.find((item) => item.id === selectedCategoryId)
  const debouncedFieldCategoryId = useDebouncedValue(selectedCategoryId, 320)

  const {
    data: attributesData,
    loading: attributesLoading,
    error: attributesError,
    refetch: refetchAttributes,
  } = useApi(
    () =>
      debouncedFieldCategoryId
        ? api.getKworkCategoryAttributes(debouncedFieldCategoryId)
        : Promise.resolve({ category_id: 0, attributes: [], flat: [] }),
    [debouncedFieldCategoryId],
  )

  const debouncedPriceCategoryId = useDebouncedValue(selectedCategoryId, 700)
  const {
    data: pricesData,
    loading: pricesLoading,
    error: pricesError,
    refetch: refetchPrices,
  } = useApi<KworkPriceRules>(
    () => (debouncedPriceCategoryId ? api.getKworkCategoryPrices(debouncedPriceCategoryId) : Promise.resolve({})),
    [debouncedPriceCategoryId],
  )

  const manifestControls = useMemo(() => formManifest?.controls || [], [formManifest])
  const sliceControls = useMemo(
    () => manifestControls.filter((control) => hasSelectableOptions(control)).slice(0, 8),
    [manifestControls],
  )
  const debouncedAttributeSelection = useDebouncedValue(attributeSelection, 220, selectedCategoryId)
  const debouncedManifestControls = useDebouncedValue(manifestControls, 220, selectedCategoryId)
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
    () => (attributesData?.category_id === selectedCategoryId ? attributesData.flat || [] : []).filter((item) => item.required),
    [attributesData, selectedCategoryId],
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
  const marketInsights = visibleMetrics?.market_insights
  const marketInsightBullets = (marketInsights?.bullets || []).filter(Boolean).slice(0, 6)
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
  const marketInsightSearchQueries = useMemo(() => {
    const raw = Array.isArray(marketInsights?.search_queries) && marketInsights.search_queries.length
      ? (marketInsights.search_queries as Array<Record<string, unknown>>)
      : [
          ...marketInsightClassifiers.slice(0, 5).map((item) => ({
            query: item.name,
            count: item.kworks_count,
            why: 'Крупный под-срез текущей рубрики.',
            source: 'classifier',
          })),
          ...marketInsightTerms.slice(0, 5).map((item) => ({
            query: item.term,
            count: item.count,
            why: 'Часто встречается в карточках.',
            source: 'terms',
          })),
        ]
    const seen = new Set<string>()
    return raw
      .map((item) => ({
        query: uiText(item.query || item.name || ''),
        count: Number(item.count || item.kworks_count || 0),
        why: uiText(item.why || item.reason || ''),
        source: uiText(item.source || ''),
      }))
      .filter((item) => {
        const query = item.query.trim().toLowerCase()
        if (!query || seen.has(query)) return false
        seen.add(query)
        return true
      })
      .slice(0, 6)
  }, [marketInsights?.search_queries, marketInsightClassifiers, marketInsightTerms])
  const wideClassifierRows = marketInsightClassifiers.slice(0, 3)
  const narrowClassifierRows = [...marketInsightClassifiers].reverse().slice(0, 3)
  const loading = categoriesLoading || metricsLoading
  const anyError = categoriesError || metricsError
  const priceRulesNotice = pricesError ||
    (pricesData?.status === 'unavailable'
      ? String(pricesData.detail || 'Ценовой справочник Kwork временно недоступен.')
      : pricesData?.status === 'stale'
        ? 'Показаны последние сохранённые ценовые ограничения.'
        : null)
  const priceRulesPending = pricesLoading && !pricesData
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
  const buyerSummary = useMemo(
    () => objectOrDefault(buyerScoutResult?.aggregate?.buyer_summary, {} as Record<string, unknown>),
    [buyerScoutResult],
  )
  const buyerRecommendations = useMemo(
    () =>
      (Array.isArray(buyerSummary.recommendations)
        ? (buyerSummary.recommendations as Array<Record<string, unknown>>)
        : Array.isArray(buyerScoutResult?.aggregate?.search_recommendations)
          ? (buyerScoutResult?.aggregate?.search_recommendations as Array<Record<string, unknown>>)
          : []
      ).slice(0, 5),
    [buyerScoutResult, buyerSummary],
  )
  const buyerNeedsTokenMode = useMemo(() => buyerLotsNeedTokenMode(buyerScoutResult), [buyerScoutResult])
  const supplyAnalysis = objectOrDefault(supplyScanResult?.analysis?.provider_output, {} as Record<string, unknown>)
  const supplyNiches = arrayOrDefault<Record<string, unknown>>(supplyAnalysis.niches, []).slice(0, 6)
  const supplyDirections = arrayOrDefault<Record<string, unknown>>(supplyAnalysis.listing_directions, []).slice(0, 6)
  const supplyAnalysisExtras = Object.entries(supplyAnalysis).filter(
    ([key, value]) => !['summary', 'supply_shape', 'niches', 'listing_directions'].includes(key) && hasStructuredValue(value),
  )
  const supplyListings = (supplyScanResult?.raw_listings || []).slice(0, 12)
  const supplySlices = (supplyScanResult?.slices || []).slice(0, 24)
  const supplyRequestDiagnostics = (supplyScanResult?.requests || []).slice(-LEGACY_REQUEST_DIAGNOSTIC_LIMIT)

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
      setAttributeSelection({ ...(manifest.selected || {}) })
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
    delayMs = 180,
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
    scheduleFormManifest({}, undefined, 320)
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
    setBuyerScoutResult(null)
    setMarketScanResult(null)
    setSupplyScanResult(null)
    setMarketAssistantResult(null)
    setMarketAssistantQuestion('')
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

  const runSupplyScan = async () => {
    if (marketScanLoading) return
    if (!selectedCategoryId) {
      setMarketScanError('Сначала выберите рубрику.')
      return
    }
    setMarketScanLoading(true)
    setMarketScanError(null)
    setMarketAssistantResult(null)
    try {
      const result = await api.getKworkSupplyScan({
        category_id: selectedCategoryId,
        category_name: selectedCategory?.name || '',
        classifier_id: selectedClassifierId,
        classifier_name: selectedClassifier?.name || classifierTrail[classifierTrail.length - 1]?.name || '',
        coverage: supplyCoverage,
        include_llm: true,
        write_file: true,
      })
      setSupplyScanResult(result)
    } catch (e) {
      setMarketScanError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketScanLoading(false)
    }
  }

  const runBuyerScout = async () => {
    if (marketScanLoading) return
    if (!selectedCategoryId) {
      setMarketScanError('Сначала выберите рубрику.')
      return
    }
    setMarketScanLoading(true)
    setMarketScanError(null)
    try {
      const buyerScout = await api.getKworkBuyerScout({
        category_id: selectedCategoryId,
        classifier_id: selectedClassifierId,
        category_name: selectedCategory?.name || '',
        classifier_name: selectedClassifier?.name || classifierTrail[classifierTrail.length - 1]?.name || '',
        attribute_selection: attributeSelection,
        attribute_controls: manifestControls,
        max_probes: 6,
        project_page_limit: 2,
        per_probe_limit: 12,
        top_limit: 24,
        include_project_details: scanWantDetails,
        include_want_details: scanWantDetails,
        include_buyer_history: scanWantDetails,
        detail_limit: scanWantDetails ? 8 : 4,
        buyer_history_limit: scanWantDetails ? 6 : 0,
        budget_max: Math.max(0, Math.min(Number(buyerBudgetMax) || 5000, 150000)),
        include_query_suggestions: false,
        include_control_windows: false,
        write_file: true,
      })
      setBuyerScoutResult(buyerScout)
    } catch (e) {
      setMarketScanError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketScanLoading(false)
    }
  }

  const askMarketAssistant = async () => {
    const contextId = supplyScanResult?.assistant_context_id
    const message = marketAssistantQuestion.trim()
    if (!contextId || !message || marketAssistantLoading) return
    setMarketAssistantLoading(true)
    setMarketScanError(null)
    try {
      const result = await api.askKworkMarketAssistant({ context_id: contextId, message })
      setMarketAssistantResult(result)
      setMarketAssistantQuestion('')
    } catch (e) {
      setMarketScanError(e instanceof Error ? e.message : String(e))
    } finally {
      setMarketAssistantLoading(false)
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
        setDraftError(preflight.preflight?.detail || 'Предпроверка публикации не прошла')
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
          <label className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-300">
            <span className="text-zinc-500">покрытие</span>
            <select
              value={supplyCoverage}
              onChange={(event) => setSupplyCoverage(event.target.value as 'quick' | 'balanced' | 'deep')}
              className="bg-transparent text-zinc-100 outline-none [&>option]:bg-zinc-950 [&>option]:text-zinc-100"
              title="Объем rubric-scoped обхода предложений; заказы покупателей сюда не входят"
            >
              <option value="quick">быстро</option>
              <option value="balanced">рабочее</option>
              <option value="deep">глубоко</option>
            </select>
          </label>
          <button
            type="button"
            onClick={runSupplyScan}
            disabled={marketScanLoading}
            className="btn btn-primary py-1.5 text-xs"
            title="Собрать предложения только в выбранной рубрике и передать их ИИ-анализу"
          >
            {marketScanLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BarChart3 className="h-3.5 w-3.5" />}
            Анализ предложений
          </button>
          <button
            type="button"
            onClick={runBuyerScout}
            disabled={marketScanLoading}
            className="btn btn-ghost py-1.5 text-xs"
            title="Отдельно найти заказы покупателей в выбранной рубрике; не влияет на анализ предложений"
          >
            <Sparkles className="h-3.5 w-3.5" />
            Поиск заказов
          </button>
          <div className="hidden">
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
            title="Добавляет правила цен Kwork к широкому снимку рынка. Медленнее обычного снимка."
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
          </div>
          <label
            className="flex items-center gap-2 rounded-md border border-surface-600 px-3 py-1.5 text-xs text-zinc-400"
            title="Строгий лимит только для отдельного поиска заказов; анализ предложений его не использует."
          >
            заказы до ₽
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
            onClick={runSupplyScan}
            disabled={marketScanLoading}
            className="hidden"
            title="Повторить анализ предложений выбранной рубрики"
          >
            {marketScanLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <BarChart3 className="h-3.5 w-3.5" />}
            Обновить предложения
          </button>
          <button
            type="button"
            onClick={loadMarketHistory}
            disabled={marketHistoryLoading}
            className="btn btn-ghost py-1.5 text-xs"
            title="Показать историю снимков рынка из index.jsonl"
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
            title="Пояснить границы анализа предложений"
          >
            <Info className="h-3.5 w-3.5" />
            методика
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
            <div className="font-medium text-white">Как читается анализ предложений</div>
            <div className="text-brand-100/80">
              Объем рубрики берется из API отдельно. Цены, повтор карточек продавца и выводы ИИ относятся только к фактически собранным карточкам и всегда показываются рядом с покрытием.
            </div>
            <div className="text-brand-100/55">
              Поиск заказов покупателей запускается отдельной кнопкой и не участвует в показателях предложения.
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

      {(priceRulesPending || priceRulesNotice) && (
        <div className="flex items-center gap-2 rounded-md border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs text-amber-100/80">
          <Info className="h-4 w-4 shrink-0 text-amber-300" />
          <span>
            {priceRulesPending
              ? 'Ценовой справочник загружается отдельно; конкуренты и метрики уже доступны.'
              : `Ценовой справочник не блокирует анализ. ${priceRulesNotice}`}
          </span>
        </div>
      )}

      {marketScanError && (
        <div className="flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{marketScanError}</span>
        </div>
      )}

      {supplyScanResult && (
        <section className="border-y border-emerald-500/30 bg-emerald-500/5 py-4">
          <div className="mx-auto max-w-[1600px] px-4">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2 text-base font-semibold text-white">
                  <BarChart3 className="h-4 w-4 text-emerald-300" />
                  Рынок предложений
                </div>
                <div className="mt-1 text-xs text-emerald-100/65">
                  {uiText(supplyScanResult.scope.category_name || 'выбранная рубрика')}
                  {supplyScanResult.scope.classifier_name ? ` / ${uiText(supplyScanResult.scope.classifier_name)}` : ''}
                </div>
              </div>
              <div className="text-right text-[11px] text-emerald-100/60">
                <div>{supplyScanResult.generated_at}</div>
                <div>{uiText(supplyScanResult.coverage.profile?.label || supplyScanResult.coverage.profile?.name || '')}</div>
              </div>
            </div>

            <div className="grid grid-cols-4 gap-px overflow-hidden border border-emerald-500/20 bg-emerald-500/20 max-lg:grid-cols-2 max-sm:grid-cols-1">
              {[
                ['объём рубрики по API', formatNumber(supplyScanResult.scope.reported_category_total), 'агрегат источника, не покрытие рынка'],
                ['наблюдаемые карточки', formatNumber(supplyScanResult.sample.observed_listings), 'фактическая выборка'],
                ['доступные API-срезы', `${formatNumber(supplyScanResult.coverage.slice_count_scanned)} / ${formatNumber(supplyScanResult.coverage.slice_count_available)}`, 'выбрано для рабочего прохода'],
                ['статус прохода', workingRunStatus(supplyScanResult.coverage), workingRunDetail(supplyScanResult.coverage)],
              ].map(([label, value, detail]) => (
                <div key={label} className="min-w-0 bg-surface-950/65 px-3 py-2.5">
                  <div className="mono-label truncate">{label}</div>
                  <div className="mt-1 truncate text-lg font-semibold text-white">{value}</div>
                  <div className="mt-1 truncate text-[11px] text-emerald-100/60">{detail}</div>
                </div>
              ))}
            </div>

            <div className="mt-3 flex flex-wrap gap-2 text-xs">
              <span className="border border-emerald-500/20 bg-surface-950/45 px-2 py-1 text-emerald-100/80">
                минимум рабочей выборки: {formatNumber(supplyScanResult.coverage.minimum_cards_target)} карточек
              </span>
              <span className="border border-emerald-500/20 bg-surface-950/45 px-2 py-1 text-emerald-100/80">
                запросов: {formatNumber(supplyScanResult.coverage.requests_completed)} / {formatNumber(supplyScanResult.coverage.requests_planned)}
              </span>
              {supplyScanResult.transport?.uses_configured_proxy_pool && (
                <span className="border border-emerald-500/20 bg-surface-950/45 px-2 py-1 text-emerald-100/80">
                  transport slots: {formatNumber(supplyScanResult.transport.client_pool_size)}
                </span>
              )}
              {supplyScanResult.scope.reported_selected_slices_total != null && (
                <span className="border border-emerald-500/20 bg-surface-950/45 px-2 py-1 text-emerald-100/80">
                  объём выбранных API-срезов: {formatNumber(supplyScanResult.scope.reported_selected_slices_total)}
                </span>
              )}
              {supplyScanResult.coverage.stopped_after_protection_signal && (
                <span className="border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-100">
                  обход остановлен по сигналу защиты
                </span>
              )}
              {supplyScanResult.coverage.aborted_after_upstream_errors && (
                <span className="border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-100">
                  обход остановлен после ошибок API
                </span>
              )}
              {supplyScanResult.coverage.request_budget_can_reach_minimum_target === false && (
                <span className="border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-100">
                  текущего лимита запросов недостаточно для цели выборки
                </span>
              )}
            </div>

            {supplyRequestDiagnostics.length > 0 && (
              <details className="mt-4 border border-surface-700 bg-surface-950/35 px-3 py-2 text-xs">
                <summary className="cursor-pointer text-zinc-300">
                  Диагностика legacy-запросов: последние {supplyRequestDiagnostics.length}
                </summary>
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full text-left text-[11px] text-zinc-400">
                    <thead className="border-b border-surface-700 text-zinc-500">
                      <tr>
                        <th className="px-2 py-1.5 font-medium">срез</th>
                        <th className="px-2 py-1.5 font-medium">запрошенный cursor</th>
                        <th className="px-2 py-1.5 font-medium">cursor в ответе</th>
                        <th className="px-2 py-1.5 font-medium">получено</th>
                        <th className="px-2 py-1.5 font-medium">новые / повторы</th>
                        <th className="px-2 py-1.5 font-medium">fingerprint</th>
                        <th className="px-2 py-1.5 font-medium">слот</th>
                        <th className="px-2 py-1.5 font-medium">статус</th>
                      </tr>
                    </thead>
                    <tbody>
                      {supplyRequestDiagnostics.map((diagnostic, index) => {
                        const requestedCursor = diagnosticCursor(
                          diagnostic.requested_cursor,
                          diagnostic.requested_page,
                          diagnostic.page,
                          diagnostic.cursor,
                        )
                        const reportedCursor = diagnosticCursor(
                          diagnostic.reported_cursor,
                          diagnostic.reported_page,
                          diagnostic.response_page,
                        )
                        const received = diagnosticNumber(diagnostic.received, diagnostic.received_count, diagnostic.card_count)
                        const newUnique = diagnosticNumber(diagnostic.new_unique, diagnostic.new, diagnostic.new_count)
                        const duplicates = diagnosticNumber(diagnostic.duplicates, diagnostic.duplicate, diagnostic.duplicate_count)
                        const fingerprint = diagnosticText(diagnostic.page_fingerprint, diagnostic.fingerprint, diagnostic.response_fingerprint)
                        const detail = diagnosticText(
                          diagnostic.detail,
                          diagnostic.contract_reason_codes?.join(', '),
                          diagnostic.failure_kind,
                        )
                        return (
                          <tr key={`${diagnostic.slice_id ?? diagnostic.slice_name ?? 'slice'}-${requestedCursor}-${index}`} className="border-b border-surface-800/80">
                            <td className="max-w-44 truncate px-2 py-1.5 text-zinc-300">
                              {diagnosticText(diagnostic.slice_name, diagnostic.slice_id, diagnostic.source)}
                            </td>
                            <td className="px-2 py-1.5 font-mono">{requestedCursor}</td>
                            <td className="px-2 py-1.5 font-mono">{reportedCursor}</td>
                            <td className="px-2 py-1.5">{received}</td>
                            <td className="px-2 py-1.5">
                              {newUnique} / {duplicates}
                            </td>
                            <td className="max-w-40 truncate px-2 py-1.5 font-mono" title={fingerprint === '—' ? undefined : fingerprint}>
                              {fingerprint}
                            </td>
                            <td className="px-2 py-1.5">{diagnosticText(diagnostic.transport_slot)}</td>
                            <td className="max-w-48 truncate px-2 py-1.5" title={detail === '—' ? undefined : detail}>
                              {diagnosticStatus(diagnostic)}
                              {detail !== '—' ? ` · ${detail}` : ''}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </details>
            )}

            <div className="mt-4 grid grid-cols-[minmax(0,1.4fr)_minmax(300px,0.6fr)] gap-4 max-xl:grid-cols-1">
              <div className="min-w-0">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs font-medium text-white">Наблюдаемые срезы</div>
                  <div className="text-[11px] text-zinc-500">данные API, не рейтинг ниши</div>
                </div>
                <div className="grid grid-cols-3 gap-2 max-lg:grid-cols-2 max-sm:grid-cols-1">
                  {supplySlices.map((slice) => (
                    <div key={String(slice.id || slice.name)} className="min-w-0 border border-surface-700 bg-surface-950/40 px-2 py-1.5">
                      <div className="truncate text-xs text-emerald-50">{uiText(slice.name || slice.id || 'срез')}</div>
                      <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-zinc-500">
                        <span>в рубрике: {formatNumber(String(slice.kworks_count || 0))}</span>
                        <span>увидено: {formatNumber(String(slice.observed_cards || 0))}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="border-l border-emerald-500/20 pl-4 max-xl:border-l-0 max-xl:border-t max-xl:pl-0 max-xl:pt-4">
                <div className="text-xs font-medium text-white">Цены в наблюдаемой выборке</div>
                <div className="mt-2 grid grid-cols-3 gap-1 text-center text-xs">
                  <div className="border border-surface-700 bg-surface-950/40 px-2 py-2">
                    <div className="text-zinc-500">минимум в выборке</div>
                    <div className="mt-1 text-emerald-100">{formatPrice(supplyScanResult.sample.price_sample?.min)}</div>
                  </div>
                  <div className="border border-surface-700 bg-surface-950/40 px-2 py-2">
                    <div className="text-zinc-500">медиана в выборке</div>
                    <div className="mt-1 text-emerald-100">{formatPrice(supplyScanResult.sample.price_sample?.median)}</div>
                  </div>
                  <div className="border border-surface-700 bg-surface-950/40 px-2 py-2">
                    <div className="text-zinc-500">максимум в выборке</div>
                    <div className="mt-1 text-emerald-100">{formatPrice(supplyScanResult.sample.price_sample?.max)}</div>
                  </div>
                </div>
                <div className="mt-3 text-[11px] leading-relaxed text-zinc-500">
                  {formatNumber(supplyScanResult.sample.unique_sellers)} уникальных продавцов в наблюдаемых карточках.
                  {supplyScanResult.sample.seller_repetition_in_observed_cards?.repeat_share_percent !== undefined
                    ? ` Повторы карточек одного продавца: ${formatNumber(supplyScanResult.sample.seller_repetition_in_observed_cards.repeat_share_percent)}%.`
                    : ''}
                </div>
              </div>
            </div>

            {!!supplyListings.length && (
              <div className="mt-4">
                <div className="mb-2 text-xs font-medium text-white">Карточки из фактической выборки</div>
                <div className="grid grid-cols-2 gap-2 max-xl:grid-cols-1">
                  {supplyListings.map((listing) => (
                    <div key={String(listing.id || listing.share_url || listing.title)} className="flex min-w-0 items-start justify-between gap-3 border border-surface-700 bg-surface-950/40 px-3 py-2">
                      <div className="min-w-0">
                        <div className="truncate text-xs text-zinc-100">{uiText(listing.title || listing.id || '-')}</div>
                        <div className="mt-1 truncate text-[11px] text-zinc-500">{uiText(listing.slice_name || 'рубрика')}</div>
                      </div>
                      <div className="shrink-0 text-xs text-emerald-100">{formatPrice(listing.price as number | string | null)}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="mt-4 border-t border-emerald-500/20 pt-4">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-xs font-medium text-white">
                  <Sparkles className="h-4 w-4 text-emerald-300" />
                  AI-анализ предложения
                </div>
                {supplyScanResult.analysis?.evidence_window && (
                  <div className="text-[11px] text-emerald-100/55">
                    ИИ: {formatNumber(supplyScanResult.analysis.evidence_window.included_listing_count)} / {formatNumber(supplyScanResult.analysis.evidence_window.raw_listing_count)} карточек
                  </div>
                )}
              </div>
              {supplyScanResult.analysis?.status === 'ok' ? (
                <div className="space-y-3 text-sm leading-relaxed text-zinc-300">
                  {hasStructuredValue(supplyAnalysis.summary) && (
                    <div>
                      <StructuredValue value={supplyAnalysis.summary} />
                    </div>
                  )}
                  {hasStructuredValue(supplyAnalysis.supply_shape) && (
                    <div className="text-zinc-400">
                      <StructuredValue value={supplyAnalysis.supply_shape} />
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
                    <div>
                      <div className="mb-2 text-xs font-medium text-emerald-100">Ниши в выборке</div>
                      <div className="space-y-2">
                        {supplyNiches.map((item, index) => (
                          <div key={`${String(item.name || index)}-${index}`} className="border-l-2 border-emerald-500/40 pl-2">
                            <div className="text-xs text-white">
                              <StructuredValue value={item.name ?? item.direction} />
                            </div>
                            <div className="mt-0.5 text-[11px] text-zinc-500">
                              <StructuredValue value={item.evidence ?? item.why} />
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                    <div>
                      <div className="mb-2 text-xs font-medium text-emerald-100">Варианты позиционирования</div>
                      <div className="space-y-2">
                        {supplyDirections.map((item, index) => (
                          <div key={`${String(item.direction || item.name || index)}-${index}`} className="border-l-2 border-brand-500/40 pl-2">
                            <div className="text-xs text-white">
                              <StructuredValue value={item.direction ?? item.name} />
                            </div>
                            <div className="mt-0.5 text-[11px] text-zinc-500">
                              <StructuredValue value={item.evidence ?? item.why} />
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                  {supplyAnalysisExtras.length > 0 && (
                    <div className="space-y-2 border-t border-surface-700 pt-3 text-xs">
                      {supplyAnalysisExtras.map(([key, value]) => (
                        <div key={key}>
                          <div className="mb-1 font-medium text-emerald-100">{structuredLabel(key)}</div>
                          <StructuredValue value={value} />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="text-xs text-zinc-500">
                  <StructuredValue value={supplyScanResult.analysis?.detail} fallback="Анализ ИИ пока недоступен." />
                </div>
              )}
            </div>

            {supplyScanResult.assistant_context_id && (
              <div className="mt-4 border-t border-emerald-500/20 pt-4">
                <div className="flex gap-2 max-md:flex-col">
                  <textarea
                    value={marketAssistantQuestion}
                    onChange={(event) => setMarketAssistantQuestion(event.target.value)}
                    rows={2}
                    placeholder="Спросить по этой выборке"
                    className="min-h-[52px] flex-1 resize-y border border-surface-600 bg-surface-950/60 px-3 py-2 text-sm text-white outline-none placeholder:text-zinc-600 focus:border-emerald-500/60"
                  />
                  <button
                    type="button"
                    onClick={askMarketAssistant}
                    disabled={!marketAssistantQuestion.trim() || marketAssistantLoading}
                    className="btn btn-primary min-w-28"
                  >
                    {marketAssistantLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                    Спросить
                  </button>
                </div>
                {marketAssistantResult && (
                  <div className="mt-3 border-l-2 border-emerald-500/50 pl-3 text-sm leading-relaxed text-zinc-200">
                    <div>
                      <StructuredValue value={marketAssistantResult.answer} />
                    </div>
                    {marketAssistantResult.refresh && (
                      <div className="mt-2 text-[11px] text-emerald-100/65">
                        Обновлен узкий срез: {uiText(objectOrDefault(marketAssistantResult.refresh, {} as Record<string, unknown>).classifier_name || '')} · {formatNumber(String(objectOrDefault(marketAssistantResult.refresh, {} as Record<string, unknown>).observed_listing_count || 0))} карточек.
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {supplyScanResult.file_path && <div className="mt-3 break-all text-[11px] text-emerald-100/55">{supplyScanResult.file_path}</div>}
          </div>
        </section>
      )}

      {buyerScoutResult && (
        <div className="market-buyer-scout rounded-md border border-brand-500/30 bg-brand-600/10 px-3 py-2 text-xs text-brand-100">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="font-medium text-white">
                Лоты покупателей: {uiText(selectedCategory?.name || 'выбранная рубрика')}
              </div>
              <div className="text-[11px] text-brand-100/60">
                ИИ анализирует лоты выбранной рубрики и проверяет только найденные в ней запросы.
              </div>
            </div>
            <div className="text-brand-100/60">{buyerScoutResult.generated_at}</div>
          </div>

          <div className="grid grid-cols-4 gap-2 max-lg:grid-cols-2 max-sm:grid-cols-1">
            {[
              ['запросов ИИ', formatNumber(buyerScoutResult.aggregate.probe_count), 'проверено внутри рубрики'],
              ['лотов', formatNumber(buyerScoutResult.aggregate.unique_projects), 'уникальных заказов'],
              ['без откликов', formatNumber(buyerScoutResult.aggregate.zero_offer_count), 'первые цели для ответа'],
              ['мало откликов', formatNumber(buyerScoutResult.aggregate.low_offer_count), '0..5 предложений'],
            ].map(([label, value, sub]) => (
              <div key={label} className="min-w-0 rounded-md border border-brand-500/20 bg-surface-950/35 p-2">
                <div className="mono-label truncate">{label}</div>
                <div className="mt-1 text-lg font-semibold text-white">{value}</div>
                <div className="mt-1 truncate text-[11px] text-brand-100/60">{sub}</div>
              </div>
            ))}
          </div>

          {(
            <div className="mt-3 grid gap-2">
              <div className="rounded-md border border-brand-500/20 bg-surface-950/35 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs font-medium text-white">Что искать сейчас</div>
                </div>
                <div className="grid gap-1.5">
                  {buyerRecommendations.map((item) => {
                    const examples = Array.isArray(item.examples)
                      ? (item.examples as Array<unknown>).map((value) => uiText(value)).filter(Boolean)
                      : []
                    return (
                      <div
                        key={String(item.query || item.name || item.priority || '')}
                        className="rounded border border-surface-700 bg-surface-950/40 px-2 py-1.5 text-[11px]"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <div className="truncate text-brand-100">{uiText(item.query || item.name || 'запрос')}</div>
                            <div className="mt-0.5 line-clamp-2 text-zinc-600">
                              {uiText(item.why || item.reason || '')}
                            </div>
                          </div>
                          <div className="shrink-0 text-right text-white">{formatNumber(String(item.count || 0))}</div>
                          <div className="shrink-0 text-right text-zinc-500">
                            приоритет {formatNumber(String(item.priority || 0))}
                          </div>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-2 text-zinc-500">
                          {item.budget ? <span>бюджет: {uiText(item.budget)}</span> : null}
                          {item.competition ? <span>конкуренция: {uiText(item.competition)}</span> : null}
                        </div>
                        {!!examples.length && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {examples.slice(0, 3).map((example) => (
                              <span
                                key={example}
                                className="max-w-full truncate rounded border border-brand-500/20 bg-brand-600/10 px-1.5 py-0.5 text-[11px] text-brand-100"
                              >
                                {example}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  })}
                  {!buyerRecommendations.length && (
                    <div className="text-[11px] text-zinc-600">
                      Недостаточно лотов выбранной рубрики, чтобы сформировать рекомендации.
                    </div>
                  )}
                </div>
              </div>
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
                    <div className="mono-label truncate">{humanizeInlineIds(signal.label || signal.kind || 'сигнал')}</div>
                    <div className="mt-1 text-lg font-semibold text-white">{formatNumber(String(signal.value ?? 0))}</div>
                    <div className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-brand-100/65">
                      {humanizeInlineIds(signal.detail || '')}
                    </div>
                    {!!projects.length && (
                      <div className="mt-2 space-y-1">
                        {projects.slice(0, 2).map((project) => (
                          <div key={String(project.id || project.title)} className="truncate text-[11px] text-zinc-400">
                            {uiText(project.title || project.id || '-')}
                          </div>
                        ))}
                      </div>
                    )}
                    {!!probes.length && (
                      <div className="mt-2 space-y-1">
                        {probes.slice(0, 2).map((probe) => (
                          <div key={String(probe.name || probe.query)} className="truncate text-[11px] text-zinc-400">
                            {humanProbeName(probe.name || probe.query || 'срез')} · {formatNumber(String(probe.count || 0))}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
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
                          {uiText(project.title || `#${String(project.id || '-')}`)}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-brand-100/70">
                          <span>балл {formatNumber(String(project.score || 0))}</span>
                          <span>откликов {formatNumber(String(project.offers ?? 0))}</span>
                          <span>бюджет {buyerProjectBudget(project)}</span>
                          {views !== null && <span>просмотров {formatNumber(views)}</span>}
                          {Boolean(project.matched_probe) && <span>{humanProbeName(project.matched_probe)}</span>}
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
                            <span className="min-w-0 truncate">{uiText(historyProject.title || `#${historyProject.id || '-'}`)}</span>
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

          {buyerNeedsTokenMode && (
            <div className="mt-2 rounded border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-100">
              Для лотов покупателей нужен вход с токеном. Сейчас Session Hub работает только по кукам, поэтому PSR пропустил медленные запросы заказов и оставил подсказки.
            </div>
          )}

          {!!buyerScoutResult.endpoint_errors?.length && !buyerNeedsTokenMode && (
            <div className="mt-2 rounded border border-red-500/25 bg-red-500/10 px-2 py-1 text-[11px] text-red-200">
              Ошибки API покупателей: {buyerScoutResult.endpoint_errors.length}. Подробности сохранены в JSON.
            </div>
          )}

          {buyerScoutResult.file_path && (
            <div className="mt-2 break-all rounded border border-brand-500/20 bg-surface-950/40 px-2 py-1 text-[11px] text-brand-100/70">
              снимок: {buyerScoutResult.file_path}
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
                <div className="text-xs font-medium text-white">Предложение по кворкам</div>
                <div className="text-[11px] text-emerald-200/60">кворки по выбранным срезам</div>
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
                            <span>рубрика {uiText(seed.category_id || '-')}</span>
                            {seed.classifier_id ? <span>классификатор {uiText(seed.classifier_id)}</span> : null}
                            <span>кворков {formatNumber(String(row.kworks_count || 0))}</span>
                            <span>заказов {formatNumber(snapshotDemandCount(demandRow))}</span>
                            <span>примеров {formatNumber(cards.length)}</span>
                          </div>
                        </div>
                        {row.timings_ms && (
                          <div className="shrink-0 text-[11px] text-zinc-500">
                            {formatNumber(String(objectOrDefault(row.timings_ms, {} as Record<string, unknown>).total || 0))} мс
                          </div>
                        )}
                      </div>
                      {!!classifiers.length && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {classifiers.slice(0, 5).map((classifier) => (
                            <span key={String(classifier.id || classifier.name)} className="rounded border border-surface-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                              {uiText(classifier.name || classifier.id)}: {formatNumber(String(classifier.kworks_count || 0))}
                            </span>
                          ))}
                        </div>
                      )}
                      {!!cards.length && (
                        <div className="mt-2 space-y-1">
                          {cards.slice(0, 3).map((card) => (
                            <div key={String(card.id || card.title)} className="flex items-center justify-between gap-2 text-[11px] text-zinc-400">
                              <span className="min-w-0 truncate">{uiText(card.title || card.id || '-')}</span>
                              <span className="shrink-0 text-emerald-100/70">{formatPrice(card.price as number | string | null)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                      {(snapshotSampleCount(demandRow) > 0 || priceText) && (
                        <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-zinc-500">
                          {snapshotSampleCount(demandRow) > 0 && <span>примеров спроса {formatNumber(snapshotSampleCount(demandRow))}</span>}
                          {priceText && <span>цены {priceText}</span>}
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
                <div className="text-xs font-medium text-white">Спрос по заказам</div>
                <div className="text-[11px] text-sky-100/60">заказы и счетчик спроса</div>
              </div>
              <div className="grid grid-cols-4 gap-2 max-xl:grid-cols-2 max-sm:grid-cols-1">
                {marketQueryDemandRows.map(({ query, value }) => {
                  const sample = Array.isArray(value.sample) ? (value.sample as Array<Record<string, unknown>>) : []
                  return (
                    <div key={query} className="min-w-0 rounded border border-sky-500/20 bg-surface-950/35 p-2">
                      <div className="truncate text-xs font-medium text-sky-50">{uiText(query || 'все')}</div>
                      <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-sky-100/70">
                        <span>заказов {formatNumber(snapshotDemandCount(value))}</span>
                        <span>примеров {formatNumber(snapshotSampleCount(value))}</span>
                        <span>{statusLabel(value.status || 'unknown')}</span>
                      </div>
                      {!!sample.length && (
                        <div className="mt-2 space-y-1">
                          {sample.slice(0, 2).map((project) => (
                            <div key={String(project.id || project.title)} className="truncate text-[11px] text-zinc-400">
                              {uiText(project.title || project.id || '-')} · {formatNumber(String(project.offers ?? 0))} откл.
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
                  title={`спрос=${uiText(row.demand_wants_count ?? '-')} конкуренты=${uiText(row.supply_kworks_count ?? '-')}`}
                >
                  {marketOpportunityLabel(row)}
                </span>
              ))}
            </div>
          )}
          {!!marketSellerRows.length && (
            <div className="mt-3 rounded-md border border-purple-500/20 bg-purple-500/10 p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium text-white">Профили продавцов</div>
                <div className="text-[11px] text-purple-100/60">профиль, портфолио, отзывы</div>
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
                          <div className="truncate text-xs font-medium text-purple-50">{uiText(seller.username || seller.display_name || seller.id || '-')}</div>
                          <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-purple-100/70">
                            {seller.level ? <span>{uiText(seller.level)}</span> : null}
                            {seller.rating ? <span>рейтинг {uiText(seller.rating)}</span> : null}
                            {seller.reviews_count ? <span>отзывов {formatNumber(String(seller.reviews_count))}</span> : null}
                            {seller.active_kworks_count ? <span>активных {formatNumber(String(seller.active_kworks_count))}</span> : null}
                          </div>
                        </div>
                        <span className="shrink-0 text-[11px] text-zinc-500">{statusLabel(seller.status || '')}</span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-zinc-400">
                        <span>портфолио {formatNumber(String(portfolio.total ?? portfolio.sample_count ?? 0))}</span>
                        <span>отзывов {formatNumber(String(allReviews.total ?? allReviews.sample_count ?? 0))}</span>
                        <span>негатив {formatNumber(String(negativeReviews.total ?? negativeReviews.sample_count ?? 0))}</span>
                      </div>
                      {!!sampleKworks.length && (
                        <div className="mt-2 space-y-1">
                          {sampleKworks.slice(0, 2).map((kwork) => (
                            <div key={String(kwork.id || kwork.title)} className="truncate text-[11px] text-zinc-500">
                              {uiText(kwork.title || kwork.id || '-')}
                            </div>
                          ))}
                        </div>
                      )}
                      {Array.isArray(seller.errors) && seller.errors.length > 0 && (
                        <div className="mt-2 line-clamp-2 text-[11px] text-amber-200/80">{uiText(seller.errors[0])}</div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          {Array.isArray(marketScanResult.aggregate.top_sellers) && marketScanResult.aggregate.top_sellers.length > 0 && (
            <div className="mt-2 rounded border border-surface-700 bg-surface-950/30 px-2 py-1 text-[11px] text-zinc-500">
              <span className="mr-2 text-zinc-400">повторяемость продавцов в наблюдаемой выборке:</span>
              {marketScanResult.aggregate.top_sellers.slice(0, 8).map((item) => {
                const tuple = Array.isArray(item) ? item : []
                return (
                  <span key={String(tuple[0])} className="mr-2 inline-block">
                    {uiText(tuple[0] || '-')}: {formatNumber(String(tuple[1] || 0))}
                  </span>
                )
              })}
            </div>
          )}
          {marketScanResult.account_context && marketScanResult.account_context.status !== 'skipped' && (
            <div className="mt-2 rounded border border-brand-500/25 bg-brand-600/10 px-2 py-1 text-[11px] text-brand-100">
              <span className="text-brand-200/70">аккаунт:</span>{' '}
              {uiText(marketScanResult.account_context.username || statusLabel(marketScanResult.account_context.status) || 'неизвестно')}
              {marketScanResult.account_context.active_kworks_count !== undefined
                ? ` · активных ${formatNumber(finiteNumber(marketScanResult.account_context.active_kworks_count) ?? 0)}`
                : ''}
              {marketScanResult.account_context.offers_count !== undefined
                ? ` · откликов ${formatNumber(finiteNumber(marketScanResult.account_context.offers_count) ?? 0)}`
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
            <div className="font-medium text-white">История снимков</div>
            <div className="text-brand-100/60">
              {marketHistory.entry_count} записей · {uiText(marketHistory.latest?.generated_at || 'нет последнего')}
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
                  <div className="min-w-0 truncate text-brand-100/70">{uiText(entry.generated_at || '-')}</div>
                  <div className="min-w-0 truncate text-brand-50">
                    кворки {formatNumber(Number(aggregate.cards_seen || 0))} · продавцы{' '}
                    {formatNumber(Number(aggregate.unique_sellers_seen || 0))} · лучший срез{' '}
                    {snapshotSeedName(top)}:{' '}
                    {uiText(top.opportunity_score ?? top.demand_per_1000_kworks ?? '-')}
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
              <MetricTile label="объем рубрики" value={formatNumber(visibleMetrics?.kworks_count)} sub="сообщает API для выбранного среза" />
              <MetricTile label="карточек в превью" value={formatNumber(competitors.length)} sub="не используется для выводов о всем рынке" />
              <MetricTile label="твоя цена" value={formatPrice(price)} sub="настраивается вручную" />
              <MetricTile label="срез рынка" value={selectedClassifierId ? 'узкий' : 'вся рубрика'} sub="для анализа предложений используйте кнопку сверху" />
            </div>

            {false && marketInsights && (
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
                      <div className="mono-label">сила продавцов</div>
                      <div className="mt-1 text-sm font-semibold text-white">
                        {formatNumber(marketInsights.seller_review_strength?.reviews_100_plus)}/{formatNumber(marketInsights.seller_review_strength?.sample_size)}
                      </div>
                      <div className="mt-1 text-[11px] text-zinc-500">карточек с 100+ отзывами</div>
                    </div>
                    <div className="rounded-md border border-surface-700 bg-surface-950/35 p-2">
                      <div className="mono-label">повторы карточек</div>
                      <div className="mt-1 text-sm font-semibold text-white">
                        {formatPercent(marketInsights.seller_repetition_in_sample?.repeat_share)}
                      </div>
                      <div className="mt-1 text-[11px] text-zinc-500">в короткой выборке, не доля рынка</div>
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
                    <Tags className="h-4 w-4 text-emerald-300" />
                    Структура рубрики
                  </div>
                  {!!marketInsightSearchQueries.length && (
                    <div className="grid gap-1.5 text-xs text-emerald-50/90">
                      {marketInsightSearchQueries.map((item) => (
                        <div key={item.query} className="rounded border border-emerald-500/15 bg-surface-950/25 px-2 py-1.5">
                          <div className="flex items-center justify-between gap-2">
                            <div className="font-medium text-white">{item.query}</div>
                            <div className="shrink-0 text-[11px] text-emerald-100/70">
                              {formatNumber(String(item.count || 0))} кворков
                            </div>
                          </div>
                          <div className="mt-0.5 text-[11px] text-emerald-100/60">Раздел текущей рубрики по данным Kwork.</div>
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
                                value={uiText(attributeSelection[control.name] ?? control.value ?? '')}
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
                        Введите фразу <span className="font-semibold text-amber-50">{uiText(publishConfirmation.phrase)}</span>,
                        чтобы отправить кворк в Kwork.
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <input
                          value={publishConfirmInput}
                          onChange={(event) => setPublishConfirmInput(event.target.value)}
                          className="input min-w-[220px] flex-1"
                          placeholder={uiText(publishConfirmation.phrase)}
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
                          {draftResult.image.status}
                          {draftResult.image.prompt_source ? ` · промпт: ${draftResult.image.prompt_source}` : ''}
                          {draftResult.image.requested_image_model ? ` · ${draftResult.image.requested_image_model}` : ''}
                          {draftResult.image.image_quality ? ` · ${draftResult.image.image_quality}` : ''}
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
              {attributesError ? (
                <div className="mt-3 rounded-md border border-amber-500/25 bg-amber-500/8 px-3 py-2 text-xs text-amber-100/80">
                  Поля рубрики временно недоступны и не блокируют анализ: {attributesError}
                </div>
              ) : attributesLoading ? (
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
