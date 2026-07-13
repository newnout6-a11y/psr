import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { AttemptEvidencePanel } from '../features/kwork-market/components/AttemptEvidencePanel'
import { EventTimeline } from '../features/kwork-market/components/EventTimeline'
import { JobAssistantPanel } from '../features/kwork-market/components/JobAssistantPanel'
import { JobConfigControls, jobConfigDraftFromJob, type JobConfigDraft } from '../features/kwork-market/components/JobConfigControls'
import { JobMetrics } from '../features/kwork-market/components/JobMetrics'
import { JobToolbar } from '../features/kwork-market/components/JobToolbar'
import { ListingsTable } from '../features/kwork-market/components/ListingsTable'
import { OperationTable } from '../features/kwork-market/components/OperationTable'
import { ResultsPanel } from '../features/kwork-market/components/ResultsPanel'
import { ShardProgressTable } from '../features/kwork-market/components/ShardProgressTable'
import { WorkerTable } from '../features/kwork-market/components/WorkerTable'
import { formatTransportProxyRoute, transportHealthLabel } from '../features/kwork-market/components/shared'
import { marketJobsApi } from '../features/kwork-market/api'
import { useMarketJobStream } from '../features/kwork-market/hooks/useMarketJobStream'
import type {
  MarketJobSnapshot,
  MarketJob,
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

export default function KworkMarketJob() {
  const { jobId } = useParams<{ jobId: string }>()
  const [initialSnapshot, setInitialSnapshot] = useState<MarketJobSnapshot | null>(null)
  const [operations, setOperations] = useState<MarketOperation[]>([])
  const [shards, setShards] = useState<MarketShard[]>([])
  const [workers, setWorkers] = useState<MarketWorker[]>([])
  const [transports, setTransports] = useState<MarketTransport[]>([])
  const [listingPage, setListingPage] = useState<MarketListingPage>({ items: [] })
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

  const refresh = useCallback(async ({ background = false }: RefreshOptions = {}) => {
    if (!jobId) return
    const requestId = refreshRequestId.current + 1
    refreshRequestId.current = requestId
    if (!background) setLoading(true)
    const responses = await Promise.allSettled([
      marketJobsApi.getJob(jobId),
      marketJobsApi.getOperations(jobId, { limit: 100 }),
      marketJobsApi.getShards(jobId, { limit: 100 }),
      marketJobsApi.getWorkers(jobId, { limit: 100 }),
      marketJobsApi.getTransports(jobId, { limit: 100 }),
      marketJobsApi.getListings(jobId, { limit: 100 }),
      marketJobsApi.getResults(jobId),
    ])
    if (requestId !== refreshRequestId.current) {
      if (!background) setLoading(false)
      return
    }

    const [snapshotResponse, operationResponse, shardResponse, workerResponse, transportResponse, listingsResponse, resultsResponse] = responses
    if (snapshotResponse.status === 'fulfilled') setInitialSnapshot(snapshotResponse.value)
    if (operationResponse.status === 'fulfilled') setOperations(operationResponse.value.items)
    if (shardResponse.status === 'fulfilled') setShards(shardResponse.value.items)
    if (workerResponse.status === 'fulfilled') setWorkers(workerResponse.value.items)
    if (transportResponse.status === 'fulfilled') setTransports(transportResponse.value.items)
    if (listingsResponse.status === 'fulfilled') setListingPage(listingsResponse.value)
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
  const persistedJob = snapshot?.job
  const job = optimisticJob && (!persistedJob || optimisticJob.revision >= persistedJob.revision)
    ? optimisticJob
    : persistedJob

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => () => {
    if (backgroundRefreshTimer.current !== null) window.clearTimeout(backgroundRefreshTimer.current)
  }, [])

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

  const activeTransports = visibleTransports.filter((transport) => transport.lease_owner).length

  return (
    <main className="min-h-0">
      <JobToolbar job={job} streamState={stream.streamState} busy={actionBusy || configBusy} onRefresh={() => void refresh()} onPause={() => void invoke(() => marketJobsApi.pauseJob(jobId))} onResume={() => void invoke(() => marketJobsApi.resumeJob(jobId))} onStop={(force) => void invoke(() => marketJobsApi.stopJob(jobId, { force }))} />
      <div className="space-y-4 p-5 max-lg:p-4">
        <JobConfigControls job={job} draft={configDraft ?? jobConfigDraftFromJob(job)} dirty={configDirty} busy={configBusy || actionBusy} error={configError} onChange={updateConfigDraft} onReset={resetConfigDraft} onSubmit={() => void applyConfig()} />
        <div className="flex items-center justify-between gap-3"><Link to="/kwork-market" className="btn btn-ghost text-xs"><ArrowLeft className="h-3.5 w-3.5" />Запуски</Link><button type="button" title="Обновить сведения о запуске" aria-label="Обновить сведения о запуске" onClick={() => void refresh()} className="btn btn-ghost h-8 w-8 justify-center px-0"><RefreshCw className="h-4 w-4" /></button></div>
        {error && <div className="border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</div>}
        <JobMetrics job={job} />
        <ResultsPanel results={results} />
        <JobAssistantPanel jobId={jobId} results={results} />
        <div className="grid grid-cols-2 gap-4 max-2xl:grid-cols-1"><WorkerTable workers={visibleWorkers} transports={visibleTransports} onCommand={(workerId, command) => void invoke(() => marketJobsApi.commandWorker(jobId, workerId, command))} /><ShardProgressTable shards={visibleShards} /></div>
        <OperationTable operations={operations} onRetry={(operationId) => void invoke(() => marketJobsApi.retryOperation(jobId, operationId))} onShowAttempts={(operation) => void showOperationAttempts(operation)} />
        <div ref={attemptPanelRef}><AttemptEvidencePanel operation={evidenceOperation} attempts={operationAttempts} loading={attemptsLoading} error={attemptsError} onClose={closeOperationAttempts} /></div>
        <ListingsTable listings={listingPage.items} loading={loading} hasMore={typeof listingPage.next_cursor === 'number'} onLoadMore={() => void loadMoreListings()} />
        <div className="grid grid-cols-[minmax(0,1fr)_360px] gap-4 max-2xl:grid-cols-1"><EventTimeline events={stream.events} /><section className="factory-panel p-4"><h2 className="text-sm font-medium text-white">Маршруты и пул</h2><p className="mt-1 text-xs text-zinc-500">{visibleTransports.length ? `${visibleTransports.length} в пуле, занято: ${activeTransports}` : 'Маршрутов из управляемого пула пока нет.'}</p><div className="mt-3 space-y-2">{visibleTransports.map((transport) => <div key={transport.transport_id} className="border-b border-surface-700/60 pb-2 text-xs last:border-0"><div className="font-mono text-zinc-300">{transport.transport_id}</div><div className="mt-1 break-all font-mono text-zinc-500">{transportHealthLabel(transport.health)} / {formatTransportProxyRoute(transport.proxy_url)}</div><div className="mt-1 text-zinc-500">{[transport.profile_name, transport.profile_id].filter(Boolean).join(' / ') || 'профиль не указан'} / {transport.country ?? 'страна не указана'} / {transport.lease_owner ? `занят: ${transport.lease_owner}` : 'свободен'}</div></div>)}{!visibleTransports.length && <p className="text-xs text-zinc-500">При прямом подключении маршрут не назначается.</p>}</div></section></div>
      </div>
    </main>
  )
}
