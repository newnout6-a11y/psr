import { useCallback, useEffect, useMemo, useState } from 'react'
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
  BuyerSearchQuery,
  BuyerSearchRun,
  BuyerSearchWorkspaceSection,
  CreateBuyerSearchRunPayload,
} from '../features/buyer-search/types'
import { useBuyerWorkspace } from '../features/buyer-search/useBuyerWorkspace'

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

export default function BuyerSearchWorkspace() {
  const { state, ready, saveState, updateBuilder, updateView } = useBuyerWorkspace()
  const { builder, view } = state
  const [runs, setRuns] = useState<BuyerSearchRun[]>([])
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
    setLoading(true)
    try {
      const [projectPage, queryPage, fleetState] = await Promise.all([
        buyerSearchApi.listProjects(view.selected_run_id, projectOptions),
        buyerSearchApi.listQueries(view.selected_run_id),
        buyerSearchApi.getFleet(view.selected_run_id),
      ])
      setProjects(projectPage.items)
      setNextCursor(projectPage.next_cursor)
      setProjectPageTotal(typeof projectPage.total === 'number' ? projectPage.total : null)
      setProjectRunTotal(typeof projectPage.run_total === 'number' ? projectPage.run_total : null)
      setQueries(queryPage.items)
      setFleet(fleetState)
      setError(null)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : 'Не удалось загрузить данные поиска')
    } finally {
      setLoading(false)
    }
  }, [projectOptions, view.selected_run_id])

  useEffect(() => { if (ready) void loadRuns() }, [loadRuns, ready])
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
      queries: preview.map((text) => ({ text, rationale: 'Подтверждено оператором' })),
    }
    setBusy(true)
    try {
      const created = await buyerSearchApi.createRun(payload)
      let active = created
      try { active = await buyerSearchApi.patchRun(created.run_id, { state: 'running' }) } catch { /* The run remains reviewable when capacity is unavailable. */ }
      setRuns((current) => [active, ...current.filter((run) => run.run_id !== active.run_id)])
      updateView({ active_section: 'projects', selected_run_id: active.run_id, selected_project_id: null, cursor: null, cursor_history: [] })
      setError(null)
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

  if (!ready) return <div className="buyer-workspace-loading"><Loader2 className="spin" />Восстанавливаем рабочее пространство…</div>

  return (
    <div className="buyer-workspace">
      <div className="buyer-commandbar">
        <div className="buyer-run-picker"><span className={`buyer-status-dot is-${selectedRun?.state || 'idle'}`} /><select value={view.selected_run_id || ''} onChange={(event) => selectRun(event.target.value)}><option value="">Новый поиск</option>{runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.name} · {run.state}</option>)}</select><ChevronDown /></div>
        <div className="buyer-save-status">{saveState === 'saving' ? 'Сохраняем…' : saveState === 'offline' ? 'Нет связи · сохранение ожидает' : 'Все изменения сохранены'}</div>
        <button type="button" className="buyer-new-button" onClick={() => updateView({ active_section: 'setup', selected_project_id: null })}><Plus />Новый поиск</button>
      </div>

      {error && <div className="buyer-error-banner"><span>{error}</span><button type="button" onClick={() => setError(null)}>Закрыть</button></div>}

      {section === 'setup' ? <SearchBuilder value={builder} busy={busy} onChange={updateBuilder} onCreate={createRun} /> : (
        <>
          <nav className="buyer-work-tabs">{SECTIONS.map(({ id, label, icon: Icon }) => <button type="button" key={id} className={section === id ? 'is-active' : ''} onClick={() => updateView({ active_section: id, cursor: null, cursor_history: [] })}><Icon />{label}</button>)}</nav>
          {showProjects && (
            <div className={`buyer-results-layout ${view.inspector_open ? 'has-inspector' : ''}`}>
              <ProjectFilters value={filters} onChange={(next) => updateView({ project_filters: { ...next }, cursor: null, cursor_history: [] })} />
              <main className="buyer-results-main">
                <div className="buyer-results-toolbar"><div><strong>{section === 'shortlist' ? 'Избранные проекты' : section === 'outreach' ? 'Работа с откликами' : 'Найденные проекты'}</strong><span>{projects.length} на странице{projectPageTotal != null ? ` · ${projectPageTotal} после фильтра` : ''}{projectRunTotal != null ? ` · ${projectRunTotal} в запуске` : ''}</span></div><div><select value={view.project_sort} onChange={(event) => updateView({ project_sort: event.target.value as typeof view.project_sort, cursor: null, cursor_history: [] })}><option value="score_desc">По оценке</option><option value="updated_desc">Сначала новые</option><option value="budget_desc">Бюджет выше</option><option value="offers_asc">Меньше откликов</option><option value="matched_queries_desc">Больше совпадений</option></select><button type="button" title="Обновить" onClick={() => void loadRunData()}><RefreshCw /></button><button type="button" title="Экспорт всей выдачи" onClick={exportProjects}><Download /></button></div></div>
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
