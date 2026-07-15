import { useMemo, useState } from 'react'
import { Ban, RefreshCw, RotateCw, Search, Wifi, Wind } from 'lucide-react'

import type { MarketTransport, MarketWorker } from '../types'
import { IconButton, formatTimestamp, formatTransportProxyRoute, workerDesiredStateLabel, workerStateLabel } from './shared'

type WorkerCommand = 'drain' | 'restart' | 'disable' | 'rotate' | 'reconnect'
type WorkerFilter = 'all' | 'active' | 'issues' | 'stopped'

interface WorkerTableProps {
  workers: MarketWorker[]
  transports: MarketTransport[]
  onCommand(workerId: string, command: WorkerCommand): void
}

function transportProfile(transport: MarketTransport): string {
  if (transport.profile_name && transport.profile_id) return `${transport.profile_name} (${transport.profile_id})`
  return transport.profile_name ?? transport.profile_id ?? '-'
}

function matchesWorkerFilter(worker: MarketWorker, filter: WorkerFilter): boolean {
  if (filter === 'active') return ['starting', 'connecting', 'leasing', 'busy', 'cooldown', 'draining'].includes(worker.actual_state)
  if (filter === 'issues') return ['backoff', 'blocked', 'crashed'].includes(worker.actual_state) || Boolean(worker.last_error)
  if (filter === 'stopped') return worker.actual_state === 'stopped'
  return true
}

export function WorkerTable({ workers, transports, onCommand }: WorkerTableProps) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<WorkerFilter>('all')
  const transportById = useMemo(() => new Map(transports.map((transport) => [transport.transport_id, transport])), [transports])
  const visibleWorkers = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    return workers.filter((worker) => {
      if (!matchesWorkerFilter(worker, filter)) return false
      if (!needle) return true
      const transport = worker.transport_id ? transportById.get(worker.transport_id) : undefined
      return `${worker.worker_id} ${worker.actual_state} ${worker.transport_id ?? ''} ${transport?.proxy_url ?? ''}`.toLocaleLowerCase('ru-RU').includes(needle)
    })
  }, [filter, query, transportById, workers])

  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div><div className="market-section-label">Runtime</div><h2 className="mt-1 text-base font-semibold text-white">Исполнители</h2></div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="market-search-field"><Search className="h-3.5 w-3.5" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Worker, IP или маршрут" /></label>
          <select className="market-plain-select" value={filter} onChange={(event) => setFilter(event.target.value as WorkerFilter)}>
            <option value="all">Все ({workers.length})</option>
            <option value="active">Активные</option>
            <option value="issues">С проблемами</option>
            <option value="stopped">Остановленные</option>
          </select>
        </div>
      </div>
      <div className="max-h-[34rem] overflow-auto">
        <table className="market-data-table min-w-[980px]">
          <thead><tr><th>Исполнитель</th><th>Состояние</th><th>Маршрут</th><th>Текущая задача</th><th>Последний сигнал</th><th className="text-right">Управление</th></tr></thead>
          <tbody>
            {visibleWorkers.map((worker) => {
              const transport = worker.transport_id ? transportById.get(worker.transport_id) : undefined
              return (
                <tr key={worker.worker_id}>
                  <td><div className="font-mono text-zinc-200">{worker.worker_id}</div><div className="market-row-note">generation {worker.generation}</div></td>
                  <td><div className="text-zinc-300">{workerStateLabel(worker.actual_state)}</div><div className="market-row-note">ожидается: {workerDesiredStateLabel(worker.desired_state)}</div></td>
                  <td><WorkerRoute worker={worker} transport={transport} /></td>
                  <td className="max-w-56 truncate font-mono">{worker.current_operation_id ?? '-'}</td>
                  <td>{formatTimestamp(worker.heartbeat_at)}</td>
                  <td className="text-right"><div className="inline-flex"><IconButton title="Завершить текущую задачу" onClick={() => onCommand(worker.worker_id, 'drain')}><Wind className="h-3.5 w-3.5" /></IconButton><IconButton title="Перезапустить исполнителя" onClick={() => onCommand(worker.worker_id, 'restart')}><RefreshCw className="h-3.5 w-3.5" /></IconButton><IconButton title="Переподключить исполнителя" onClick={() => onCommand(worker.worker_id, 'reconnect')}><Wifi className="h-3.5 w-3.5" /></IconButton><IconButton title="Сменить маршрут" onClick={() => onCommand(worker.worker_id, 'rotate')}><RotateCw className="h-3.5 w-3.5" /></IconButton><IconButton title="Отключить исполнителя" className="text-red-300 hover:text-red-200" onClick={() => onCommand(worker.worker_id, 'disable')}><Ban className="h-3.5 w-3.5" /></IconButton></div></td>
                </tr>
              )
            })}
            {!visibleWorkers.length && <tr><td colSpan={6}><div className="market-empty-state">По выбранному фильтру исполнителей нет.</div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="market-table-footer"><span>Показано {visibleWorkers.length} из {workers.length}</span></div>
    </section>
  )
}

function WorkerRoute({ worker, transport }: { worker: MarketWorker; transport: MarketTransport | undefined }) {
  if (!worker.transport_id) return <span className="text-zinc-500">Маршрут освобождён</span>
  if (!transport) return <span className="break-all font-mono text-zinc-400">{worker.transport_id}</span>
  return (
    <div className="min-w-56">
      <div className="font-mono text-zinc-200">{transport.transport_id}</div>
      <div className="market-row-note break-all font-mono">{formatTransportProxyRoute(transport.proxy_url)}</div>
      <div className="market-row-note">{transportProfile(transport)} · {transport.country ?? '-'}</div>
    </div>
  )
}
