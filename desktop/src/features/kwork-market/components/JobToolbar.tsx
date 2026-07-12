import { Pause, Play, RotateCcw, Square, X } from 'lucide-react'

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
  return (
    <header className="toolbar sticky top-0 z-10 flex min-h-16 flex-wrap items-center justify-between gap-3 px-5 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="truncate text-base font-semibold text-white">{job.scope.category_name || job.scope.canonical_alias || `Рубрика ${job.scope.category_id}`}</h1>
          <MarketStateBadge state={job.state} />
          <span className="mono-label">{phaseLabel(job.phase)}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500">
          <span>цель {job.target_unique_cards.toLocaleString()}</span>
          <span>исполнителей {job.desired_workers}</span>
          <StreamState state={streamState} />
        </div>
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
    </header>
  )
}
