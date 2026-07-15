import { ArrowLeft, Pause, Play, RotateCcw, Square, X } from 'lucide-react'
import { Link } from 'react-router-dom'

import type { MarketJob, MarketStreamState } from '../types'
import { IconButton, MarketStateBadge, StreamState, phaseLabel } from './shared'

export interface JobToolbarProps {
  job: MarketJob
  streamState: MarketStreamState
  busy?: boolean
  onPause(): void
  onResume(): void
  onStop(force: boolean): void
  onRefresh(): void
}

export function JobToolbar({ job, streamState, busy = false, onPause, onResume, onStop, onRefresh }: JobToolbarProps) {
  const paused = job.state === 'paused' || job.state === 'pausing'
  const terminal = ['completed', 'stopped', 'failed'].includes(job.state)
  const uniqueCards = Number(job.counters.unique_cards ?? job.counters.unique_listings ?? 0)
  const progress = Math.min(100, Math.round((uniqueCards / Math.max(1, job.target_unique_cards)) * 100))
  return (
    <header className="market-job-header sticky top-0 z-20">
      <div className="market-job-header-inner">
        <Link to="/kwork-market" className="market-icon-action shrink-0" title="Вернуться к запускам" aria-label="Вернуться к запускам"><ArrowLeft className="h-4 w-4" /></Link>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate text-lg font-semibold text-white">{job.scope.category_name || job.scope.canonical_alias || `Рубрика ${job.scope.category_id}`}</h1>
            <MarketStateBadge state={job.state} />
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
            <span className="truncate font-mono text-zinc-600">{job.job_id}</span>
            <span>{phaseLabel(job.phase)}</span>
            <StreamState state={streamState} />
          </div>
        </div>
        <div className="market-header-progress max-lg:hidden">
          <div><span>Прогресс</span><strong>{progress}%</strong></div>
          <div className="market-progress"><span style={{ width: `${Math.max(progress, uniqueCards ? 2 : 0)}%` }} /></div>
          <small>{uniqueCards.toLocaleString('ru-RU')} / {job.target_unique_cards.toLocaleString('ru-RU')}</small>
        </div>
        <div className="flex items-center gap-1">
          <IconButton title="Обновить задачу" onClick={onRefresh} disabled={busy}><RotateCcw className="h-4 w-4" /></IconButton>
          {paused ? (
            <IconButton title="Продолжить сбор" onClick={onResume} disabled={busy || terminal}><Play className="h-4 w-4" /></IconButton>
          ) : (
            <IconButton title="Приостановить сбор" onClick={onPause} disabled={busy || terminal}><Pause className="h-4 w-4" /></IconButton>
          )}
          <IconButton title="Остановить после текущих задач" onClick={() => onStop(false)} disabled={busy || terminal} className="text-red-300 hover:text-red-200"><Square className="h-4 w-4" /></IconButton>
          <IconButton title="Остановить немедленно" onClick={() => onStop(true)} disabled={busy || terminal} className="text-red-300 hover:text-red-200"><X className="h-4 w-4" /></IconButton>
        </div>
      </div>
    </header>
  )
}
