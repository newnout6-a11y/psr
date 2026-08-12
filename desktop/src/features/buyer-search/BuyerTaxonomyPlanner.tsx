import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronRight, FolderTree, Loader2, RefreshCw, Search, X } from 'lucide-react'

import { api, type KworkCategoryNode } from '../../lib/api'
import { buyerTaxonomyApi } from './taxonomy-api'
import type {
  BuyerTaxonomyCategory,
  BuyerTaxonomyCategoryContext,
  BuyerTaxonomyJson,
  BuyerTaxonomySelection,
  BuyerTaxonomySnapshot,
} from './taxonomy-types'

export interface BuyerTaxonomyPlannerProps {
  onSelect(selections: BuyerTaxonomySelection[]): void
  disabled?: boolean
  className?: string
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function requestErrorMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : ''
  if (message.includes('disabled by feature flag')) return 'Загрузка каталога отключена в настройках приложения.'
  if (message.includes('no idle Buyer worker')) return 'Для загрузки нужен свободный воркер выбранного аккаунта с маршрутом VPNTE.'
  if (message.includes('does not match the requested taxonomy account')) return 'Выбранный аккаунт не привязан к активному запуску.'
  if (message.includes('taxonomy is unavailable')) return 'Сервис каталога временно недоступен.'
  return fallback
}

function categoryScope(context: BuyerTaxonomyCategoryContext): Record<string, BuyerTaxonomyJson> {
  const { category, generation_defaults: defaults, snapshot } = context
  return {
    category_id: defaults.category_id,
    category_path: defaults.category_path,
    taxonomy_snapshot_id: defaults.taxonomy_snapshot_id,
    taxonomy_source: defaults.taxonomy_source,
    category_name: category.name,
    rubric_id: category.rubric_id,
    classifier_id: category.classifier_id,
    snapshot_captured_at: snapshot.captured_at,
  }
}

function snapshotLabel(snapshot: BuyerTaxonomySnapshot): string {
  const capturedAt = new Date(snapshot.captured_at)
  const when = Number.isNaN(capturedAt.valueOf()) ? snapshot.captured_at : capturedAt.toLocaleString('ru-RU')
  const source = snapshot.source === 'kwork_market_categories' ? 'Каталог Kwork' : snapshot.source
  return `${source} · обновлён ${when}`
}

function visibleSnapshots(items: BuyerTaxonomySnapshot[]): BuyerTaxonomySnapshot[] {
  let hasAutomaticCatalog = false
  return items.filter((snapshot) => {
    if (snapshot.source !== 'kwork_market_categories') return true
    if (hasAutomaticCatalog) return false
    hasAutomaticCatalog = true
    return true
  })
}

function breadcrumbLabel(path: BuyerTaxonomyCategory[]): string {
  return path.map((category) => category.name).join(' / ')
}

function formatKworksCount(count: number): string {
  return new Intl.NumberFormat('ru-RU').format(count)
}

function kworksLabel(count: number | null | undefined): string {
  if (count === undefined) return 'Услуг в каталоге: считаем…'
  if (count === null) return 'Услуг в каталоге: нет данных'
  return `Услуг в каталоге: ${formatKworksCount(count)}`
}

function flattenMarketCategories(
  nodes: KworkCategoryNode[],
  parentCategoryId: number | null = null,
  parentPath: number[] = [],
): Array<Record<string, unknown>> {
  const result: Array<Record<string, unknown>> = []
  for (const node of nodes) {
    const categoryId = Number(node.id)
    if (!Number.isInteger(categoryId) || categoryId <= 0) continue
    const categoryPath = [...parentPath, categoryId]
    result.push({
      category_id: categoryId,
      name: node.name.trim() || String(categoryId),
      parent_category_id: parentCategoryId,
      rubric_id: parentPath[0] ?? categoryId,
      category_path: categoryPath,
      source_path: node.alias ?? null,
      metadata: { ...(node.raw ?? {}), kworks_count: node.kworks_count ?? null },
    })
    result.push(...flattenMarketCategories(node.children ?? [], categoryId, categoryPath))
  }
  return result
}

export function BuyerTaxonomyPlanner({
  onSelect,
  disabled = false,
  className = '',
}: BuyerTaxonomyPlannerProps) {
  const [snapshots, setSnapshots] = useState<BuyerTaxonomySnapshot[]>([])
  const [snapshotId, setSnapshotId] = useState<string | null>(null)
  const [path, setPath] = useState<BuyerTaxonomyCategory[]>([])
  const [categories, setCategories] = useState<BuyerTaxonomyCategory[]>([])
  const [nextAfterCategoryId, setNextAfterCategoryId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [loadingSnapshots, setLoadingSnapshots] = useState(true)
  const [loadingCategories, setLoadingCategories] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [bootstrappingSnapshot, setBootstrappingSnapshot] = useState(false)
  const [selectingCategoryIds, setSelectingCategoryIds] = useState<number[]>([])
  const [selectedSelections, setSelectedSelections] = useState<BuyerTaxonomySelection[]>([])
  const [error, setError] = useState<string | null>(null)
  const categoryRequestRef = useRef<AbortController | null>(null)
  const snapshotRequestRef = useRef<AbortController | null>(null)
  const nextAfterCategoryIdRef = useRef<number | null>(null)
  const bootstrapAttemptedRef = useRef(false)
  const bootstrapInFlightRef = useRef(false)
  const selectedSelectionsRef = useRef<BuyerTaxonomySelection[]>([])
  const snapshotIdRef = useRef<string | null>(null)

  const activeSnapshot = useMemo(
    () => snapshots.find((snapshot) => snapshot.snapshot_id === snapshotId) ?? null,
    [snapshotId, snapshots],
  )
  const parentCategoryId = path.length ? path[path.length - 1].category_id : null
  const normalizedQuery = query.trim()
  const selectedCategoryIds = useMemo(
    () => new Set(selectedSelections.map((selection) => selection.category.category_id)),
    [selectedSelections],
  )
  const selectedKworksTotal = useMemo(() => {
    if (!selectedSelections.length || selectedSelections.some((selection) => typeof selection.kworksCount !== 'number')) return null
    return selectedSelections.reduce((total, selection) => total + (selection.kworksCount ?? 0), 0)
  }, [selectedSelections])

  const commitSelections = useCallback((nextSelections: BuyerTaxonomySelection[]) => {
    selectedSelectionsRef.current = nextSelections
    setSelectedSelections(nextSelections)
    onSelect(nextSelections)
  }, [onSelect])

  useEffect(() => {
    snapshotIdRef.current = snapshotId
  }, [snapshotId])

  const loadSnapshots = useCallback(async () => {
    snapshotRequestRef.current?.abort()
    const controller = new AbortController()
    snapshotRequestRef.current = controller
    setLoadingSnapshots(true)
    setError(null)
    try {
      const response = await buyerTaxonomyApi.listSnapshots({ signal: controller.signal })
      const items = visibleSnapshots(response.items)
      setSnapshots(items)
      setSnapshotId((current) => {
        if (current && items.some((snapshot) => snapshot.snapshot_id === current)) return current
        return items[0]?.snapshot_id ?? null
      })
      return items
    } catch (requestError) {
      if (isAbortError(requestError)) return null
      if (!isAbortError(requestError)) setError(requestErrorMessage(requestError, 'Не удалось загрузить сохраненные снимки каталога.'))
      return []
    } finally {
      if (!controller.signal.aborted) setLoadingSnapshots(false)
    }
  }, [])

  const bootstrapSnapshot = useCallback(async () => {
    if (disabled || bootstrapInFlightRef.current) return
    bootstrapInFlightRef.current = true
    setBootstrappingSnapshot(true)
    setError(null)
    try {
      const catalog = await api.getKworkMarketCategories()
      const catalogCategories = flattenMarketCategories(catalog.categories)
      if (!catalogCategories.length) throw new Error('Каталог Kwork не вернул ни одной категории.')
      await buyerTaxonomyApi.recordSnapshot({
        source: 'kwork_market_categories',
        capturedAt: new Date().toISOString(),
        provenance: {
          source: 'Kwork market categories',
          transport: 'VPNTE proxy',
          capture_mode: 'automatic',
        },
        categories: catalogCategories,
      })
      await loadSnapshots()
    } catch (requestError) {
      setError(requestErrorMessage(requestError, 'Не удалось автоматически загрузить каталог категорий.'))
    } finally {
      bootstrapInFlightRef.current = false
      setBootstrappingSnapshot(false)
    }
  }, [disabled, loadSnapshots])

  const loadCategories = useCallback(
    async ({ append = false }: { append?: boolean } = {}) => {
      if (!snapshotId) {
        setCategories([])
        nextAfterCategoryIdRef.current = null
        setNextAfterCategoryId(null)
        return
      }
      if (!append) {
        categoryRequestRef.current?.abort()
        categoryRequestRef.current = new AbortController()
        setLoadingCategories(true)
      } else {
        setLoadingMore(true)
      }
      const controller = categoryRequestRef.current
      if (!controller) return
      setError(null)
      try {
        const response = await buyerTaxonomyApi.listCategories({
          snapshotId,
          parentCategoryId,
          query: normalizedQuery || undefined,
          afterCategoryId: append ? nextAfterCategoryIdRef.current : null,
          signal: controller.signal,
        })
        setCategories((current) => (append ? [...current, ...response.items] : response.items))
        nextAfterCategoryIdRef.current = response.next_after_category_id
        setNextAfterCategoryId(response.next_after_category_id)
      } catch (requestError) {
        if (!isAbortError(requestError)) setError(requestErrorMessage(requestError, 'Не удалось загрузить категории.'))
      } finally {
        if (!controller.signal.aborted) {
          setLoadingCategories(false)
          setLoadingMore(false)
        }
      }
    },
    [normalizedQuery, parentCategoryId, snapshotId],
  )

  useEffect(() => {
    void loadSnapshots().then((items) => {
      if (items !== null && !items.length && !bootstrapAttemptedRef.current) {
        bootstrapAttemptedRef.current = true
        void bootstrapSnapshot()
      }
    })
    return () => snapshotRequestRef.current?.abort()
  }, [bootstrapSnapshot, loadSnapshots])

  useEffect(() => {
    if (!snapshotId) return undefined
    const timer = window.setTimeout(() => void loadCategories(), normalizedQuery ? 180 : 0)
    return () => window.clearTimeout(timer)
  }, [loadCategories, normalizedQuery, parentCategoryId, snapshotId])

  useEffect(() => () => categoryRequestRef.current?.abort(), [])

  const chooseSnapshot = useCallback((nextSnapshotId: string) => {
    setSnapshotId(nextSnapshotId)
    snapshotIdRef.current = nextSnapshotId
    setPath([])
    setCategories([])
    nextAfterCategoryIdRef.current = null
    setNextAfterCategoryId(null)
    setQuery('')
    commitSelections([])
  }, [commitSelections])

  const browseCategory = useCallback((category: BuyerTaxonomyCategory) => {
    setPath((current) => [...current, category])
    setCategories([])
    nextAfterCategoryIdRef.current = null
    setNextAfterCategoryId(null)
    setQuery('')
  }, [])

  const browseBreadcrumb = useCallback((index: number) => {
    setPath((current) => (index < 0 ? [] : current.slice(0, index + 1)))
    setCategories([])
    nextAfterCategoryIdRef.current = null
    setNextAfterCategoryId(null)
    setQuery('')
  }, [])

  const updateSelectionKworksCount = useCallback((categoryId: number, kworksCount: number | null) => {
    const current = selectedSelectionsRef.current
    if (!current.some((selection) => selection.category.category_id === categoryId)) return
    commitSelections(current.map((selection) => (
      selection.category.category_id === categoryId ? { ...selection, kworksCount } : selection
    )))
  }, [commitSelections])

  const selectCategory = useCallback(
    async (category: BuyerTaxonomyCategory) => {
      if (!snapshotId || disabled) return
      const existingSelections = selectedSelectionsRef.current
      if (existingSelections.some((selection) => selection.category.category_id === category.category_id)) {
        commitSelections(existingSelections.filter((selection) => selection.category.category_id !== category.category_id))
        return
      }
      setSelectingCategoryIds((current) => [...new Set([...current, category.category_id])])
      setError(null)
      try {
        const context = await buyerTaxonomyApi.getCategoryContext(category.category_id, { snapshotId })
        if (snapshotIdRef.current !== snapshotId) return
        const categoryPath = [...context.ancestors, context.category]
        const selection: BuyerTaxonomySelection = {
          snapshot: context.snapshot,
          category: context.category,
          categoryPath,
          categoryScope: categoryScope(context),
          kworksCount: undefined,
          generationDefaults: context.generation_defaults,
          context,
        }
        commitSelections([...selectedSelectionsRef.current, selection])
        void api.getKworkMarketMetrics({
          category_id: context.category.category_id,
          include_demand: false,
          include_competitor_details: false,
          competitor_detail_limit: 0,
        }).then(
          (metrics) => updateSelectionKworksCount(
            context.category.category_id,
            Number.isFinite(metrics.kworks_count) && metrics.kworks_count >= 0 ? metrics.kworks_count : null,
          ),
          () => updateSelectionKworksCount(context.category.category_id, null),
        )
      } catch (requestError) {
        setError(requestErrorMessage(requestError, 'Не удалось загрузить параметры выбранной категории.'))
      } finally {
        setSelectingCategoryIds((current) => current.filter((categoryId) => categoryId !== category.category_id))
      }
    },
    [commitSelections, disabled, snapshotId, updateSelectionKworksCount],
  )

  const isEmpty = !loadingSnapshots && !bootstrappingSnapshot && !error && snapshots.length === 0
  const hasNoCategories = !loadingCategories && !error && snapshots.length > 0 && categories.length === 0
  const retry = () => {
    if (!snapshots.length && !snapshotId) {
      bootstrapAttemptedRef.current = true
      void bootstrapSnapshot()
    }
    else void loadCategories()
  }

  return (
    <section className={`overflow-hidden border border-surface-700 bg-surface-900 ${className}`.trim()} aria-label="Выбор категории Kwork">
      <div className="flex items-center justify-between gap-3 border-b border-surface-700 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <FolderTree className="h-4 w-4 shrink-0 text-brand-300" aria-hidden="true" />
          <span className="truncate text-sm font-medium text-zinc-100">Категории Kwork</span>
        </div>
        <button
          type="button"
          className="btn btn-ghost h-7 w-7 shrink-0 justify-center px-0"
          title="Обновить сохраненные снимки"
          aria-label="Обновить сохраненные снимки"
          disabled={loadingSnapshots || bootstrappingSnapshot || disabled}
          onClick={() => {
            if (!snapshots.length) {
              bootstrapAttemptedRef.current = true
              void bootstrapSnapshot()
            } else {
              void loadSnapshots()
            }
          }}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loadingSnapshots ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="space-y-2 border-b border-surface-700 p-3">
        {snapshots.length === 1 ? (
          <div className="input flex h-9 items-center text-xs text-zinc-100" aria-label="Каталог Kwork">
            {snapshotLabel(snapshots[0])}
          </div>
        ) : (
          <label className="block">
            <span className="sr-only">Версия каталога</span>
            <select
              className="input h-9 w-full text-xs"
              value={snapshotId ?? ''}
              disabled={disabled || loadingSnapshots || snapshots.length === 0}
              onChange={(event) => chooseSnapshot(event.target.value)}
            >
              {!snapshotId && <option value="">Нет сохраненных каталогов</option>}
              {snapshots.map((snapshot) => (
                <option key={snapshot.snapshot_id} value={snapshot.snapshot_id}>{snapshotLabel(snapshot)}</option>
              ))}
            </select>
          </label>
        )}

        {activeSnapshot && (
          <div className="flex items-center justify-between gap-2 text-[11px] text-zinc-500">
            <span className="truncate">{activeSnapshot.category_count} категорий</span>
          </div>
        )}
        {selectedSelections.length > 0 && (
          <div className="border-t border-surface-800 pt-2">
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px]">
              <span className="text-zinc-300">Выбрано: {selectedSelections.length}</span>
              <span className="text-cyan-200">
                {selectedKworksTotal !== null
                  ? `Услуг в каталоге: ${formatKworksCount(selectedKworksTotal)}`
                  : selectedSelections.some((selection) => selection.kworksCount === undefined)
                    ? 'Услуг в каталоге: считаем…'
                    : 'Услуг в каталоге: нет данных'}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {selectedSelections.map((selection) => (
                <button
                  key={selection.category.category_id}
                  type="button"
                  className="flex max-w-full items-center gap-1 border border-brand-500/35 bg-brand-500/10 px-1.5 py-1 text-left text-[11px] text-zinc-100 hover:bg-brand-500/20"
                  disabled={disabled}
                  title={`Убрать ${selection.category.name}`}
                  onClick={() => void selectCategory(selection.category)}
                >
                  <Check className="h-3 w-3 shrink-0 text-brand-300" aria-hidden="true" />
                  <span className="max-w-44 truncate">{selection.category.name}</span>
                  <span className="shrink-0 text-zinc-400">{kworksLabel(selection.kworksCount)}</span>
                  <X className="h-3 w-3 shrink-0 text-zinc-400" aria-hidden="true" />
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {snapshots.length > 0 && (
        <div className="border-b border-surface-700 p-3">
          <label className="relative block">
            <span className="sr-only">Поиск категорий</span>
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" aria-hidden="true" />
            <input
              className="input h-9 w-full pl-8 text-xs"
              placeholder="Название или ID категории"
              value={query}
              disabled={disabled || !snapshotId}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>
      )}

      {path.length > 0 && !normalizedQuery && (
        <nav className="flex min-h-9 items-center gap-1 overflow-x-auto border-b border-surface-700 px-3 py-2 text-[11px] text-zinc-400" aria-label="Путь категории">
          <button type="button" className="shrink-0 hover:text-zinc-100" disabled={disabled} onClick={() => browseBreadcrumb(-1)}>Все категории</button>
          {path.map((category, index) => (
            <span key={category.category_id} className="flex min-w-0 items-center gap-1">
              <ChevronRight className="h-3 w-3 shrink-0 text-zinc-600" aria-hidden="true" />
              <button type="button" className="max-w-32 truncate hover:text-zinc-100" disabled={disabled} onClick={() => browseBreadcrumb(index)} title={breadcrumbLabel(path.slice(0, index + 1))}>
                {category.name}
              </button>
            </span>
          ))}
        </nav>
      )}

      <div className="max-h-64 overflow-y-auto" aria-live="polite">
        {(loadingSnapshots || bootstrappingSnapshot) && <div className="flex items-center justify-center gap-2 px-3 py-8 text-xs text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" />Загрузка каталога рубрик...</div>}
        {isEmpty && (
          <div className="px-3 py-8 text-center text-xs leading-5 text-zinc-500">
            Каталог рубрик пока пуст. Нажмите «Обновить», чтобы загрузить его автоматически.
          </div>
        )}
        {!loadingSnapshots && !bootstrappingSnapshot && error && (
          <div className="px-3 py-6 text-center">
            <p className="break-words text-xs leading-5 text-rose-300">{error}</p>
            <button type="button" className="btn btn-ghost mt-2 h-7 text-xs" disabled={disabled} onClick={retry}>Повторить</button>
          </div>
        )}
        {!loadingSnapshots && !bootstrappingSnapshot && !error && loadingCategories && (
          <div className="flex items-center justify-center gap-2 px-3 py-8 text-xs text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" />Загрузка категорий...</div>
        )}
        {hasNoCategories && (
          <div className="px-3 py-8 text-center text-xs leading-5 text-zinc-500">
            {normalizedQuery ? 'В выбранном снимке нет категорий, подходящих под поиск.' : 'На этом уровне снимка нет категорий.'}
          </div>
        )}
        {!loadingSnapshots && !bootstrappingSnapshot && !error && !loadingCategories && categories.map((category) => {
          const selecting = selectingCategoryIds.includes(category.category_id)
          const selected = selectedCategoryIds.has(category.category_id)
          const selection = selectedSelections.find((item) => item.category.category_id === category.category_id)
          return (
            <div key={category.category_id} className="flex min-w-0 items-stretch border-b border-surface-800 last:border-b-0">
              <label
                className={`flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-surface-800 ${disabled || selecting ? 'cursor-wait opacity-70' : ''}`}
              >
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 shrink-0 accent-brand-400"
                  checked={selected || selecting}
                  disabled={disabled || selecting}
                  aria-label={`${selected ? 'Убрать' : 'Выбрать'} ${category.name}`}
                  onChange={() => void selectCategory(category)}
                />
                <span className="min-w-0 flex-1 truncate text-xs text-zinc-100">{category.name}</span>
                {selecting ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-brand-300" aria-hidden="true" /> : null}
                {selected && selection ? <span className="shrink-0 text-[10px] text-cyan-200">{kworksLabel(selection.kworksCount)}</span> : null}
                <span className="shrink-0 font-mono text-[10px] text-zinc-500">№ {category.category_id}</span>
              </label>
              {!normalizedQuery && (
                <button
                  type="button"
                  className="btn btn-ghost h-auto w-9 shrink-0 justify-center rounded-none border-l border-surface-800 px-0"
                  title={`Открыть ${category.name}`}
                  aria-label={`Открыть ${category.name}`}
                  disabled={disabled}
                  onClick={() => browseCategory(category)}
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              )}
            </div>
          )
        })}
        {!loadingSnapshots && !bootstrappingSnapshot && !error && !loadingCategories && categories.length > 0 && nextAfterCategoryId !== null && (
          <div className="border-t border-surface-700 p-2">
            <button type="button" className="btn btn-ghost h-8 w-full text-xs" disabled={disabled || loadingMore} onClick={() => void loadCategories({ append: true })}>
              {loadingMore ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Показать еще
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
