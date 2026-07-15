import { useEffect, useMemo, useState } from 'react'
import {
  ArrowUpRight,
  BarChart3,
  ChevronDown,
  CircleDollarSign,
  Search,
  Sparkles,
  Store,
  Target,
  TrendingUp,
  Users,
} from 'lucide-react'

import {
  aiStatusLabel,
  asRecord,
  asStrings,
  cleanClusterLabel,
  formatMoney,
  marketResultModel,
  resultNumber,
  resultValue,
  verdictLabel,
  verdictTone,
  type JsonRecord,
} from '../resultModel'
import type { MarketResults } from '../types'
import { formatCount } from './shared'

type ClusterSort = 'confidence' | 'demand' | 'competition' | 'members'
type ClusterFilter = 'all' | 'promising_for_entry' | 'popular_crowded' | 'do_not_take'

function clusterMetric(cluster: JsonRecord, key: string): number {
  return resultNumber(asRecord(cluster.metrics), key)
}

function clusterPrice(cluster: JsonRecord): JsonRecord | undefined {
  return asRecord(asRecord(cluster.metrics)?.price)
}

function shareLabel(value: number): string {
  if (!Number.isFinite(value)) return '-'
  return `${(value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`
}

export function ResultsPanel({
  results,
  onOpenPublication,
}: {
  results: MarketResults | null
  onOpenPublication(opportunity: JsonRecord): void
}) {
  const [query, setQuery] = useState('')
  const [verdictFilter, setVerdictFilter] = useState<ClusterFilter>('all')
  const [sortBy, setSortBy] = useState<ClusterSort>('confidence')
  const [visibleCount, setVisibleCount] = useState(20)
  const [expandedCluster, setExpandedCluster] = useState<string | null>(null)
  const [selectedOpportunity, setSelectedOpportunity] = useState<JsonRecord | null>(null)

  const model = useMemo(() => marketResultModel(results), [results])
  const uniqueCards = results?.counters.unique_cards ?? 0
  const aiReady = model.aiVerdict?.status === 'ok'
  const marketVerdict = model.aiVerdict?.market_verdict
  const confidence = resultNumber(model.aiVerdict, 'confidence')
  const reasons = asStrings(model.aiVerdict?.reasons)

  const filteredClusters = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    return model.clusters
      .filter((cluster) => verdictFilter === 'all' || String(cluster.state) === verdictFilter)
      .filter((cluster) => {
        if (!needle) return true
        return `${cleanClusterLabel(cluster.label)} ${resultValue(cluster, 'cluster_id')} ${verdictLabel(cluster.state)}`
          .toLocaleLowerCase('ru-RU')
          .includes(needle)
      })
      .sort((left, right) => {
        if (sortBy === 'demand') return clusterMetric(right, 'demand_proof_score') - clusterMetric(left, 'demand_proof_score')
        if (sortBy === 'competition') return clusterMetric(right, 'competition_score') - clusterMetric(left, 'competition_score')
        if (sortBy === 'members') return clusterMetric(right, 'member_count') - clusterMetric(left, 'member_count')
        return Number(right.confidence ?? 0) - Number(left.confidence ?? 0)
      })
  }, [model.clusters, query, sortBy, verdictFilter])

  useEffect(() => {
    setVisibleCount(20)
    setExpandedCluster(null)
  }, [query, sortBy, verdictFilter])

  function openPublication(opportunity: JsonRecord) {
    setSelectedOpportunity(opportunity)
    onOpenPublication(opportunity)
  }

  if (!results) {
    return (
      <section className="market-surface p-8 text-center">
        <BarChart3 className="mx-auto h-6 w-6 text-zinc-600" />
        <h2 className="mt-3 text-base font-medium text-zinc-200">Итоговый анализ ещё не создан</h2>
        <p className="mt-1 text-sm text-zinc-500">После завершения сбора здесь появятся выводы, цены и смысловые группы.</p>
      </section>
    )
  }

  const pricePoints = [
    ['Минимум', model.price?.min],
    ['P10', model.price?.p10],
    ['P25', model.price?.p25],
    ['Медиана', model.price?.p50],
    ['P75', model.price?.p75],
    ['P90', model.price?.p90],
    ['Максимум', model.price?.max],
  ] as const
  const visibleClusters = filteredClusters.slice(0, visibleCount)
  const topSellers = model.repeatedSellers.slice(0, 12)
  const analysisPending = !model.semantic && results.phase !== 'analyze' && results.phase !== 'export'

  return (
    <div className="market-view-stack">
      <section className="market-insight-hero">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sky-300">
            <Sparkles className="h-4 w-4" />
            <span className="market-section-label text-sky-300">AI-вердикт</span>
          </div>
          <h2 className="mt-4 max-w-4xl text-2xl font-semibold text-white">
            {aiReady ? verdictLabel(marketVerdict) : aiStatusLabel(model.aiVerdict?.status)}
          </h2>
          <p className="mt-3 max-w-5xl whitespace-pre-wrap text-[15px] leading-7 text-zinc-300">
            {aiReady
              ? resultValue(model.aiVerdict, 'summary')
              : resultValue(model.aiVerdict, 'summary', resultValue(model.aiVerdict, 'detail', 'Вердикт ещё не сохранён.'))}
          </p>
          {!!reasons.length && (
            <ol className="mt-6 grid grid-cols-2 gap-x-8 gap-y-3 max-lg:grid-cols-1">
              {reasons.map((reason, index) => (
                <li key={reason} className="grid grid-cols-[28px_1fr] gap-2 text-sm leading-6 text-zinc-400">
                  <span className="font-mono text-xs text-sky-300">{String(index + 1).padStart(2, '0')}</span>
                  <span>{reason}</span>
                </li>
              ))}
            </ol>
          )}
        </div>
        <div className={`market-confidence-dial market-tone-${verdictTone(marketVerdict)}`} style={{ '--confidence': `${confidence * 3.6}deg` } as React.CSSProperties}>
          <div>
            <strong>{formatCount(confidence)}</strong>
            <span>уверенность</span>
          </div>
        </div>
      </section>

      {analysisPending && (
        <div className="market-notice">
          Итоговый анализ ещё не запускался: собрано {formatCount(uniqueCards)} из {formatCount(results.target_unique_cards)} карточек.
        </div>
      )}

      {model.opportunities.length > 0 && (
        <section className="market-surface overflow-hidden">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Рекомендации</div>
              <h3 className="mt-1 text-base font-semibold text-white">Возможности для входа</h3>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-zinc-500">{model.opportunities.length} вариантов</span>
              <button
                type="button"
                className="market-compact-action"
                onClick={() => openPublication(selectedOpportunity ?? model.opportunities[0])}
              >
                К публикации <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
          <div
            className="market-opportunity-grid"
            style={{
              '--market-opportunity-columns': model.opportunities.length === 4
                ? 2
                : Math.min(model.opportunities.length, 3),
            } as React.CSSProperties}
          >
            {model.opportunities.map((item, index) => (
              <article
                key={`${resultValue(item, 'cluster_id')}-${index}`}
                className={`market-opportunity-card ${selectedOpportunity === item ? 'is-selected' : ''}`}
              >
                <div className="flex items-center justify-between gap-3">
                  <span className={`market-tone-label market-tone-${verdictTone(item.verdict)}`}>{verdictLabel(item.verdict)}</span>
                  <span className="font-mono text-xs text-zinc-500">{resultValue(item, 'confidence')}%</span>
                </div>
                <h4 className="mt-4 text-base font-medium leading-6 text-white">{cleanClusterLabel(item.label)}</h4>
                <p className="mt-2 text-sm leading-6 text-zinc-400">{resultValue(item, 'why')}</p>
                {!!asStrings(item.evidence_ids).length && (
                  <div className="mt-4 truncate font-mono text-[10px] text-zinc-600">{asStrings(item.evidence_ids).join(' · ')}</div>
                )}
                <button type="button" className="market-opportunity-action" onClick={() => openPublication(item)}>
                  Создать карточку <ArrowUpRight className="h-3.5 w-3.5" />
                </button>
              </article>
            ))}
          </div>
        </section>
      )}

      <div className="grid grid-cols-[minmax(0,1.35fr)_minmax(320px,0.65fr)] gap-4 max-xl:grid-cols-1">
        <section className="market-surface overflow-hidden">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Ценовой коридор</div>
              <h3 className="mt-1 text-base font-semibold text-white">Распределение цен</h3>
            </div>
            <span className="text-xs text-zinc-500">{formatCount(resultNumber(model.price, 'price_count'))} карточек с ценой</span>
          </div>
          <div className="market-price-spectrum">
            <div className="market-price-track"><span /></div>
            <div className="market-price-points">
              {pricePoints.map(([label, price], index) => (
                <div key={label} className={index === 3 ? 'is-primary' : ''}>
                  <span>{label}</span>
                  <strong>{formatMoney(price)}</strong>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="market-surface overflow-hidden">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Структура продавцов</div>
              <h3 className="mt-1 text-base font-semibold text-white">Насыщенность рынка</h3>
            </div>
            <Store className="h-4 w-4 text-zinc-500" />
          </div>
          <div className="market-seller-summary">
            <InsightMetric icon={Users} label="Уникальных" value={resultValue(model.sellers, 'unique_seller_count', '0')} />
            <InsightMetric icon={TrendingUp} label="Повторяются" value={resultValue(model.sellers, 'repeated_seller_count', '0')} />
            <InsightMetric icon={Target} label="Доля повторов" value={shareLabel(resultNumber(model.sellers, 'repeat_share_of_observed_cards'))} />
          </div>
          {topSellers.length > 0 && (
            <div className="market-ranked-list">
              {topSellers.map((seller, index) => (
                <div key={`${resultValue(seller, 'seller_key')}-${index}`}>
                  <span className="font-mono text-zinc-300">{resultValue(seller, 'seller_key')}</span>
                  <span className="font-mono text-zinc-500">{resultValue(seller, 'observed_card_count')}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      <section className="market-surface overflow-hidden">
        <div className="market-surface-head flex-wrap">
          <div>
            <div className="market-section-label">Смысловые группы</div>
            <h3 className="mt-1 text-base font-semibold text-white">Карта локальных ниш</h3>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="market-search-field">
              <Search className="h-3.5 w-3.5" />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Поиск группы" />
            </label>
            <label className="market-select-field">
              <span className="sr-only">Фильтр по вердикту</span>
              <select value={verdictFilter} onChange={(event) => setVerdictFilter(event.target.value as ClusterFilter)}>
                <option value="all">Все оценки</option>
                <option value="promising_for_entry">Перспективные</option>
                <option value="popular_crowded">Плотный рынок</option>
                <option value="do_not_take">Не брать</option>
              </select>
              <ChevronDown className="h-3.5 w-3.5" />
            </label>
            <label className="market-select-field">
              <span className="sr-only">Сортировка групп</span>
              <select value={sortBy} onChange={(event) => setSortBy(event.target.value as ClusterSort)}>
                <option value="confidence">По уверенности</option>
                <option value="demand">По спросу</option>
                <option value="competition">По конкуренции</option>
                <option value="members">По размеру</option>
              </select>
              <ChevronDown className="h-3.5 w-3.5" />
            </label>
          </div>
        </div>

        <div className="market-cluster-scroll">
          <div className="market-cluster-head">
            <span>Группа</span><span>Оценка</span><span>Увер.</span><span>Спрос</span><span>Конкур.</span><span>Медиана</span>
          </div>
          {visibleClusters.map((cluster) => {
            const clusterId = resultValue(cluster, 'cluster_id')
            const expanded = clusterId === expandedCluster
            const metrics = asRecord(cluster.metrics)
            return (
              <div key={clusterId} className={`market-cluster-item ${expanded ? 'is-expanded' : ''}`}>
                <button type="button" className="market-cluster-row" onClick={() => setExpandedCluster(expanded ? null : clusterId)}>
                  <span className="min-w-0">
                    <strong>{cleanClusterLabel(cluster.label)}</strong>
                    <small>{formatCount(resultNumber(metrics, 'member_count'))} карточек · {formatCount(resultNumber(metrics, 'unique_seller_count'))} продавцов</small>
                  </span>
                  <span className={`market-tone-label market-tone-${verdictTone(cluster.state)}`}>{verdictLabel(cluster.state)}</span>
                  <ScoreCell value={Number(cluster.confidence ?? 0)} />
                  <ScoreCell value={resultNumber(metrics, 'demand_proof_score')} />
                  <ScoreCell value={resultNumber(metrics, 'competition_score')} invert />
                  <span className="font-mono text-sm text-zinc-300">{formatMoney(clusterPrice(cluster)?.median)}</span>
                </button>
                {expanded && (
                  <div className="market-cluster-detail">
                    <div><span>Барьер входа</span><strong>{resultValue(metrics, 'entry_barrier_score')}</strong></div>
                    <div><span>Продавцов</span><strong>{resultValue(metrics, 'unique_seller_count')}</strong></div>
                    <div><span>Карточек</span><strong>{resultValue(metrics, 'member_count')}</strong></div>
                    <div className="min-w-0"><span>ID группы</span><strong className="truncate font-mono">{clusterId}</strong></div>
                  </div>
                )}
              </div>
            )
          })}
          {!visibleClusters.length && <div className="market-empty-state">По выбранным условиям группы не найдены.</div>}
        </div>
        <div className="market-table-footer">
          <span>Показано {Math.min(visibleCount, filteredClusters.length)} из {filteredClusters.length}; всего {model.clusters.length}</span>
          {visibleCount < filteredClusters.length && (
            <button type="button" className="market-text-action" onClick={() => setVisibleCount((current) => current + 20)}>Показать ещё</button>
          )}
        </div>
      </section>

      {model.shardRows.length > 0 && (
        <details className="market-surface market-disclosure">
          <summary>
            <span><span className="market-section-label">Источники</span><strong>Распределение по потокам</strong></span>
            <span>{model.shardRows.length} потоков <ChevronDown className="h-4 w-4" /></span>
          </summary>
          <div className="overflow-x-auto border-t border-white/10">
            <table className="market-data-table min-w-[760px]">
              <thead><tr><th>Поток</th><th className="text-right">Карточки</th><th className="text-right">Минимум</th><th className="text-right">Медиана</th><th className="text-right">Максимум</th></tr></thead>
              <tbody>{model.shardRows.map((row) => { const distribution = asRecord(row.price_distribution); return (
                <tr key={resultValue(row, 'shard_id')}><td className="font-mono">{resultValue(row, 'shard_id')}</td><td className="text-right font-mono">{resultValue(row, 'observed_listing_count')}</td><td className="text-right font-mono">{formatMoney(distribution?.min)}</td><td className="text-right font-mono text-zinc-200">{formatMoney(distribution?.p50)}</td><td className="text-right font-mono">{formatMoney(distribution?.max)}</td></tr>
              )})}</tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  )
}

function InsightMetric({ icon: Icon, label, value }: { icon: typeof Users; label: string; value: string }) {
  return (
    <div>
      <Icon className="h-3.5 w-3.5 text-sky-300" />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function ScoreCell({ value, invert = false }: { value: number; invert?: boolean }) {
  const normalized = Math.max(0, Math.min(100, value))
  const tone = invert ? 100 - normalized : normalized
  const color = tone >= 70 ? '#34d399' : tone >= 45 ? '#fbbf24' : '#fb7185'
  return (
    <span className="market-score-cell">
      <span><i style={{ width: `${normalized}%`, backgroundColor: color }} /></span>
      <b>{formatCount(value)}</b>
    </span>
  )
}
