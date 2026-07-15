import {
  Activity,
  ArrowUpRight,
  Bot,
  CircleDollarSign,
  Database,
  Gauge,
  Globe2,
  Layers3,
  Network,
  ShieldCheck,
  UsersRound,
} from 'lucide-react'

import { asStrings, formatMoney, marketResultModel, resultNumber, resultValue, verdictLabel, verdictTone } from '../resultModel'
import type { MarketJob, MarketJobEvent, MarketResults, MarketShard, MarketTransport, MarketWorker } from '../types'
import { formatCount, formatTimestamp, phaseLabel } from './shared'

interface JobOverviewPanelProps {
  job: MarketJob
  results: MarketResults | null
  workers: MarketWorker[]
  transports: MarketTransport[]
  shards: MarketShard[]
  events: MarketJobEvent[]
  onOpenInsights(): void
  onOpenPublication(): void
  onOpenExecution(): void
}

const PHASES: MarketJob['phase'][] = ['prepare', 'map', 'plan', 'collect', 'enrich', 'analyze', 'export']

function elapsedLabel(job: MarketJob): string {
  const start = Date.parse(job.started_at || job.created_at)
  const end = Date.parse(job.finished_at || new Date().toISOString())
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '-'
  const totalSeconds = Math.round((end - start) / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours) return `${hours} ч ${minutes} мин`
  if (minutes) return `${minutes} мин ${seconds} с`
  return `${seconds} с`
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`
}

export function JobOverviewPanel({
  job,
  results,
  workers,
  transports,
  shards,
  events,
  onOpenInsights,
  onOpenPublication,
  onOpenExecution,
}: JobOverviewPanelProps) {
  const model = marketResultModel(results)
  const uniqueCards = Number(job.counters.unique_cards ?? job.counters.unique_listings ?? 0)
  const observations = Number(job.counters.card_occurrences ?? job.counters.listing_observations ?? 0)
  const progress = Math.min(1, uniqueCards / Math.max(1, job.target_unique_cards))
  const activeWorkers = workers.filter((worker) => ['starting', 'connecting', 'leasing', 'busy', 'cooldown', 'backoff', 'draining'].includes(worker.actual_state)).length
  const healthyRoutes = transports.filter((transport) => transport.health === 'healthy').length
  const distinctIps = resultNumber(model.execution, 'distinct_egress_ips', Number(job.counters.distinct_egress_ips ?? 0))
  const warningCount = events.filter((event) => event.type === 'warning' || event.type === 'operation.failed' || event.type === 'operation.contract_violation').length
  const aiReady = model.aiVerdict?.status === 'ok'
  const marketVerdict = model.aiVerdict?.market_verdict
  const tone = verdictTone(marketVerdict)
  const reasons = asStrings(model.aiVerdict?.reasons).slice(0, 3)
  const topOpportunities = [...model.opportunities]
    .sort((left, right) => Number(right.confidence ?? 0) - Number(left.confidence ?? 0))
    .slice(0, 2)
  const repeatedShare = resultNumber(model.sellers, 'repeat_share_of_observed_cards')
  const currentPhaseIndex = PHASES.indexOf(job.phase)
  const isTerminal = ['completed', 'stopped', 'failed'].includes(job.state)

  return (
    <div className="market-view-stack">
      <section className="market-overview-hero">
        <div className="min-w-0">
          <div className="market-section-label">Результат запуска</div>
          <div className="mt-3 flex flex-wrap items-end gap-x-5 gap-y-2">
            <div className="text-4xl font-semibold text-white max-md:text-3xl">{formatCount(uniqueCards)}</div>
            <div className="pb-1 text-sm text-zinc-400">уникальных карточек из {formatCount(job.target_unique_cards)}</div>
          </div>
          <div className="market-progress mt-4" aria-label={`Прогресс ${Math.round(progress * 100)}%`}>
            <span style={{ width: `${Math.max(progress * 100, uniqueCards ? 2 : 0)}%` }} />
          </div>
          <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs text-zinc-500">
            <span>{Math.round(progress * 100)}% цели</span>
            <span>{isTerminal ? `Завершено ${formatTimestamp(job.finished_at)}` : `Текущий этап: ${phaseLabel(job.phase)}`}</span>
          </div>
        </div>
        <div className={`market-verdict-orbit market-tone-${tone}`}>
          <div className="market-verdict-score">{aiReady ? resultValue(model.aiVerdict, 'confidence', '0') : Math.round(progress * 100)}</div>
          <div className="market-verdict-caption">{aiReady ? 'уверенность AI' : 'готовность данных'}</div>
        </div>
      </section>

      <section className="market-stat-strip" aria-label="Ключевые показатели">
        <OverviewStat icon={Database} label="Наблюдений" value={formatCount(observations)} note={`${formatCount(job.counters.accepted_batches ?? 0)} порций`} />
        <OverviewStat icon={UsersRound} label="Исполнители" value={`${activeWorkers} / ${job.desired_workers}`} note={isTerminal ? `${workers.length} сохранено` : 'активно сейчас'} />
        <OverviewStat icon={Globe2} label="Уникальные IP" value={formatCount(distinctIps)} note={`${healthyRoutes} здоровых маршрутов`} />
        <OverviewStat icon={Layers3} label="Смысловые группы" value={formatCount(model.clusters.length)} note={`${formatCount(resultNumber(model.enrichment, 'selected_count'))} обогащено`} />
        <OverviewStat icon={CircleDollarSign} label="Медианная цена" value={formatMoney(model.price?.p50)} note={`${formatMoney(model.price?.p25)} – ${formatMoney(model.price?.p75)}`} />
        <OverviewStat icon={Gauge} label="Длительность" value={elapsedLabel(job)} note={`${formatCount(job.counters.requests ?? resultNumber(model.execution, 'fetch_attempt_count'))} запросов`} />
      </section>

      <div className="grid grid-cols-[minmax(0,1.45fr)_minmax(320px,0.75fr)] gap-4 max-xl:grid-cols-1">
        <section className="market-surface min-w-0">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Решение</div>
              <h2 className="mt-1 text-lg font-semibold text-white">{aiReady ? verdictLabel(marketVerdict) : 'Вывод формируется'}</h2>
            </div>
            <button type="button" className="market-text-action" onClick={onOpenInsights}>
              Все выводы <ArrowUpRight className="h-4 w-4" />
            </button>
          </div>
          <div className="market-surface-body">
            {aiReady ? (
              <>
                <p className="max-w-5xl text-[15px] leading-7 text-zinc-300">{resultValue(model.aiVerdict, 'summary')}</p>
                {!!reasons.length && (
                  <div className="mt-5 grid grid-cols-3 gap-px overflow-hidden rounded-md bg-white/10 max-lg:grid-cols-1">
                    {reasons.map((reason, index) => (
                      <div key={reason} className="bg-[#0d1115] px-4 py-3 text-sm leading-5 text-zinc-400">
                        <span className="mr-2 font-mono text-[11px] text-sky-300">0{index + 1}</span>{reason}
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="market-empty-inline">
                <Bot className="h-5 w-5" />
                <span>AI-вердикт появится после сохранения итогового среза. Уже собрано {formatCount(uniqueCards)} карточек.</span>
              </div>
            )}
          </div>
        </section>

        <section className="market-surface min-w-0">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Состояние</div>
              <h2 className="mt-1 text-base font-semibold text-white">Контроль выполнения</h2>
            </div>
            <button type="button" className="market-icon-action" title="Открыть выполнение" aria-label="Открыть выполнение" onClick={onOpenExecution}>
              <Activity className="h-4 w-4" />
            </button>
          </div>
          <div className="market-phase-list">
            {PHASES.map((phase, index) => {
              const completed = isTerminal || index < currentPhaseIndex
              const active = !isTerminal && index === currentPhaseIndex
              return (
                <div key={phase} className={`market-phase-row ${completed ? 'is-complete' : ''} ${active ? 'is-active' : ''}`}>
                  <span className="market-phase-mark">{completed ? <ShieldCheck className="h-3.5 w-3.5" /> : index + 1}</span>
                  <span>{phaseLabel(phase)}</span>
                </div>
              )
            })}
          </div>
          <div className="grid grid-cols-3 border-t border-white/10 text-center">
            <MiniMetric icon={Network} value={formatCount(shards.length)} label="потоков" />
            <MiniMetric icon={ShieldCheck} value={formatCount(healthyRoutes)} label="маршрутов" />
            <MiniMetric icon={Activity} value={formatCount(warningCount)} label="сигналов" />
          </div>
        </section>
      </div>

      {topOpportunities.length > 0 && (
        <section className="market-surface overflow-hidden">
          <div className="market-surface-head">
            <div>
              <div className="market-section-label">Точки входа</div>
              <h2 className="mt-1 text-base font-semibold text-white">Самые сильные возможности</h2>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-zinc-500">повторы продавцов {percent(repeatedShare)}</span>
              <button
                type="button"
                className="market-compact-action"
                onClick={onOpenPublication}
              >
                Подготовить карточку <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
          <div className="market-opportunity-grid">
            {topOpportunities.map((opportunity, index) => (
              <article key={`${resultValue(opportunity, 'cluster_id')}-${index}`} className="market-opportunity-card">
                <div className="flex items-start justify-between gap-3">
                  <span className={`market-tone-label market-tone-${verdictTone(opportunity.verdict)}`}>{verdictLabel(opportunity.verdict)}</span>
                  <span className="font-mono text-xs text-zinc-500">{resultValue(opportunity, 'confidence')}%</span>
                </div>
                <h3 className="mt-4 text-base font-medium leading-6 text-white">{resultValue(opportunity, 'label')}</h3>
                <p className="mt-2 line-clamp-3 text-sm leading-6 text-zinc-400">{resultValue(opportunity, 'why')}</p>
              </article>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function OverviewStat({
  icon: Icon,
  label,
  value,
  note,
}: {
  icon: typeof Database
  label: string
  value: string
  note: string
}) {
  return (
    <div className="market-stat">
      <Icon className="h-4 w-4 text-sky-300" />
      <div className="min-w-0">
        <div className="market-section-label truncate">{label}</div>
        <div className="mt-1 truncate text-xl font-semibold text-white">{value}</div>
        <div className="mt-1 truncate text-xs text-zinc-500">{note}</div>
      </div>
    </div>
  )
}

function MiniMetric({ icon: Icon, value, label }: { icon: typeof Activity; value: string; label: string }) {
  return (
    <div className="px-2 py-3">
      <Icon className="mx-auto h-3.5 w-3.5 text-zinc-500" />
      <div className="mt-1 font-mono text-sm text-zinc-200">{value}</div>
      <div className="mt-0.5 text-[10px] uppercase text-zinc-600">{label}</div>
    </div>
  )
}
