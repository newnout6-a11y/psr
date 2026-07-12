import { RotateCcw } from 'lucide-react'

import type { MarketJob } from '../types'
import { IconButton } from './shared'

export interface JobConfigDraft {
  targetUniqueCards: string
  desiredWorkers: string
  profile: string
  requestBudget: string
  timeBudgetSeconds: string
}

export type JobConfigField = keyof JobConfigDraft

interface JobConfigControlsProps {
  job: MarketJob
  draft: JobConfigDraft
  dirty: boolean
  busy: boolean
  error: string | null
  onChange(field: JobConfigField, value: string): void
  onReset(): void
  onSubmit(): void
}

export function jobConfigDraftFromJob(job: MarketJob): JobConfigDraft {
  return {
    targetUniqueCards: String(job.target_unique_cards),
    desiredWorkers: String(job.desired_workers),
    profile: job.profile,
    requestBudget: job.request_budget == null ? '' : String(job.request_budget),
    timeBudgetSeconds: job.time_budget_seconds == null ? '' : String(job.time_budget_seconds),
  }
}

export function JobConfigControls({
  job,
  draft,
  dirty,
  busy,
  error,
  onChange,
  onReset,
  onSubmit,
}: JobConfigControlsProps) {
  return (
    <section className="factory-panel px-4 py-3" aria-label="Параметры запуска">
      <form
        className="flex flex-wrap items-end gap-x-3 gap-y-2"
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <div className="mr-1 min-w-20">
          <div className="text-sm font-medium text-white">Параметры сбора</div>
          <div className="mono-label mt-1">версия {job.revision}</div>
        </div>
        <label className="grid min-w-28 gap-1 text-xs text-zinc-500">
          Цель, карточек
          <input className="input h-8 min-w-0 px-2 text-xs" type="number" min="1" max="10000" value={draft.targetUniqueCards} disabled={busy} onChange={(event) => onChange('targetUniqueCards', event.target.value)} />
        </label>
        <label className="grid min-w-24 gap-1 text-xs text-zinc-500">
          Исполнители
          <input className="input h-8 min-w-0 px-2 text-xs" type="number" min="1" max="10" value={draft.desiredWorkers} disabled={busy} onChange={(event) => onChange('desiredWorkers', event.target.value)} />
        </label>
        <label className="grid min-w-40 flex-1 gap-1 text-xs text-zinc-500">
          Профиль
          <input className="input h-8 min-w-0 px-2 text-xs" type="text" maxLength={100} value={draft.profile} disabled={busy} onChange={(event) => onChange('profile', event.target.value)} />
        </label>
        <label className="grid min-w-28 gap-1 text-xs text-zinc-500">
          Лимит запросов
          <input className="input h-8 min-w-0 px-2 text-xs" type="number" min="1" placeholder="Без лимита" value={draft.requestBudget} disabled={busy} onChange={(event) => onChange('requestBudget', event.target.value)} />
        </label>
        <label className="grid min-w-28 gap-1 text-xs text-zinc-500">
          Лимит времени, с
          <input className="input h-8 min-w-0 px-2 text-xs" type="number" min="1" placeholder="Без лимита" value={draft.timeBudgetSeconds} disabled={busy} onChange={(event) => onChange('timeBudgetSeconds', event.target.value)} />
        </label>
        <div className="flex items-center gap-1 pb-px">
          <IconButton title="Сбросить изменения параметров" onClick={onReset} disabled={busy || !dirty}>
            <RotateCcw className="h-3.5 w-3.5" />
          </IconButton>
          <button type="submit" className="btn h-8 px-3 text-xs" disabled={busy || !dirty}>
            {busy ? 'Сохраняем...' : 'Применить'}
          </button>
        </div>
      </form>
      {error && <div className="mt-3 border border-red-500/35 bg-red-500/10 px-3 py-2 text-xs text-red-100" role="alert">{error}</div>}
    </section>
  )
}
