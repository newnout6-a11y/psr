import { RotateCcw, Search } from 'lucide-react'

import type { MarketOperation } from '../types'
import { IconButton, operationKindLabel, operationLeaseLabel, operationStateLabel } from './shared'

interface OperationTableProps {
  operations: MarketOperation[]
  onRetry(operationId: string): void
  onShowAttempts(operation: MarketOperation): void
}

export function OperationTable({ operations, onRetry, onShowAttempts }: OperationTableProps) {
  return (
    <section className="factory-panel overflow-hidden">
      <div className="table-head flex items-center justify-between px-4 py-3">
        <h2 className="text-sm font-medium text-white">Ход выполнения</h2>
        <span className="mono-label">{operations.length}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-xs">
          <thead className="text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Действие</th>
              <th className="px-3 py-2 font-medium">Состояние</th>
              <th className="px-3 py-2 font-medium">Сегмент</th>
              <th className="px-3 py-2 font-medium">Исполнитель</th>
              <th className="px-3 py-2 font-medium">Попытки</th>
              <th className="px-3 py-2 text-right font-medium">Действия</th>
            </tr>
          </thead>
          <tbody>
            {operations.map((operation) => (
              <tr key={operation.operation_id} className="border-t border-surface-700/60 hover:bg-surface-800/45">
                <td className="px-4 py-2 text-zinc-200"><div>{operationKindLabel(operation.kind)}</div><div className="mt-0.5 font-mono text-[10px] text-zinc-600">{operation.kind}</div></td>
                <td className="px-3 py-2 text-zinc-300">{operationStateLabel(operation.state)}</td>
                <td className="max-w-44 truncate px-3 py-2 font-mono text-zinc-500">{operation.shard_id ?? '-'}</td>
                <td className="px-3 py-2 text-zinc-500">{operationLeaseLabel(operation)}</td>
                <td className="px-3 py-2 text-zinc-400">{operation.current_attempt}</td>
                <td className="px-3 py-1 text-right">
                  <div className="inline-flex">
                    <IconButton title="Детали выполнения" onClick={() => onShowAttempts(operation)}>
                      <Search className="h-3.5 w-3.5" />
                    </IconButton>
                    {['failed', 'blocked', 'contract_violation'].includes(operation.state) && (
                      <IconButton title="Повторить действие" onClick={() => onRetry(operation.operation_id)}>
                        <RotateCcw className="h-3.5 w-3.5" />
                      </IconButton>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {!operations.length && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-zinc-500">Действий пока нет.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
