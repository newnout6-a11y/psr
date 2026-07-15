import { useMemo, useState } from 'react'
import { RotateCcw, Search } from 'lucide-react'

import type { MarketOperation } from '../types'
import { IconButton, operationKindLabel, operationLeaseLabel, operationStateLabel } from './shared'

interface OperationTableProps {
  operations: MarketOperation[]
  onRetry(operationId: string): void
  onShowAttempts(operation: MarketOperation): void
}

export function OperationTable({ operations, onRetry, onShowAttempts }: OperationTableProps) {
  const [query, setQuery] = useState('')
  const [onlyProblems, setOnlyProblems] = useState(false)
  const visibleOperations = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    return operations.filter((operation) => {
      if (onlyProblems && !['failed', 'blocked', 'contract_violation', 'retry_wait'].includes(operation.state)) return false
      if (!needle) return true
      return `${operation.operation_id} ${operation.kind} ${operation.state} ${operation.shard_id ?? ''} ${operation.lease_owner ?? ''}`.toLocaleLowerCase('ru-RU').includes(needle)
    })
  }, [onlyProblems, operations, query])

  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div><div className="market-section-label">Очередь</div><h2 className="mt-1 text-base font-semibold text-white">Ход выполнения</h2></div>
        <div className="flex items-center gap-2">
          <label className="market-search-field"><Search className="h-3.5 w-3.5" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Действие или worker" /></label>
          <button type="button" className={`market-filter-button ${onlyProblems ? 'is-active' : ''}`} aria-pressed={onlyProblems} onClick={() => setOnlyProblems((current) => !current)}>Только проблемы</button>
        </div>
      </div>
      <div className="max-h-[34rem] overflow-auto">
        <table className="market-data-table min-w-[820px]">
          <thead><tr><th>Действие</th><th>Состояние</th><th>Сегмент</th><th>Исполнитель</th><th className="text-right">Попытки</th><th className="text-right">Действия</th></tr></thead>
          <tbody>
            {visibleOperations.map((operation) => (
              <tr key={operation.operation_id}>
                <td><div className="text-zinc-200">{operationKindLabel(operation.kind)}</div><div className="market-row-note font-mono">{operation.operation_id}</div></td>
                <td><span className={`market-tone-label ${operation.state === 'succeeded' ? 'market-tone-positive' : ['failed', 'blocked', 'contract_violation'].includes(operation.state) ? 'market-tone-negative' : 'market-tone-neutral'}`}>{operationStateLabel(operation.state)}</span></td>
                <td className="max-w-48 truncate font-mono">{operation.shard_id ?? '-'}</td>
                <td>{operationLeaseLabel(operation)}</td>
                <td className="text-right font-mono">{operation.current_attempt}</td>
                <td className="text-right"><div className="inline-flex"><IconButton title="Детали выполнения" onClick={() => onShowAttempts(operation)}><Search className="h-3.5 w-3.5" /></IconButton>{['failed', 'blocked', 'contract_violation'].includes(operation.state) && <IconButton title="Повторить действие" onClick={() => onRetry(operation.operation_id)}><RotateCcw className="h-3.5 w-3.5" /></IconButton>}</div></td>
              </tr>
            ))}
            {!visibleOperations.length && <tr><td colSpan={6}><div className="market-empty-state">По выбранному фильтру действий нет.</div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="market-table-footer"><span>Показано {visibleOperations.length} из {operations.length}</span></div>
    </section>
  )
}
