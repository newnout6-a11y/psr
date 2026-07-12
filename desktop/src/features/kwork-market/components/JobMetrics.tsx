import type { MarketJob } from '../types'
import { formatCount } from './shared'

export function JobMetrics({ job }: { job: MarketJob }) {
  const counters = job.counters ?? {}
  const metrics = [
    ['Уникальных карточек', counters.unique_cards ?? counters.unique_listings ?? 0],
    ['Наблюдений карточек', counters.card_occurrences ?? counters.listing_observations ?? 0],
    ['Принято порций', counters.accepted_batches ?? 0],
    ['Запросов', counters.requests ?? 0],
    ['Карточек в области', counters.aggregate_scope_total ?? 0],
  ] as const
  return (
    <section className="grid grid-cols-5 gap-px overflow-hidden rounded-lg border border-surface-600/70 bg-surface-600/70 max-xl:grid-cols-3 max-md:grid-cols-2">
      {metrics.map(([label, value]) => (
        <div key={label} className="min-w-0 bg-surface-900/85 px-4 py-3">
          <div className="mono-label truncate">{label}</div>
          <div className="mt-1 text-xl font-semibold text-white">{formatCount(value)}</div>
        </div>
      ))}
    </section>
  )
}
