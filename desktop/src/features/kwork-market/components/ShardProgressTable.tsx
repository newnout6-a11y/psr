import type { MarketShard } from '../types'
import { formatCount } from './shared'

function shardStateLabel(state: string): string {
  const labels: Record<string, string> = {
    queued: 'в очереди',
    running: 'в работе',
    completed: 'завершён',
    exhausted: 'исчерпан',
    failed: 'ошибка',
  }
  return labels[state] ?? state
}

export function ShardProgressTable({ shards }: { shards: MarketShard[] }) {
  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head">
        <div><div className="market-section-label">Источники</div><h2 className="mt-1 text-base font-semibold text-white">Сегменты сбора</h2></div>
        <span className="font-mono text-xs text-zinc-500">{shards.length}</span>
      </div>
      <div className="max-h-[34rem] overflow-auto">
        <table className="market-data-table min-w-[680px]">
          <thead>
            <tr>
              <th>Источник</th>
              <th>Рубрика</th>
              <th>Состояние</th>
              <th className="text-right">Ожидаемо</th>
              <th>Продолжение</th>
            </tr>
          </thead>
          <tbody>
            {shards.map((shard) => (
              <tr key={shard.shard_id}>
                <td className="font-mono text-zinc-300">{shard.source}</td>
                <td><div className="text-zinc-200">{shard.alias}</div><div className="market-row-note truncate font-mono">{shard.shard_id}</div></td>
                <td><span className={`market-tone-label ${['completed', 'exhausted'].includes(shard.state) ? 'market-tone-positive' : shard.state === 'failed' ? 'market-tone-negative' : 'market-tone-neutral'}`}>{shardStateLabel(shard.state)}</span></td>
                <td className="text-right font-mono">{shard.expected_count === null || shard.expected_count === undefined ? '—' : formatCount(shard.expected_count)}</td>
                <td className="max-w-72 truncate text-zinc-500">{shard.cursor ? 'Позиция сохранена' : 'Нет продолжения'}</td>
              </tr>
            ))}
            {!shards.length && <tr><td colSpan={5}><div className="market-empty-state">Сегменты пока не подготовлены.</div></td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
