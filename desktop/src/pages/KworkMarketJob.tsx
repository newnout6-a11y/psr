import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, ArrowLeft, BarChart3, Database, LayoutDashboard, Send } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { AttemptEvidencePanel } from '../features/kwork-market/components/AttemptEvidencePanel'
import { EventTimeline } from '../features/kwork-market/components/EventTimeline'
import { FleetPanel } from '../features/kwork-market/components/FleetPanel'
import { JobAssistantPanel } from '../features/kwork-market/components/JobAssistantPanel'
import { JobConfigControls, jobConfigDraftFromJob, type JobConfigDraft } from '../features/kwork-market/components/JobConfigControls'
import { JobOverviewPanel } from '../features/kwork-market/components/JobOverviewPanel'
import { JobToolbar } from '../features/kwork-market/components/JobToolbar'
import { ListingsTable } from '../features/kwork-market/components/ListingsTable'
import { OperationTable } from '../features/kwork-market/components/OperationTable'
import { PublicationWorkspace } from '../features/kwork-market/components/PublicationWorkspace'
import { ResultsPanel } from '../features/kwork-market/components/ResultsPanel'
import { ShardProgressTable } from '../features/kwork-market/components/ShardProgressTable'
import { TransportPanel } from '../features/kwork-market/components/TransportPanel'
import { WorkerTable } from '../features/kwork-market/components/WorkerTable'
import { marketJobsApi } from '../features/kwork-market/api'
import { useMarketJobStream } from '../features/kwork-market/hooks/useMarketJobStream'
import { marketResultModel, type JsonRecord } from '../features/kwork-market/resultModel'
import type {
  MarketJobSnapshot,
  MarketJob,
  MarketJobEvent,
  MarketListingPage,
  MarketOperation,
  MarketOperationAttempt,
  MarketResults,
  MarketShard,
  MarketTransport,
  MarketWorker,
  UpdateMarketJobPayload,
} from '../features/kwork-market/types'

function parseBoundedInteger(value: string, label: string, minimum: number, maximum?: number): number {
  const parsed = Number(value.trim())
  if (!Number.isInteger(parsed) || parsed < minimum || (maximum !== undefined && parsed > maximum)) {
    const bounds = maximum === undefined ? `не меньше ${minimum}` : `от ${minimum} до ${maximum}`
    throw new Error(`${label}: целое число ${bounds}.`)
  }
  return parsed
}

function parseOptionalPositiveInteger(value: string, label: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value.trim())
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error(`${label}: положительное целое число или пустое поле.`)
  return parsed
}

type RefreshOptions = { background?: boolean }
type JobView = 'overview' | 'insights' | 'publication' | 'data' | 'execution'

export default function KworkMarketJob() {
  const { jobId } = useParams<{ jobId: string }>()
  const [initialSnapshot, setInitialSnapshot] = useState<MarketJobSnapshot | null>(null)
  const [operations, setOperations] = useState<MarketOperation[]>([])
  const [shards, setShards] = useState<MarketShard[]>([])
  const [workers, setWorkers] = useState<MarketWorker[]>([])
  const [transports, setTransports] = useState<MarketTransport[]>([])
  const [listingPage, setListingPage] = useState<MarketListingPage>({ items: [] })
  const [durableEvents, setDurableEvents] = useState<MarketJobEvent[]>([])
  const [results, setResults] = useState<MarketResults | null>(null)
  const [configDraft, setConfigDraft] = useState<JobConfigDraft | null>(null)
  const [configDirty, setConfigDirty] = useState(false)
  const [configBusy, setConfigBusy] = useState(false)
  const [configError, setConfigError] = useState<string | null>(null)
  const [optimisticJob, setOptimisticJob] = useState<MarketJob | null>(null)
  const [evidenceOperation, setEvidenceOperation] = useState<MarketOperation | null>(null)
  const [operationAttempts, setOperationAttempts] = useState<MarketOperationAttempt[]>([])
  const [attemptsLoading, setAttemptsLoading] = useState(false)
  const [attemptsError, setAttemptsError] = useState<string | null>(null)
  const attemptRequestId = useRef(0)
  const refreshRequestId = useRef(0)
  const backgroundRefreshTimer = useRef<number | null>(null)
  const attemptPanelRef = useRef<HTMLDivElement | null>(null)
  const [loading, setLoading] = useState(true)
  const [actionBusy, setActionBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [publicationOpportunity, setPublicationOpportunity] = useState<JsonRecord | null>(null)
  const [activeView, setActiveView] = useState<JobView>(() => {
    const saved = window.sessionStorage.getItem(`psr:kwork-market:view:${jobId ?? ''}`)
    return ['overview', 'insights', 'publication', 'data', 'execution'].includes(saved ?? '') ? saved as JobView : 'overview'
  })

  const refresh = useCallback(async ({ background = false }: RefreshOptions = {}) => {
    if (!jobId) return
    const requestId = refreshRequestId.current + 1
    refreshRequestId.current = requestId
    if (!background) setLoading(true)
    const responses = await Promise.allSettled([
      marketJobsApi.getJob(jobId),
      marketJobsApi.getOperations(jobId, { limit: 100 }),
      marketJobsApi.getShards(jobId, { limit: 100 }),
      marketJobsApi.getWorkers(jobId, { limit: 1000 }),
      marketJobsApi.getTransports(jobId, { limit: 100 }),
      marketJobsApi.getListings(jobId, { limit: 100 }),
      marketJobsApi.getEvents(jobId, { limit: 1000, tail: true }),
      marketJobsApi.getResults(jobId),
    ])
    if (requestId !== refreshRequestId.current) {
      if (!background) setLoading(false)
      return
    }

    const [snapshotResponse, operationResponse, shardResponse, workerResponse, transportResponse, listingsResponse, eventsResponse, resultsResponse] = responses
    if (snapshotResponse.status === 'fulfilled') setInitialSnapshot(snapshotResponse.value)
    if (operationResponse.status === 'fulfilled') setOperations(operationResponse.value.items)
    if (shardResponse.status === 'fulfilled') setShards(shardResponse.value.items)
    if (workerResponse.status === 'fulfilled') setWorkers(workerResponse.value.items)
    if (transportResponse.status === 'fulfilled') setTransports(transportResponse.value.items)
    if (listingsResponse.status === 'fulfilled') setListingPage(listingsResponse.value)
    if (eventsResponse.status === 'fulfilled') setDurableEvents(eventsResponse.value.items)
    if (resultsResponse.status === 'fulfilled') setResults(resultsResponse.value)

    const failure = responses.find((response) => response.status === 'rejected')
    if (failure?.status === 'rejected') {
      setError(failure.reason instanceof Error ? failure.reason.message : String(failure.reason))
    } else {
      setError(null)
    }
    if (!background) setLoading(false)
  }, [jobId])

  const scheduleBackgroundRefresh = useCallback(() => {
    if (backgroundRefreshTimer.current !== null) return
    backgroundRefreshTimer.current = window.setTimeout(() => {
      backgroundRefreshTimer.current = null
      void refresh({ background: true })
    }, 350)
  }, [refresh])

  const stream = useMarketJobStream({
    jobId,
    onSnapshot: setInitialSnapshot,
    onEvent: (event) => {
      if (['job.state_changed', 'job.phase_changed', 'job.metrics', 'operation.queued', 'operation.completed', 'operation.failed', 'shard.progress', 'transport.state_changed', 'result.ready', 'warning'].includes(event.type)) {
        scheduleBackgroundRefresh()
      }
    },
  })
  const snapshot = initialSnapshot && (!stream.snapshot || initialSnapshot.job.revision >= stream.snapshot.job.revision)
    ? initialSnapshot
    : stream.snapshot
  const visibleWorkers = workers.length ? workers : snapshot?.workers ?? []
  const visibleShards = shards.length ? shards : snapshot?.shards ?? []
  const visibleTransports = transports.length ? transports : snapshot?.transports ?? []
  const visibleEvents = useMemo(() => {
    const merged = new Map<number, MarketJobEvent>()
    for (const event of durableEvents) merged.set(event.seq, event)
    for (const event of stream.events) merged.set(event.seq, event)
    return [...merged.values()].sort((left, right) => left.seq - right.seq)
  }, [durableEvents, stream.events])
  const persistedJob = snapshot?.job
  const job = optimisticJob && (!persistedJob || optimisticJob.revision >= persistedJob.revision)
    ? optimisticJob
    : persistedJob

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => () => {
    if (backgroundRefreshTimer.current !== null) window.clearTimeout(backgroundRefreshTimer.current)
  }, [])

  useEffect(() => {
    if (jobId) window.sessionStorage.setItem(`psr:kwork-market:view:${jobId}`, activeView)
  }, [activeView, jobId])

  useEffect(() => {
    if (!job || configBusy || configDirty) return
    setConfigDraft(jobConfigDraftFromJob(job))
  }, [configBusy, configDirty, job?.job_id, job?.revision])

  useEffect(() => {
    if (evidenceOperation && !attemptsLoading) attemptPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [attemptsLoading, evidenceOperation])

  async function invoke(action: () => Promise<unknown>) {
    setActionBusy(true)
    try {
      await action()
      await refresh()
      scheduleBackgroundRefresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setActionBusy(false)
    }
  }

  async function loadMoreListings() {
    if (!jobId || typeof listingPage.next_cursor !== 'number') return
    try {
      const page = await marketJobsApi.getListings(jobId, { cursor: listingPage.next_cursor, limit: 100 })
      setListingPage((current) => ({ ...page, items: [...current.items, ...page.items] }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const updateConfigDraft = useCallback((field: keyof JobConfigDraft, value: string) => {
    if (!job) return
    setConfigDraft((current) => ({ ...(current ?? jobConfigDraftFromJob(job)), [field]: value }))
    setConfigDirty(true)
    setConfigError(null)
  }, [job])

  const resetConfigDraft = useCallback(() => {
    if (!job) return
    setConfigDraft(jobConfigDraftFromJob(job))
    setConfigDirty(false)
    setConfigError(null)
  }, [job])

  const applyConfig = useCallback(async () => {
    if (!jobId || !job || !configDraft || configBusy) return
    try {
      const targetUniqueCards = parseBoundedInteger(configDraft.targetUniqueCards, 'Цель', 1, 10_000)
      const desiredWorkers = parseBoundedInteger(configDraft.desiredWorkers, 'Исполнители', 1)
      const profile = configDraft.profile.trim()
      if (!profile) throw new Error('Профиль не может быть пустым.')
      const requestBudget = parseOptionalPositiveInteger(configDraft.requestBudget, 'Лимит запросов')
      const timeBudgetSeconds = parseOptionalPositiveInteger(configDraft.timeBudgetSeconds, 'Лимит времени')
      const jobPatch: UpdateMarketJobPayload = {}
      if (targetUniqueCards !== job.target_unique_cards) jobPatch.target_unique_cards = targetUniqueCards
      if (profile !== job.profile) jobPatch.profile = profile
      if (requestBudget !== (job.request_budget ?? null)) jobPatch.request_budget = requestBudget
      if (timeBudgetSeconds !== (job.time_budget_seconds ?? null)) jobPatch.time_budget_seconds = timeBudgetSeconds
      const workerPoolChanged = desiredWorkers !== job.desired_workers
      const hasJobPatch = Object.keys(jobPatch).length > 0
      if (!hasJobPatch && !workerPoolChanged) {
        setConfigDirty(false)
        return
      }

      const expectedRevision = job.revision
      const finalRevision = expectedRevision + Number(hasJobPatch) + Number(workerPoolChanged)
      setConfigBusy(true)
      setConfigError(null)
      setOptimisticJob({
        ...job,
        ...jobPatch,
        desired_workers: desiredWorkers,
        revision: finalRevision,
      })

      let nextRevision = expectedRevision
      if (hasJobPatch) {
        await marketJobsApi.updateJob(jobId, { ...jobPatch, expected_revision: nextRevision })
        nextRevision += 1
      }
      if (workerPoolChanged) {
        await marketJobsApi.setWorkerPool(jobId, desiredWorkers, nextRevision)
      }
      setConfigDirty(false)
      await refresh()
      setOptimisticJob(null)
    } catch (reason) {
      setOptimisticJob(null)
      setConfigError(reason instanceof Error ? reason.message : String(reason))
      await refresh()
    } finally {
      setConfigBusy(false)
    }
  }, [configBusy, configDraft, job, jobId, refresh])

  const showOperationAttempts = useCallback(async (operation: MarketOperation) => {
    if (!jobId) return
    const requestId = attemptRequestId.current + 1
    attemptRequestId.current = requestId
    setEvidenceOperation(operation)
    setOperationAttempts([])
    setAttemptsError(null)
    setAttemptsLoading(true)
    try {
      const page = await marketJobsApi.getOperationAttempts(jobId, operation.operation_id)
      if (attemptRequestId.current === requestId) setOperationAttempts(page.items)
    } catch (reason) {
      if (attemptRequestId.current === requestId) setAttemptsError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      if (attemptRequestId.current === requestId) setAttemptsLoading(false)
    }
  }, [jobId])

  const closeOperationAttempts = useCallback(() => {
    attemptRequestId.current += 1
    setEvidenceOperation(null)
    setOperationAttempts([])
    setAttemptsError(null)
    setAttemptsLoading(false)
  }, [])

  if (!jobId) return <main className="p-6 text-zinc-400">Не указан идентификатор запуска.</main>
  if (!job) return <main className="p-6"><Link to="/kwork-market" className="btn btn-ghost"><ArrowLeft className="h-4 w-4" />Запуски</Link><p className="mt-5 text-sm text-zinc-400">{loading ? 'Загружаем запуск…' : error ?? 'Запуск не найден.'}</p></main>

  const tabs: Array<{ id: JobView; label: string; note: string; icon: typeof LayoutDashboard }> = [
    { id: 'overview', label: 'Обзор', note: 'главное', icon: LayoutDashboard },
    { id: 'insights', label: 'Выводы', note: results ? 'готовы' : 'ожидаются', icon: BarChart3 },
    { id: 'publication', label: 'Публикация', note: 'карточка Kwork', icon: Send },
    { id: 'data', label: 'Данные', note: `${listingPage.items.length} карточек`, icon: Database },
    { id: 'execution', label: 'Выполнение', note: `${visibleWorkers.length} workers`, icon: Activity },
  ]

  return (
    <main className="market-job-workbench min-h-0">
      <JobToolbar job={job} streamState={stream.streamState} busy={actionBusy || configBusy} onRefresh={() => void refresh()} onPause={() => void invoke(() => marketJobsApi.pauseJob(jobId))} onResume={() => void invoke(() => marketJobsApi.resumeJob(jobId))} onStop={(force) => void invoke(() => marketJobsApi.stopJob(jobId, { force }))} />
      <nav className="market-job-nav" aria-label="Разделы запуска">
        <div className="market-job-nav-inner">
          {tabs.map(({ id, label, note, icon: Icon }) => (
            <button key={id} type="button" className={activeView === id ? 'is-active' : ''} aria-current={activeView === id ? 'page' : undefined} onClick={() => setActiveView(id)}>
              <Icon className="h-4 w-4" />
              <span><strong>{label}</strong><small>{note}</small></span>
            </button>
          ))}
        </div>
      </nav>
      <div className="market-job-content">
        {error && <div className="market-error-banner">{error}</div>}

        {activeView === 'overview' && (
          <JobOverviewPanel
            job={job}
            results={results}
            workers={visibleWorkers}
            transports={visibleTransports}
            shards={visibleShards}
            events={visibleEvents}
            onOpenInsights={() => setActiveView('insights')}
            onOpenPublication={() => {
              setPublicationOpportunity(marketResultModel(results).opportunities[0] ?? null)
              setActiveView('publication')
            }}
            onOpenExecution={() => setActiveView('execution')}
          />
        )}

        {activeView === 'insights' && (
          <div className="market-view-stack">
            <ResultsPanel
              results={results}
              onOpenPublication={(opportunity) => {
                setPublicationOpportunity(opportunity)
                setActiveView('publication')
              }}
            />
            <JobAssistantPanel jobId={jobId} results={results} />
          </div>
        )}

        {activeView === 'publication' && (
          <PublicationWorkspace
            job={job}
            results={results}
            opportunity={publicationOpportunity ?? marketResultModel(results).opportunities[0] ?? null}
          />
        )}

        {activeView === 'data' && (
          <div className="market-view-stack">
            <ListingsTable listings={listingPage.items} loading={loading} hasMore={typeof listingPage.next_cursor === 'number'} onLoadMore={() => void loadMoreListings()} />
            <ShardProgressTable shards={visibleShards} />
          </div>
        )}

        {activeView === 'execution' && (
          <div className="market-view-stack">
            <JobConfigControls job={job} draft={configDraft ?? jobConfigDraftFromJob(job)} dirty={configDirty} busy={configBusy || actionBusy} error={configError} onChange={updateConfigDraft} onReset={resetConfigDraft} onSubmit={() => void applyConfig()} />
            <FleetPanel jobId={jobId} />
            <div className="grid grid-cols-[minmax(0,1fr)_360px] gap-4 max-2xl:grid-cols-1">
              <WorkerTable workers={visibleWorkers} transports={visibleTransports} onCommand={(workerId, command) => void invoke(() => marketJobsApi.commandWorker(jobId, workerId, command))} />
              <TransportPanel transports={visibleTransports} />
            </div>
            <OperationTable operations={operations} onRetry={(operationId) => void invoke(() => marketJobsApi.retryOperation(jobId, operationId))} onShowAttempts={(operation) => void showOperationAttempts(operation)} />
            <div ref={attemptPanelRef}><AttemptEvidencePanel operation={evidenceOperation} attempts={operationAttempts} loading={attemptsLoading} error={attemptsError} onClose={closeOperationAttempts} /></div>
            <EventTimeline events={visibleEvents} job={job} transports={visibleTransports} />
          </div>
        )}
      </div>
    </main>
  )
}
