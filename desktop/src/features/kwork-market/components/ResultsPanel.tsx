import type { MarketResults } from '../types'
import { formatCount } from './shared'

type JsonRecord = Record<string, unknown>

function numberFrom(record: JsonRecord | undefined, key: string): unknown {
  return record?.[key]
}

export function ResultsPanel({ results }: { results: MarketResults | null }) {
  if (!results) return null

  const analysis = results.analysis as JsonRecord | undefined
  const enrichment = analysis?.enrichment_selection as JsonRecord | undefined
  const aiEvidence = analysis?.ai_evidence as JsonRecord | undefined
  const evidenceSample = aiEvidence?.evidence_sample as JsonRecord | undefined
  const priceDistribution = results.metrics?.price_distribution as JsonRecord | undefined
  const aiDisabled = aiEvidence?.enabled === false
  const uniqueCards = results.counters.unique_cards ?? 0
  const analysisPending = !results.latest_checkpoint && results.phase !== 'analyze' && results.phase !== 'export'

  return (
    <section className="factory-panel p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-white">Результаты сбора</h2>
        <span className="mono-label">{results.state}/{results.phase}</span>
      </div>
      {analysisPending && (
        <p className="mt-1 text-xs text-zinc-500">
          Итоговый анализ ещё не запускался: собрано {formatCount(uniqueCards)} из {formatCount(results.target_unique_cards)} карточек.
        </p>
      )}
      <div className="mt-3 grid grid-cols-6 gap-3 max-2xl:grid-cols-3 max-md:grid-cols-1">
        <ResultValue label="Цель" value={formatCount(results.target_unique_cards)} />
        <ResultValue label="Уникальных" value={formatCount(uniqueCards)} />
        <ResultValue label="Медианная цена" value={String(numberFrom(priceDistribution, 'p50') ?? '-')} />
        <ResultValue label="Обогащение" value={String(numberFrom(enrichment, 'selected_count') ?? '-')} />
        <ResultValue
          label="Доказательства для AI"
          value={aiDisabled ? 'выключено' : String(numberFrom(evidenceSample, 'included_evidence_count') ?? '-')}
        />
        <ResultValue
          label="Срез"
          value={results.latest_checkpoint?.checkpoint_id ? String(results.latest_checkpoint.checkpoint_id) : '-'}
          compact
        />
      </div>
    </section>
  )
}

function ResultValue({ label, value, compact = false }: { label: string; value: string; compact?: boolean }) {
  return (
    <div>
      <div className="mono-label">{label}</div>
      <div className={compact ? 'mt-1 truncate font-mono text-xs text-zinc-400' : 'mt-1 text-lg font-semibold text-white'}>{value}</div>
    </div>
  )
}
