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
    <section className="factory-panel overflow-hidden">
      <div className="table-head flex items-center justify-between px-4 py-3">
        <h2 className="text-sm font-medium text-white">Сегменты сбора</h2>
        <span className="mono-label">{shards.length}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[650px] text-left text-xs">
          <thead className="text-zinc-500">
            <tr>
              <th className="px-4 py-2 font-medium">Источник</th>
              <th className="px-3 py-2 font-medium">Рубрика</th>
              <th className="px-3 py-2 font-medium">Состояние</th>
              <th className="px-3 py-2 font-medium">Ожидаемо</th>
              <th className="px-3 py-2 font-medium">Позиция в каталоге</th>
            </tr>
          </thead>
          <tbody>
            {shards.map((shard) => (
              <tr key={shard.shard_id} className="border-t border-surface-700/60 hover:bg-surface-800/45">
                <td className="px-4 py-2 font-mono text-zinc-300">{shard.source}</td>
                <td className="px-3 py-2 text-zinc-200">{shard.alias}</td>
                <td className="px-3 py-2 text-zinc-400">{shardStateLabel(shard.state)}</td>
                <td className="px-3 py-2 text-zinc-400">{shard.expected_count === null || shard.expected_count === undefined ? '—' : formatCount(shard.expected_count)}</td>
                <td className="max-w-72 truncate px-3 py-2 text-zinc-500">{shard.cursor ? 'сохранена' : 'нет продолжения'}</td>
              </tr>
            ))}
            {!shards.length && <tr><td colSpan={5} className="px-4 py-8 text-center text-zinc-500">Сегменты пока не подготовлены.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
