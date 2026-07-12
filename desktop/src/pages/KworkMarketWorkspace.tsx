import { useEffect, useMemo, useState } from 'react'
import { ArrowRight, ChevronDown, ExternalLink, Plus, RefreshCw, Search, X } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'

import { marketJobsApi } from '../features/kwork-market/api'
import { useMarketJobs, type MarketJobsLoadState } from '../features/kwork-market/hooks/useMarketJobs'
import type { CreateMarketJobPayload, MarketNetworkPolicy, MarketSourcePolicy } from '../features/kwork-market/types'
import { MarketStateBadge, formatCount, formatTimestamp } from '../features/kwork-market/components/shared'
import { useApi } from '../hooks/useApi'
import { api, type KworkCatalogAlias, type KworkCategoryNode } from '../lib/api'

const DEFAULT_FORM: CreateMarketJobPayload = {
  scope: { category_id: 0, category_name: '', canonical_alias: '' },
  profile: 'working',
  target_unique_cards: 60,
  desired_workers: 2,
  network_policy: 'prefer_vpnte',
  source_policy: 'validated_only',
  include_ai: true,
}

const PROFILE_PRESETS: Record<string, { targetUniqueCards: number; desiredWorkers: number }> = {
  working: { targetUniqueCards: 60, desiredWorkers: 2 },
  deep: { targetUniqueCards: 500, desiredWorkers: 3 },
  extended: { targetUniqueCards: 2000, desiredWorkers: 4 },
}

const JOBS_LOAD_STATE_LABEL: Record<MarketJobsLoadState, string> = {
  idle: 'ожидание',
  loading: 'загрузка',
  ready: 'готово',
  error: 'ошибка',
}

interface CategoryChoice {
  id: number
  name: string
  label: string
  alias: string | null
  depth: number
}

function flattenCategories(nodes: KworkCategoryNode[], parent = '', depth = 0): CategoryChoice[] {
  const result: CategoryChoice[] = []
  for (const node of nodes) {
    if (!node?.id) continue
    const name = (typeof node.name === 'string' ? node.name.trim() : '') || `#${node.id}`
    const label = parent ? `${parent} / ${name}` : name
    const alias = typeof node.alias === 'string' ? node.alias.trim() : ''
    result.push({ id: node.id, name, label, alias: alias || null, depth })
    result.push(...flattenCategories(node.children || [], label, depth + 1))
  }
  return result
}

function CategoryPicker({
  open,
  categories,
  loading,
  error,
  selectedId,
  onClose,
  onRetry,
  onSelect,
}: {
  open: boolean
  categories: CategoryChoice[]
  loading: boolean
  error: string | null
  selectedId: number
  onClose(): void
  onRetry(): void
  onSelect(category: CategoryChoice): void
}) {
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    if (!needle) return categories
    return categories.filter((category) =>
      `${category.id} ${category.label} ${category.alias ?? ''}`.toLocaleLowerCase('ru-RU').includes(needle),
    )
  }, [categories, query])
  const visible = filtered.slice(0, 200)

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4" role="presentation" onClick={onClose}>
      <section
        aria-labelledby="category-picker-title"
        aria-modal="true"
        className="w-full max-w-3xl overflow-hidden rounded-lg border border-surface-600 bg-surface-900 shadow-2xl"
        role="dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-surface-700 px-5 py-4">
          <div>
            <h2 id="category-picker-title" className="text-base font-semibold text-white">Выберите категорию</h2>
            <p className="mt-1 text-xs text-zinc-500">ID и название подставятся в задачу автоматически.</p>
          </div>
          <button type="button" title="Закрыть" aria-label="Закрыть" className="btn btn-ghost h-8 w-8 justify-center px-0" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="border-b border-surface-700 px-5 py-3">
          <label className="relative block">
            <span className="sr-only">Поиск категории</span>
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
            <input
              autoFocus
              className="input pl-9"
              placeholder="Введите название, ID или алиас"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>

        <div className="max-h-[min(480px,calc(100vh-16rem))] overflow-y-auto p-2">
          {loading && <p className="px-3 py-8 text-center text-sm text-zinc-500">Загружаем категории Kwork...</p>}
          {!loading && error && (
            <div className="px-3 py-8 text-center">
              <p className="text-sm text-red-300">Не удалось загрузить категории.</p>
              <button type="button" className="btn btn-ghost mt-3 text-xs" onClick={onRetry}>Повторить</button>
            </div>
          )}
          {!loading && !error && !visible.length && <p className="px-3 py-8 text-center text-sm text-zinc-500">Категории не найдены.</p>}
          {!loading && !error && visible.map((category) => (
            <button
              key={category.id}
              type="button"
              aria-pressed={category.id === selectedId}
              className={`flex w-full items-center justify-between gap-4 border-b border-surface-800 px-3 py-3 text-left transition-colors hover:bg-surface-800/80 ${category.id === selectedId ? 'bg-brand-600/15' : ''}`}
              onClick={() => onSelect(category)}
            >
              <span className="min-w-0">
                <span className="block truncate text-sm text-zinc-100">{category.label}</span>
                {category.alias && <span className="mt-0.5 block truncate font-mono text-[11px] text-zinc-500">Алиас: {category.alias}</span>}
              </span>
              <span className="shrink-0 font-mono text-xs text-zinc-400">ID {category.id}</span>
            </button>
          ))}
        </div>
        {!loading && !error && filtered.length > visible.length && (
          <p className="border-t border-surface-700 px-5 py-3 text-center text-xs text-zinc-500">Показаны первые {visible.length} категорий. Уточните поиск.</p>
        )}
      </section>
    </div>
  )
}

function CatalogAliasPicker({
  open,
  aliases,
  categoryName,
  loading,
  error,
  selectedAlias,
  onClose,
  onRetry,
  onSelect,
}: {
  open: boolean
  aliases: KworkCatalogAlias[]
  categoryName: string
  loading: boolean
  error: string | null
  selectedAlias: string
  onClose(): void
  onRetry(): void
  onSelect(alias: KworkCatalogAlias): void
}) {
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    if (!needle) return aliases
    return aliases.filter((item) => `${item.label} ${item.alias}`.toLocaleLowerCase('ru-RU').includes(needle))
  }, [aliases, query])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4" role="presentation" onClick={onClose}>
      <section
        aria-labelledby="catalog-alias-picker-title"
        aria-modal="true"
        className="w-full max-w-2xl overflow-hidden rounded-lg border border-surface-600 bg-surface-900 shadow-2xl"
        role="dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-surface-700 px-5 py-4">
          <div>
            <h2 id="catalog-alias-picker-title" className="text-base font-semibold text-white">Выберите раздел веб-каталога</h2>
            <p className="mt-1 text-xs text-zinc-500">{categoryName || 'Выбранная категория'}: выберите подходящий раздел Kwork.</p>
          </div>
          <button type="button" title="Закрыть" aria-label="Закрыть" className="btn btn-ghost h-8 w-8 justify-center px-0" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="border-b border-surface-700 px-5 py-3">
          <label className="relative block">
            <span className="sr-only">Поиск раздела веб-каталога</span>
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
            <input
              autoFocus
              className="input pl-9"
              placeholder="Введите название раздела или алиас"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>

        <div className="max-h-[min(480px,calc(100vh-16rem))] overflow-y-auto p-2">
          {loading && <p className="px-3 py-8 text-center text-sm text-zinc-500">Загружаем разделы веб-каталога...</p>}
          {!loading && error && (
            <div className="px-3 py-8 text-center">
              <p className="text-sm text-red-300">Не удалось загрузить разделы веб-каталога.</p>
              <button type="button" className="btn btn-ghost mt-3 text-xs" onClick={onRetry}>Повторить</button>
            </div>
          )}
          {!loading && !error && !filtered.length && <p className="px-3 py-8 text-center text-sm text-zinc-500">Для этой категории разделы не найдены.</p>}
          {!loading && !error && filtered.map((item) => (
            <button
              key={item.alias}
              type="button"
              aria-pressed={item.alias === selectedAlias}
              className={`flex w-full items-center justify-between gap-4 border-b border-surface-800 px-3 py-3 text-left transition-colors hover:bg-surface-800/80 ${item.alias === selectedAlias ? 'bg-brand-600/15' : ''}`}
              onClick={() => onSelect(item)}
            >
              <span className="min-w-0">
                <span className="block truncate text-sm text-zinc-100">{item.label}</span>
                <span className="mt-0.5 block truncate font-mono text-[11px] text-zinc-500">{item.alias}</span>
              </span>
              {item.recommended && <span className="shrink-0 text-[11px] text-emerald-300">рекомендуется</span>}
            </button>
          ))}
        </div>
      </section>
    </div>
  )
}

export default function KworkMarketWorkspace() {
  const navigate = useNavigate()
  const { jobs, state, error, hasMore, loadMore, refresh } = useMarketJobs({ limit: 50 })
  const { data: categoryData, loading: categoriesLoading, error: categoriesError, refetch: refetchCategories } = useApi(
    () => api.getKworkMarketCategories(),
    [],
    { categories: [] },
  )
  const { data: catalogAliasesData, loading: catalogAliasesLoading, error: catalogAliasesError, refetch: refetchCatalogAliases } = useApi(
    () => api.getKworkCatalogAliases(),
    [],
    { schema_version: 1, items: [], total: 0 },
  )
  const [form, setForm] = useState<CreateMarketJobPayload>(DEFAULT_FORM)
  const [creating, setCreating] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [categoryPickerOpen, setCategoryPickerOpen] = useState(false)
  const [catalogAliasPickerOpen, setCatalogAliasPickerOpen] = useState(false)
  const isMobileFirstPage = form.source_policy === 'mobile_first_page_only'
  const sourceLabel = isMobileFirstPage ? 'только 1-я мобильная страница' : 'проверенный веб-каталог'
  const activeJobs = useMemo(() => jobs.filter((job) => !['completed', 'stopped', 'failed'].includes(job.state)), [jobs])
  const categories = useMemo(() => flattenCategories(categoryData?.categories || []), [categoryData])
  const selectedCategory = useMemo(
    () => categories.find((category) => category.id === form.scope.category_id),
    [categories, form.scope.category_id],
  )
  const catalogAliases = catalogAliasesData?.items || []
  const categoryCatalogAliases = useMemo(
    () => catalogAliases.filter((item) => item.category_id === form.scope.category_id),
    [catalogAliases, form.scope.category_id],
  )
  const recommendedCatalogAlias = useMemo(
    () => categoryCatalogAliases.find((item) => item.recommended) ?? categoryCatalogAliases[0],
    [categoryCatalogAliases],
  )
  const selectedCatalogAlias = useMemo(
    () => categoryCatalogAliases.find((item) => item.alias === form.scope.canonical_alias) ?? null,
    [categoryCatalogAliases, form.scope.canonical_alias],
  )

  useEffect(() => {
    if (!form.scope.category_id || form.scope.canonical_alias || !recommendedCatalogAlias) return
    setForm((current) => (
      current.scope.category_id === form.scope.category_id && !current.scope.canonical_alias
        ? { ...current, scope: { ...current.scope, canonical_alias: recommendedCatalogAlias.alias } }
        : current
    ))
  }, [form.scope.canonical_alias, form.scope.category_id, recommendedCatalogAlias])

  function chooseCategory(category: CategoryChoice) {
    setForm((current) => ({
      ...current,
      scope: {
        ...current.scope,
        category_id: category.id,
        category_name: category.name,
        canonical_alias: catalogAliases.find((item) => item.category_id === category.id && item.recommended)?.alias ?? '',
      },
    }))
    setCategoryPickerOpen(false)
    setMessage(null)
  }

  function chooseCatalogAlias(alias: KworkCatalogAlias) {
    setForm((current) => ({ ...current, scope: { ...current.scope, canonical_alias: alias.alias } }))
    setCatalogAliasPickerOpen(false)
    setMessage(null)
  }

  function chooseProfile(profile: string) {
    const preset = PROFILE_PRESETS[profile]
    setForm((current) => ({
      ...current,
      profile,
      target_unique_cards: preset?.targetUniqueCards ?? current.target_unique_cards,
      desired_workers: preset?.desiredWorkers ?? current.desired_workers,
    }))
  }

  async function createJob() {
    if (!form.scope.category_id || !form.scope.category_name?.trim()) {
      setMessage('Сначала выберите категорию.')
      return
    }
    if (!isMobileFirstPage && !form.scope.canonical_alias?.trim()) {
      setMessage('Выберите раздел веб-каталога для выбранной категории.')
      return
    }
    setCreating(true)
    setMessage(null)
    try {
      const created = await marketJobsApi.createJob({
        ...form,
        scope: { ...form.scope, canonical_alias: form.scope.canonical_alias?.trim() || null },
      })
      navigate(`/kwork-market/jobs/${created.job_id}`)
    } catch {
      setMessage('Не удалось создать задачу. Проверьте параметры и подключение к серверу.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <main className="space-y-5 p-6 max-lg:p-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="page-kicker">Рынок Kwork</div>
          <h1 className="text-xl font-semibold text-white">Задачи сбора</h1>
          <p className="mt-1 text-sm text-zinc-500">Сбор карточек с сохранением результатов и проверкой источника.</p>
        </div>
        <div className="flex items-center gap-2"><Link to="/kwork-market/legacy" className="btn btn-ghost text-xs"><ExternalLink className="h-3.5 w-3.5" />Предыдущий анализ</Link><button type="button" title="Обновить задачи" aria-label="Обновить задачи" onClick={() => void refresh()} className="btn btn-ghost h-8 w-8 justify-center px-0"><RefreshCw className="h-4 w-4" /></button></div>
      </header>

      <section className="factory-panel p-4">
        <div className="flex items-center justify-between gap-3"><h2 className="text-sm font-medium text-white">Новый сбор</h2><span className="mono-label">{sourceLabel}</span></div>
        <div className="mt-4 grid grid-cols-6 gap-3 max-2xl:grid-cols-3 max-md:grid-cols-1">
          <label><span className="label">ID категории</span><button type="button" className="input flex w-full items-center justify-between gap-2 text-left" onClick={() => setCategoryPickerOpen(true)}><span className={form.scope.category_id ? 'text-zinc-100' : 'text-zinc-500'}>{form.scope.category_id || 'Выбрать'}</span><ChevronDown className="h-4 w-4 shrink-0 text-zinc-500" /></button></label>
          <label><span className="label">Название категории</span><button type="button" className="input flex w-full items-center justify-between gap-2 text-left" onClick={() => setCategoryPickerOpen(true)}><span className={selectedCategory || form.scope.category_name ? 'truncate text-zinc-100' : 'truncate text-zinc-500'}>{selectedCategory?.label || form.scope.category_name || 'Выберите из списка'}</span><ChevronDown className="h-4 w-4 shrink-0 text-zinc-500" /></button></label>
          <label><span className="label">Раздел веб-каталога</span><button type="button" className="input flex w-full items-center justify-between gap-2 text-left disabled:cursor-not-allowed" disabled={isMobileFirstPage || !form.scope.category_id || catalogAliasesLoading} onClick={() => setCatalogAliasPickerOpen(true)}><span className="min-w-0"><span className={selectedCatalogAlias || form.scope.canonical_alias ? 'block truncate text-zinc-100' : 'block truncate text-zinc-500'}>{isMobileFirstPage ? 'Не требуется' : catalogAliasesLoading ? 'Загрузка...' : selectedCatalogAlias?.label || form.scope.canonical_alias || 'Выберите из списка'}</span>{!isMobileFirstPage && (selectedCatalogAlias || form.scope.canonical_alias) && <span className="mt-0.5 block truncate font-mono text-[11px] text-zinc-500">{selectedCatalogAlias?.alias || form.scope.canonical_alias}</span>}</span><ChevronDown className="h-4 w-4 shrink-0 text-zinc-500" /></button></label>
          <label><span className="label">Профиль</span><select className="input" value={form.profile} onChange={(event) => chooseProfile(event.target.value)}><option value="working">Рабочий (60 / 2)</option><option value="deep">Глубокий (500 / 3)</option><option value="extended">Расширенный (2 000 / 4)</option><option value="custom">Пользовательский</option></select></label>
          <label><span className="label">Цель, уникальных карточек</span><input className="input" type="number" min="1" max="10000" value={form.target_unique_cards} onChange={(event) => setForm((current) => ({ ...current, target_unique_cards: Math.max(1, Number(event.target.value) || 1) }))} /></label>
          <label><span className="label">Исполнители</span><input className="input" type="number" min="1" max="10" value={form.desired_workers} onChange={(event) => setForm((current) => ({ ...current, desired_workers: Math.min(10, Math.max(1, Number(event.target.value) || 1)) }))} /></label>
          <label><span className="label">Маршрут сети</span><select className="input" value={form.network_policy} onChange={(event) => setForm((current) => ({ ...current, network_policy: event.target.value as MarketNetworkPolicy }))}><option value="prefer_vpnte">Предпочитать VPNTE</option><option value="vpnte_only">Только VPNTE</option><option value="direct_only">Только прямое подключение</option><option value="explicit_pool">Выбранный пул</option></select></label>
          <label><span className="label">Источник</span><select className="input" value={form.source_policy} onChange={(event) => setForm((current) => ({ ...current, source_policy: event.target.value as MarketSourcePolicy }))}><option value="validated_only">Проверенный веб-каталог</option><option value="mobile_first_page_only">Только первая мобильная страница</option></select></label>
          <label><span className="label">Лимит запросов</span><input className="input" type="number" min="1" placeholder="Автоматически" value={form.request_budget ?? ''} onChange={(event) => setForm((current) => ({ ...current, request_budget: event.target.value ? Math.max(1, Number(event.target.value)) : null }))} /></label>
          <label><span className="label">Лимит времени, с</span><input className="input" type="number" min="1" placeholder="Автоматически" value={form.time_budget_seconds ?? ''} onChange={(event) => setForm((current) => ({ ...current, time_budget_seconds: event.target.value ? Math.max(1, Number(event.target.value)) : null }))} /></label>
          <label className="flex h-full items-end gap-2 pb-2 text-xs text-zinc-400"><input type="checkbox" checked={form.include_ai ?? true} onChange={(event) => setForm((current) => ({ ...current, include_ai: event.target.checked }))} />Добавить AI-обоснование</label>
          <div className="flex items-end"><button type="button" className="btn-primary w-full justify-center" disabled={creating} onClick={() => void createJob()}><Plus className="h-4 w-4" />{creating ? 'Создание...' : 'Создать задачу'}</button></div>
        </div>
        {message && <p className="mt-3 text-xs text-red-300">{message}</p>}
      </section>

      <section className="factory-panel overflow-hidden">
        <div className="table-head flex items-center justify-between px-4 py-3"><div><h2 className="text-sm font-medium text-white">Задачи</h2><p className="mt-1 text-xs text-zinc-500">Активных: {activeJobs.length}, загружено: {jobs.length}</p></div><span className="mono-label">{JOBS_LOAD_STATE_LABEL[state]}</span></div>
        <div className="overflow-x-auto"><table className="w-full min-w-[780px] text-left text-xs"><thead className="text-zinc-500"><tr><th className="px-4 py-2 font-medium">Категория</th><th className="px-3 py-2 font-medium">Состояние</th><th className="px-3 py-2 text-right font-medium">Уникальные / цель</th><th className="px-3 py-2 text-right font-medium">Исполнители</th><th className="px-3 py-2 font-medium">Обновлено</th><th className="px-3 py-2 text-right font-medium">Открыть</th></tr></thead><tbody>{jobs.map((job) => <tr key={job.job_id} className="border-t border-surface-700/60 hover:bg-surface-800/45"><td className="px-4 py-2"><div className="font-medium text-zinc-200">{job.scope.category_name || job.scope.canonical_alias || `Категория ${job.scope.category_id}`}</div><div className="mt-0.5 font-mono text-zinc-600">{job.job_id}</div></td><td className="px-3 py-2"><MarketStateBadge state={job.state} /></td><td className="px-3 py-2 text-right text-zinc-300">{formatCount(job.counters.unique_cards ?? job.counters.unique_listings ?? 0)} / {formatCount(job.target_unique_cards)}</td><td className="px-3 py-2 text-right text-zinc-400">{job.desired_workers}</td><td className="px-3 py-2 text-zinc-500">{formatTimestamp(job.finished_at ?? job.started_at ?? job.created_at)}</td><td className="px-3 py-2 text-right"><Link title="Открыть задачу" aria-label="Открыть задачу" to={`/kwork-market/jobs/${job.job_id}`} className="btn btn-ghost h-8 w-8 justify-center px-0"><ArrowRight className="h-4 w-4" /></Link></td></tr>)}{!jobs.length && <tr><td colSpan={6} className="px-4 py-10 text-center text-zinc-500">{error ? 'Не удалось загрузить задачи.' : 'Задач сбора пока нет.'}</td></tr>}</tbody></table></div>
        {hasMore && <div className="border-t border-surface-700/60 px-4 py-2 text-right"><button type="button" className="btn btn-ghost text-xs" onClick={() => void loadMore()}>Загрузить ещё</button></div>}
      </section>

      <CategoryPicker
        open={categoryPickerOpen}
        categories={categories}
        loading={categoriesLoading}
        error={categoriesError}
        selectedId={form.scope.category_id}
        onClose={() => setCategoryPickerOpen(false)}
        onRetry={() => void refetchCategories()}
        onSelect={chooseCategory}
      />
      <CatalogAliasPicker
        open={catalogAliasPickerOpen}
        aliases={categoryCatalogAliases}
        categoryName={selectedCategory?.label || form.scope.category_name || ''}
        loading={catalogAliasesLoading}
        error={catalogAliasesError}
        selectedAlias={form.scope.canonical_alias || ''}
        onClose={() => setCatalogAliasPickerOpen(false)}
        onRetry={() => void refetchCatalogAliases()}
        onSelect={chooseCatalogAlias}
      />
    </main>
  )
}
