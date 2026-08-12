import { Activity, PauseCircle, PlayCircle, RefreshCw, Server, Square } from 'lucide-react'

import type { BuyerSearchFleet, BuyerSearchQuery, BuyerSearchRun } from '../types'

interface Props {
  run: BuyerSearchRun | null
  fleet: BuyerSearchFleet | null
  queries: BuyerSearchQuery[]
  busy: boolean
  onState(state: 'running' | 'paused' | 'stopped'): void
  onRefresh(): void
  onToggleQuery(query: BuyerSearchQuery): void
}

const STATE_LABELS: Record<string, string> = {
  ready: 'Готов',
  running: 'Выполняется',
  paused: 'Пауза',
  completed: 'Завершён',
  stopped: 'Остановлен',
  failed: 'Ошибка',
  blocked: 'Нет мощности',
  planning: 'Планирование',
}

function shortAccount(value: string) {
  return value.length > 12 ? `${value.slice(0, 8)}...` : value
}

function capacityReasonLabel(reason: string, capacity: NonNullable<BuyerSearchFleet['capacity']>) {
  const requested = capacity.requested_workers
  if (reason === 'unique_account_capacity') return `Уникальные аккаунты: ${capacity.eligible_accounts}/${requested}`
  if (reason === 'unique_transport_capacity') return `Уникальные маршруты: ${capacity.healthy_transports}/${requested}`
  if (reason === 'unique_egress_capacity') return `Уникальные IP: ${capacity.unique_egress_ips}/${requested}`
  if (reason === 'canary_cap') return `Ограничение canary: максимум ${capacity.canary_cap}`
  if (reason === 'identity_collisions') return `Коллизии идентичностей: ${capacity.collision_count}`
  return reason
}

export function SearchMonitor({ run, fleet, queries, busy, onState, onRefresh, onToggleQuery }: Props) {
  if (!run) {
    return <div className="buyer-monitor-empty"><Server /><strong>Запуск не выбран</strong><span>Создайте поиск или выберите сохранённый запуск.</span></div>
  }

  const metrics = fleet?.metrics
  const activeWorkers = fleet?.workers ?? []
  const workerHistory = metrics?.worker_history ?? []
  const terminalRun = ['completed', 'stopped', 'failed', 'blocked'].includes(run.state)
  const canStart = !terminalRun && !busy && ['ready', 'paused'].includes(run.state)
  const canPause = !terminalRun && !busy && run.state === 'running'
  const canStop = !terminalRun && !busy && ['planning', 'ready', 'running', 'paused', 'pausing', 'completing'].includes(run.state)
  const executionWorkers = terminalRun
    ? workerHistory.length
    : fleet?.effective_workers ?? activeWorkers.length
  const target = Math.max(1, run.target_unique_projects || 1)
  const uniqueProjects = run.counters?.unique_projects ?? 0

  return (
    <div className="buyer-monitor">
      <header>
        <div><span>Мониторинг запуска</span><h2>{run.name}</h2></div>
        <div className="buyer-monitor-actions">
          <button type="button" className="is-start" title="Запустить" aria-label="Запустить" disabled={!canStart} onClick={() => onState('running')}><PlayCircle /></button>
          <button type="button" className="is-pause" title="Пауза" aria-label="Пауза" disabled={!canPause} onClick={() => onState('paused')}><PauseCircle /></button>
          <button type="button" className="is-stop" title="Остановить" aria-label="Остановить" disabled={!canStop} onClick={() => onState('stopped')}><Square /></button>
          <button type="button" title="Обновить" onClick={onRefresh}><RefreshCw /></button>
        </div>
      </header>

      <div className="buyer-monitor-metrics">
        <div><span>Состояние</span><strong>{STATE_LABELS[run.state] ?? run.state}</strong></div>
        <div><span>Уникальные проекты</span><strong>{uniqueProjects}/{target}</strong><small>найдено / цель</small></div>
        <div><span>Параллельность</span><strong>{executionWorkers}/{run.requested_workers}</strong></div>
        <div><span>Доля уникальных карточек</span><strong>{metrics ? `${metrics.uniqueness_percent}%` : '—'}</strong><small>{metrics ? `${metrics.duplicate_observations} повторных наблюдений из ${metrics.projects_seen}` : 'Нет данных'}</small></div>
        <div><span>Скорость</span><strong>{metrics ? `${metrics.unique_projects_per_second}/с` : '—'}</strong><small>уникальных проектов</small></div>
        <div><span>Задержка</span><strong>{metrics?.average_latency_ms != null ? `${metrics.average_latency_ms} мс` : '—'}</strong><small>{metrics?.completed_requests ?? 0} запросов</small></div>
      </div>

      {fleet?.capacity?.reasons?.length ? <div className="buyer-monitor-notice"><Activity />{fleet.capacity.reasons.map((reason) => capacityReasonLabel(reason, fleet.capacity!)).join(' · ')}</div> : null}

      <section className="buyer-monitor-section">
        <h3>План запросов</h3>
        <div className="buyer-query-table">
          <div className="buyer-query-head"><span>Запрос</span><span>Состояние</span><span>Страницы</span><span>Уникальные</span><span /></div>
          {queries.map((query) => <div key={query.query_id}><span>{query.text}</span><span>{query.state}</span><span>{query.pages_completed}/{query.pages_scheduled}</span><span>{query.unique_projects}</span><button type="button" disabled={terminalRun || busy} onClick={() => onToggleQuery(query)}>{query.enabled ? 'Отключить' : 'Включить'}</button></div>)}
        </div>
      </section>

      <section className="buyer-monitor-section">
        <h3>Маршруты воркеров</h3>
        {activeWorkers.length ? <div className="buyer-worker-list">{activeWorkers.map((worker) => {
          const task = typeof worker.current_task === 'object' && worker.current_task ? worker.current_task : null
          return <div key={worker.worker_id}><span className={`buyer-worker-dot is-${worker.state}`} /><strong title={worker.account_registration_id}>{shortAccount(worker.account_registration_id || worker.worker_id)}</strong><span>{worker.transport_id}</span><span>{worker.egress_ip}</span><span>{task?.query_text || worker.last_error || worker.last_outcome || 'Ожидает задачу'}{task?.page ? ` · стр. ${task.page}` : ''}</span></div>
        })}</div> : workerHistory.length ? <div className="buyer-worker-list">{workerHistory.map((worker) => <div key={`${worker.account_registration_id}:${worker.transport_id}`}><span className="buyer-worker-dot" /><strong title={worker.account_registration_id}>{shortAccount(worker.account_registration_id)}</strong><span>{worker.transport_id}</span><span>{worker.egress_ip}</span><span>{worker.pages} стр. · {worker.projects_seen} карточек</span></div>)}</div> : <div className="buyer-monitor-empty compact">Исполнители ещё не запускались</div>}
      </section>
    </div>
  )
}
