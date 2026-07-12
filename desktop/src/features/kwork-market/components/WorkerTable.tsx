import { useMemo } from 'react'
import { Ban, RefreshCw, RotateCw, Wifi, Wind } from 'lucide-react'

import type { MarketTransport, MarketWorker } from '../types'
import {
  IconButton,
  formatTimestamp,
  formatTransportProxyRoute,
  workerDesiredStateLabel,
  workerStateLabel,
} from './shared'

type WorkerCommand = 'drain' | 'restart' | 'disable' | 'rotate' | 'reconnect'

interface WorkerTableProps {
  workers: MarketWorker[]
  transports: MarketTransport[]
  onCommand(workerId: string, command: WorkerCommand): void
}

function transportProfile(transport: MarketTransport): string {
  if (transport.profile_name && transport.profile_id) return `${transport.profile_name} (${transport.profile_id})`
  return transport.profile_name ?? transport.profile_id ?? '-'
}

function WorkerRoute({ worker, transport }: { worker: MarketWorker; transport: MarketTransport | undefined }) {
  if (!worker.transport_id) return <span className="text-zinc-400">маршрут освобождён</span>
  if (!transport) {
    return <span className="break-all font-mono text-zinc-400">{worker.transport_id} (данные маршрута ещё не получены)</span>
  }
  return (
    <div className="min-w-56 space-y-0.5">
      <div className="font-mono text-zinc-200">{transport.transport_id}</div>
      <div className="break-all font-mono text-zinc-400">{formatTransportProxyRoute(transport.proxy_url)}</div>
      <div className="text-zinc-500">{transportProfile(transport)} / {transport.country ?? '-'}</div>
    </div>
  )
}

export function WorkerTable({ workers, transports, onCommand }: WorkerTableProps) {
  const transportById = useMemo(
    () => new Map(transports.map((transport) => [transport.transport_id, transport])),
    [transports],
  )

  return (
    <section className="factory-panel overflow-hidden">
      <div className="table-head flex items-center justify-between px-4 py-3">
        <h2 className="text-sm font-medium text-white">Исполнители</h2>
        <span className="mono-label">{workers.length}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-left text-xs">
          <thead className="text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Исполнитель</th>
              <th className="px-3 py-2 font-medium">Состояние</th>
              <th className="px-3 py-2 font-medium">Маршрут</th>
              <th className="px-3 py-2 font-medium">Текущая задача</th>
              <th className="px-3 py-2 font-medium">Последний сигнал</th>
              <th className="px-3 py-2 text-right font-medium">Управление</th>
            </tr>
          </thead>
          <tbody>
            {workers.map((worker) => (
              <tr key={worker.worker_id} className="border-t border-surface-700/60 hover:bg-surface-800/45">
                <td className="px-4 py-2 font-mono text-zinc-200">{worker.worker_id}<span className="ml-2 text-zinc-500">g{worker.generation}</span></td>
                <td className="px-3 py-2"><span className="text-zinc-300">{workerStateLabel(worker.actual_state)}</span><span className="ml-1 text-zinc-500">/{workerDesiredStateLabel(worker.desired_state)}</span></td>
                <td className="px-3 py-2"><WorkerRoute worker={worker} transport={worker.transport_id ? transportById.get(worker.transport_id) : undefined} /></td>
                <td className="max-w-48 truncate px-3 py-2 font-mono text-zinc-400">{worker.current_operation_id ?? '-'}</td>
                <td className="px-3 py-2 text-zinc-500">{formatTimestamp(worker.heartbeat_at)}</td>
                <td className="px-3 py-1 text-right"><div className="inline-flex"><IconButton title="Завершить текущую задачу" onClick={() => onCommand(worker.worker_id, 'drain')}><Wind className="h-3.5 w-3.5" /></IconButton><IconButton title="Перезапустить исполнителя" onClick={() => onCommand(worker.worker_id, 'restart')}><RefreshCw className="h-3.5 w-3.5" /></IconButton><IconButton title="Переподключить исполнителя" onClick={() => onCommand(worker.worker_id, 'reconnect')}><Wifi className="h-3.5 w-3.5" /></IconButton><IconButton title="Сменить маршрут" onClick={() => onCommand(worker.worker_id, 'rotate')}><RotateCw className="h-3.5 w-3.5" /></IconButton><IconButton title="Отключить исполнителя" className="text-red-300 hover:text-red-200" onClick={() => onCommand(worker.worker_id, 'disable')}><Ban className="h-3.5 w-3.5" /></IconButton></div></td>
              </tr>
            ))}
            {!workers.length && <tr><td colSpan={6} className="px-4 py-8 text-center text-zinc-500">Для этого запуска ещё нет исполнителей.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
