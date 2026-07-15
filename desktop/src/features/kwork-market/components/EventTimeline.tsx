import { useMemo, useState } from 'react'
import { Activity, AlertTriangle, CheckCircle2, ChevronDown, Network, Route, Search, Send, UsersRound } from 'lucide-react'

import type { MarketJob, MarketJobEvent, MarketTransport } from '../types'
import { formatCount, formatTimestamp } from './shared'

const HIDDEN_EVENT_TYPES = new Set(['worker.heartbeat'])
type EventFilter = 'all' | 'warnings' | 'network' | 'workers' | 'publication'
type EventTone = 'neutral' | 'positive' | 'warning' | 'network' | 'publication'
type EventTag = Exclude<EventFilter, 'all'>

interface IdentityContext {
  username: string
  registrationId: string
  transportId: string
  slot: string
  egressIp: string
  signupIp: string
  bindingMode: string
  personaId: string
}

interface EventDetail {
  label: string
  value: string
}

interface TimelineRow {
  key: string
  seqs: number[]
  type: string
  title: string
  summary: string
  emittedAt: string
  tone: EventTone
  tags: Set<EventTag>
  details: EventDetail[]
  workers: string[]
  searchText: string
}

function textValue(value: unknown): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function numberValue(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function addDetail(details: EventDetail[], label: string, value: unknown) {
  const clean = textValue(value).trim()
  if (clean) details.push({ label, value: clean })
}

function workerLabel(workerId: string | null | undefined): string {
  if (!workerId) return 'worker не указан'
  const tail = workerId.match(/-(\d+)$/)?.[1]
  return tail ? `worker #${tail}` : workerId
}

function stateLabel(state: unknown): string {
  const labels: Record<string, string> = {
    starting: 'запускается',
    connecting: 'подключается',
    leasing: 'получает маршрут',
    idle: 'свободен',
    busy: 'выполняет запрос',
    cooldown: 'на паузе после запроса',
    backoff: 'ожидает повторной попытки',
    draining: 'завершает текущую работу',
    stopped: 'остановлен',
    failed: 'завершился ошибкой',
    disabled: 'отключён',
    running: 'должен работать',
  }
  const key = textValue(state)
  return labels[key] || key || 'состояние не указано'
}

function sourceLabel(source: unknown): string {
  const labels: Record<string, string> = {
    web_catalog: 'веб-каталог Kwork',
    mobile_api: 'mobile API Kwork',
    listing_details: 'карточка Kwork',
    enrichment: 'обогащение карточки',
  }
  const key = textValue(source)
  return labels[key] || key.replace(/_/g, ' ') || 'Kwork'
}

function outcomeLabel(outcome: unknown): string {
  const labels: Record<string, string> = {
    ok: 'успешно',
    success: 'успешно',
    completed: 'успешно',
    timeout: 'тайм-аут',
    failed: 'ошибка',
    error: 'ошибка',
    cancelled: 'отменён',
  }
  const key = textValue(outcome).toLocaleLowerCase('ru-RU')
  return labels[key] || key || 'результат не указан'
}

function durationLabel(value: unknown): string {
  const ms = numberValue(value)
  if (!ms) return ''
  if (ms < 1_000) return `${formatCount(ms)} мс`
  return `${(ms / 1_000).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} с`
}

function identityFromEvent(event: MarketJobEvent): IdentityContext {
  const payload = event.payload
  return {
    username: textValue(payload.username),
    registrationId: textValue(payload.registration_id || payload.account_registration_id),
    transportId: textValue(payload.transport_id),
    slot: textValue(payload.slot),
    egressIp: textValue(payload.egress_ip || payload.current_egress_ip),
    signupIp: textValue(payload.signup_ip),
    bindingMode: textValue(payload.binding_mode),
    personaId: textValue(payload.persona_id),
  }
}

function routeSummary(payload: MarketJobEvent['payload'], identity?: IdentityContext): string {
  const slot = textValue(payload.slot) || identity?.slot || ''
  const ip = textValue(payload.egress_ip || payload.current_egress_ip) || identity?.egressIp || ''
  const transport = textValue(payload.transport_id) || identity?.transportId || ''
  const parts = [slot ? `slot ${slot}` : '', ip, transport && !slot ? transport : ''].filter(Boolean)
  return parts.join(' · ')
}

function tagsFor(event: MarketJobEvent, tone: EventTone): Set<EventTag> {
  const tags = new Set<EventTag>()
  if (tone === 'warning') tags.add('warnings')
  if (event.type.startsWith('request.') || event.type === 'transport.state_changed') tags.add('network')
  if (event.type.startsWith('worker.')) tags.add('workers')
  if (event.type === 'publication.state_changed') tags.add('publication')
  return tags
}

function publicationDescription(event: MarketJobEvent): Pick<TimelineRow, 'title' | 'summary' | 'tone'> {
  const payload = event.payload
  const stage = textValue(payload.stage)
  const service = textValue(payload.service_summary)
  const descriptions: Record<string, Pick<TimelineRow, 'title' | 'summary' | 'tone'>> = {
    proposed: { title: 'Рекомендация сохранена для публикации', summary: service || 'Создана durable-рекомендация из рыночной возможности.', tone: 'publication' },
    rejected: { title: 'Рекомендация отклонена', summary: service || 'Связанный контур публикации закрыт.', tone: 'warning' },
    mapping: { title: 'Создан контур карточки Kwork', summary: service || 'Рекомендация подтверждена, начато сопоставление обязательных полей.', tone: 'publication' },
    manifest_loaded: { title: 'Поля Kwork загружены', summary: `${formatCount(numberValue(payload.control_count))} полей, обязательных без значения: ${formatCount(numberValue(payload.unresolved_count))}.`, tone: 'publication' },
    fields_saved: { title: 'Выбор полей Kwork сохранён', summary: `${formatCount(numberValue(payload.unresolved_count))} обязательных полей ещё требуют значения.`, tone: 'publication' },
    fields_confirmed: { title: 'Поля Kwork подтверждены', summary: 'Серверная проверка пройдена, можно генерировать связанный черновик.', tone: 'positive' },
    draft_generated: { title: 'Черновик карточки сохранён', summary: textValue(payload.title) || service || 'Текст и параметры карточки связаны с исходным анализом.', tone: 'positive' },
    dry_run: { title: 'Dry-run публикации выполнен', summary: textValue(payload.detail) || 'Проверен итоговый payload без отправки в Kwork.', tone: payload.ok === false ? 'warning' : 'publication' },
    published: { title: 'Карточка опубликована в Kwork', summary: textValue(payload.kwork_id) ? `Kwork ID ${payload.kwork_id}` : 'Kwork подтвердил публикацию.', tone: 'positive' },
    failed: { title: 'Публикация не выполнена', summary: textValue(payload.detail) || 'Kwork вернул ошибку публикации.', tone: 'warning' },
  }
  return descriptions[stage] || { title: 'Состояние публикации изменено', summary: stage || 'Обновлён связанный контур карточки.', tone: 'publication' }
}

function publicationStageLabel(stage: unknown): string {
  const labels: Record<string, string> = {
    proposed: 'рекомендация сохранена',
    mapping: 'настройка полей',
    manifest_loaded: 'поля загружены',
    fields_saved: 'поля сохранены',
    fields_confirmed: 'поля подтверждены',
    draft_generated: 'черновик готов',
    dry_run: 'dry-run пройден',
    published: 'опубликовано',
    failed: 'ошибка публикации',
    rejected: 'отклонено',
  }
  const key = textValue(stage)
  return labels[key] || key || 'не начата'
}

function describeEvent(event: MarketJobEvent, identities: Map<string, IdentityContext>): TimelineRow {
  const payload = event.payload
  const identity = event.worker_id ? identities.get(event.worker_id) : undefined
  const details: EventDetail[] = []
  let title: string = event.type
  let summary = ''
  let tone: EventTone = 'neutral'

  if (event.type === 'worker.identity_bound') {
    const bound = identityFromEvent(event)
    title = `${bound.username || 'Аккаунт'} назначен ${workerLabel(event.worker_id)}`
    summary = routeSummary(payload, bound) || 'Аккаунт получил отдельный сетевой маршрут.'
    addDetail(details, 'Регистрация', bound.registrationId)
    addDetail(details, 'Режим привязки', bound.bindingMode)
    addDetail(details, 'IP регистрации', bound.signupIp)
    addDetail(details, 'Persona', bound.personaId)
    addDetail(details, 'Маршрут', bound.transportId)
    tone = 'positive'
  } else if (event.type === 'request.finished') {
    const outcome = outcomeLabel(payload.outcome)
    const duration = durationLabel(payload.duration_ms)
    title = `${sourceLabel(payload.source)}: ${outcome}${duration ? ` за ${duration}` : ''}`
    summary = [identity?.username || workerLabel(event.worker_id), routeSummary(payload, identity)].filter(Boolean).join(' · ')
    addDetail(details, 'Операция', event.operation_id)
    addDetail(details, 'Аккаунт', identity?.registrationId || payload.account_registration_id)
    addDetail(details, 'Пик параллельности', payload.peak_requests)
    addDetail(details, 'Активно после ответа', payload.active_requests)
    addDetail(details, 'Тип ошибки', payload.error_type)
    addDetail(details, 'Ошибка', payload.error || payload.detail)
    tone = ['ошибка', 'тайм-аут'].includes(outcome) ? 'warning' : 'network'
  } else if (event.type === 'request.started') {
    title = `Запрос к ${sourceLabel(payload.source)} выполняется`
    summary = [identity?.username || workerLabel(event.worker_id), routeSummary(payload, identity)].filter(Boolean).join(' · ')
    addDetail(details, 'Операция', event.operation_id)
    addDetail(details, 'Активных запросов', payload.active_requests)
    addDetail(details, 'Пик параллельности', payload.peak_requests)
    tone = 'network'
  } else if (event.type === 'publication.state_changed') {
    const publication = publicationDescription(event)
    title = publication.title
    summary = publication.summary
    tone = publication.tone
    addDetail(details, 'Рекомендация', payload.recommendation_id)
    addDetail(details, 'Контур', payload.handoff_id)
    addDetail(details, 'Кластер', payload.source_cluster_id)
    addDetail(details, 'Evidence', Array.isArray(payload.evidence_ids) ? payload.evidence_ids.join(', ') : '')
    addDetail(details, 'Ревизия', payload.revision)
  } else if (event.type === 'operation.completed') {
    title = `Действие ${textValue(payload.kind).replace(/_/g, ' ') || 'worker'} выполнено`
    summary = [workerLabel(event.worker_id), durationLabel(payload.duration_ms)].filter(Boolean).join(' · ')
    addDetail(details, 'Операция', event.operation_id)
    addDetail(details, 'Результат', payload.outcome)
    tone = 'positive'
  } else if (event.type === 'operation.failed' || event.type === 'operation.contract_violation') {
    title = event.type === 'operation.failed' ? 'Действие завершилось ошибкой' : 'Ответ источника не прошёл проверку'
    summary = textValue(payload.message || payload.error || payload.reason) || workerLabel(event.worker_id)
    addDetail(details, 'Операция', event.operation_id)
    addDetail(details, 'Тип', payload.kind)
    addDetail(details, 'Код', payload.code)
    tone = 'warning'
  } else if (event.type === 'shard.progress') {
    title = `Сегмент принёс ${formatCount(numberValue(payload.new_unique))} новых карточек`
    summary = `${formatCount(numberValue(payload.duplicate_count))} повторов · всего уникальных ${formatCount(numberValue(payload.unique_cards || payload.total_unique))}`
    addDetail(details, 'Сегмент', payload.shard_id || payload.shard)
    addDetail(details, 'Позиция', payload.cursor || payload.position)
  } else if (event.type === 'transport.state_changed') {
    title = `Маршрут ${textValue(payload.transport_id) || 'VPNTE'}: ${textValue(payload.health || payload.state) || 'состояние изменено'}`
    summary = [textValue(payload.egress_ip), textValue(payload.reason)].filter(Boolean).join(' · ')
    addDetail(details, 'Generation', payload.generation)
    addDetail(details, 'Worker', event.worker_id)
    tone = ['healthy', 'active'].includes(textValue(payload.health || payload.state)) ? 'network' : 'warning'
  } else if (event.type === 'job.state_changed' || event.type === 'job.phase_changed') {
    title = event.type === 'job.state_changed' ? `Запуск: ${stateLabel(payload.state)}` : `Этап: ${textValue(payload.phase).replace(/_/g, ' ')}`
    summary = textValue(payload.reason || payload.message)
    addDetail(details, 'Состояние', payload.state)
    addDetail(details, 'Этап', payload.phase)
  } else if (event.type === 'job.metrics') {
    title = 'Метрики сбора обновлены'
    summary = `${formatCount(numberValue(payload.unique_cards || payload.unique_listings))} уникальных · ${formatCount(numberValue(payload.requests))} запросов`
    addDetail(details, 'Наблюдений', payload.card_occurrences || payload.listing_observations)
    addDetail(details, 'Разных IP', payload.distinct_egress_ips)
  } else if (event.type === 'warning') {
    title = textValue(payload.message) || 'Предупреждение запуска'
    summary = textValue(payload.reason || payload.code)
    addDetail(details, 'Worker', event.worker_id)
    addDetail(details, 'Маршрут', payload.transport_id)
    tone = 'warning'
  } else if (event.type === 'result.ready' || event.type === 'checkpoint.saved') {
    title = event.type === 'result.ready' ? 'Итоговый анализ готов' : 'Сохранён итоговый срез'
    summary = textValue(payload.message || payload.path)
    tone = 'positive'
  } else if (event.type === 'worker.concurrency_recommended') {
    title = `Подтверждена ёмкость ${formatCount(numberValue(payload.recommended_workers || payload.effective_capacity))} workers`
    summary = `${formatCount(numberValue(payload.healthy_routes))} здоровых маршрутов · ${formatCount(numberValue(payload.distinct_egress_ips))} уникальных IP`
  } else if (event.type === 'request.wave_waiting' || event.type === 'request.wave_released') {
    title = event.type === 'request.wave_waiting' ? 'Worker готов к параллельной волне' : 'Параллельная волна запросов выпущена'
    summary = `${formatCount(numberValue(payload.ready_workers))} из ${formatCount(numberValue(payload.expected_workers))} workers готовы`
    tone = 'network'
  } else {
    const labels: Partial<Record<MarketJobEvent['type'], string>> = {
      'worker.registered': 'Worker зарегистрирован',
      'operation.queued': 'Действие добавлено в очередь',
      'operation.started': 'Действие запущено',
      'enrichment.queued': 'Обогащение карточек поставлено в очередь',
      'enrichment.completed': 'Карточка обогащена',
    }
    title = labels[event.type] || event.type
    summary = textValue(payload.message || payload.reason || payload.kind || payload.state)
    addDetail(details, 'Worker', event.worker_id)
    addDetail(details, 'Операция', event.operation_id)
    addDetail(details, 'Источник', payload.source)
  }

  const tags = tagsFor(event, tone)
  const workers = event.worker_id ? [event.worker_id] : []
  const searchText = [title, summary, event.type, ...details.flatMap((item) => [item.label, item.value]), ...workers].join(' ')
  return {
    key: String(event.seq),
    seqs: [event.seq],
    type: event.type,
    title,
    summary,
    emittedAt: event.emitted_at,
    tone,
    tags,
    details,
    workers,
    searchText,
  }
}

function describeWorkerGroup(events: MarketJobEvent[], identities: Map<string, IdentityContext>): TimelineRow {
  const latest = events[events.length - 1]
  const payload = latest.payload
  const actual = textValue(payload.actual_state)
  const desired = textValue(payload.desired_state)
  const workers = events.map((event) => event.worker_id).filter((value): value is string => Boolean(value))
  const identitiesInGroup = workers.map((worker) => identities.get(worker)).filter((value): value is IdentityContext => Boolean(value))
  const routes = new Set(identitiesInGroup.map((identity) => [identity.slot ? `slot ${identity.slot}` : '', identity.egressIp].filter(Boolean).join(' · ')).filter(Boolean))
  const accounts = identitiesInGroup.map((identity) => identity.username).filter(Boolean)
  const errors = [...new Set(events.map((event) => textValue(event.payload.last_error)).filter(Boolean))]
  const title = events.length === 1
    ? `${workerLabel(latest.worker_id)}: ${stateLabel(actual)}`
    : `${formatCount(events.length)} workers: ${stateLabel(actual)}`
  const summaryParts = [`цель: ${stateLabel(desired)}`]
  if (accounts.length) summaryParts.push(`${formatCount(accounts.length)} аккаунтов`)
  if (routes.size) summaryParts.push(`${formatCount(routes.size)} маршрутов`)
  if (errors.length) summaryParts.push(errors[0])
  const details: EventDetail[] = []
  if (textValue(payload.previous_actual_state)) {
    addDetail(details, 'Переход', `${stateLabel(payload.previous_actual_state)} → ${stateLabel(actual)}`)
  } else {
    addDetail(details, 'Состояние', stateLabel(actual))
  }
  addDetail(details, 'Ожидаемое состояние', stateLabel(desired))
  addDetail(details, 'Поколение', payload.generation)
  addDetail(details, 'Текущая операция', payload.current_operation_id)
  addDetail(details, 'Аккаунты', accounts.join(', '))
  addDetail(details, 'Маршруты', [...routes].join(', '))
  addDetail(details, 'Ошибка', errors.join('; '))
  const tone: EventTone = ['failed', 'backoff'].includes(actual) ? 'warning' : actual === 'busy' ? 'network' : actual === 'stopped' ? 'positive' : 'neutral'
  return {
    key: `worker-state-${latest.seq}`,
    seqs: events.map((event) => event.seq),
    type: 'worker.state_changed',
    title,
    summary: summaryParts.join(' · '),
    emittedAt: latest.emitted_at,
    tone,
    tags: new Set<EventTag>(tone === 'warning' ? ['workers', 'warnings'] : ['workers']),
    details,
    workers,
    searchText: [title, ...summaryParts, ...accounts, ...routes, ...workers, ...errors].join(' '),
  }
}

function buildTimeline(events: MarketJobEvent[]): TimelineRow[] {
  const ordered = [...events].sort((left, right) => left.seq - right.seq)
  const identities = new Map<string, IdentityContext>()
  const finishedOperations = new Set<string>()
  for (const event of ordered) {
    if (event.type === 'worker.identity_bound' && event.worker_id) identities.set(event.worker_id, identityFromEvent(event))
    if (event.type === 'request.finished' && event.operation_id) finishedOperations.add(event.operation_id)
  }

  const rows: TimelineRow[] = []
  const workerGroups = new Map<string, MarketJobEvent[]>()
  for (const event of ordered) {
    if (HIDDEN_EVENT_TYPES.has(event.type)) continue
    if (event.type === 'request.started' && event.operation_id && finishedOperations.has(event.operation_id)) continue
    if (event.type === 'worker.state_changed') {
      const second = event.emitted_at.slice(0, 19)
      const key = [second, textValue(event.payload.actual_state), textValue(event.payload.desired_state)].join('|')
      const group = workerGroups.get(key) || []
      group.push(event)
      workerGroups.set(key, group)
      continue
    }
    rows.push(describeEvent(event, identities))
  }
  for (const group of workerGroups.values()) rows.push(describeWorkerGroup(group, identities))
  return rows.sort((left, right) => Math.max(...right.seqs) - Math.max(...left.seqs)).slice(0, 500)
}

export function EventTimeline({
  events,
  job,
  transports,
}: {
  events: MarketJobEvent[]
  job: MarketJob
  transports: MarketTransport[]
}) {
  const [filter, setFilter] = useState<EventFilter>('all')
  const [query, setQuery] = useState('')
  const rows = useMemo(() => buildTimeline(events), [events])
  const visibleRows = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    return rows
      .filter((row) => filter === 'all' || row.tags.has(filter))
      .filter((row) => !needle || row.searchText.toLocaleLowerCase('ru-RU').includes(needle))
  }, [filter, query, rows])
  const summary = useMemo(() => {
    const successfulRequests = Number(job.counters.requests ?? 0) || events.filter((event) => event.type === 'request.finished' && ['ok', 'success', 'completed'].includes(textValue(event.payload.outcome).toLowerCase())).length
    const warningRows = rows.filter((row) => row.tags.has('warnings')).length
    const identities = events.filter((event) => event.type === 'worker.identity_bound')
    const healthyRoutes = transports.filter((transport) => transport.health === 'healthy').length
    const routes = new Set(identities.map((event) => routeSummary(event.payload)).filter(Boolean))
    const latestPublication = [...events].reverse().find((event) => event.type === 'publication.state_changed')
    return {
      successfulRequests,
      warningRows,
      workers: job.desired_workers,
      routes: healthyRoutes || routes.size,
      publication: publicationStageLabel(latestPublication?.payload.stage),
    }
  }, [events, job.counters.requests, job.desired_workers, rows, transports])

  const filters: Array<{ id: EventFilter; label: string; icon: typeof Activity }> = [
    { id: 'all', label: 'Все', icon: Activity },
    { id: 'warnings', label: 'Сигналы', icon: AlertTriangle },
    { id: 'network', label: 'Сеть', icon: Network },
    { id: 'workers', label: 'Workers', icon: UsersRound },
    { id: 'publication', label: 'Публикация', icon: Send },
  ]

  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div>
          <div className="market-section-label">Активность</div>
          <h2 className="mt-1 text-base font-semibold text-white">Журнал запуска</h2>
        </div>
        <label className="market-search-field">
          <Search className="h-3.5 w-3.5" />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Аккаунт, IP, ошибка" />
        </label>
      </div>
      <div className="market-filter-tabs">
        {filters.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" className={filter === id ? 'is-active' : ''} onClick={() => setFilter(id)}>
            <Icon className="h-3.5 w-3.5" />{label}
          </button>
        ))}
      </div>
      <div className="market-event-summary">
        <div><CheckCircle2 className="h-4 w-4" /><span>Успешные запросы<strong>{formatCount(summary.successfulRequests)}</strong></span></div>
        <div><AlertTriangle className="h-4 w-4" /><span>Сигналы<strong>{formatCount(summary.warningRows)}</strong></span></div>
        <div><Route className="h-4 w-4" /><span>Workers / здоровые маршруты<strong>{formatCount(summary.workers)} / {formatCount(summary.routes)}</strong></span></div>
        <div><Send className="h-4 w-4" /><span>Публикация<strong>{summary.publication}</strong></span></div>
      </div>
      <ol className="market-event-list">
        {visibleRows.map((row) => (
          <li key={row.key} className={`market-event market-event-${row.tone}`}>
            <span className="market-event-dot" />
            <div className="min-w-0">
              <div className="flex items-start justify-between gap-4">
                <strong>{row.title}</strong>
                <time>{formatTimestamp(row.emittedAt)}</time>
              </div>
              {row.summary && <p>{row.summary}</p>}
              {(row.details.length > 0 || row.workers.length > 1) && (
                <details className="market-event-disclosure">
                  <summary><ChevronDown className="h-3 w-3" /> Детали{row.seqs.length > 1 ? ` · ${row.seqs.length} событий` : ''}</summary>
                  <div className="market-event-detail-grid">
                    {row.details.map((detail) => <div key={`${detail.label}-${detail.value}`}><span>{detail.label}</span><b>{detail.value}</b></div>)}
                  </div>
                  {row.workers.length > 1 && <div className="market-event-workers">{row.workers.map(workerLabel).join(' · ')}</div>}
                </details>
              )}
              <div className="mt-1 font-mono text-[10px] text-zinc-700">#{Math.min(...row.seqs)}{row.seqs.length > 1 ? `–${Math.max(...row.seqs)}` : ''} · {row.type}</div>
            </div>
          </li>
        ))}
        {!visibleRows.length && <li className="market-empty-state">По выбранному фильтру событий нет.</li>}
      </ol>
      <div className="market-table-footer"><span>Показано {visibleRows.length} строк из {rows.length}; исходных событий {events.length}</span></div>
    </section>
  )
}
