import { useState } from 'react'
import { ChevronDown, RotateCcw, Settings2 } from 'lucide-react'

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
  const [open, setOpen] = useState(false)
  return (
    <section className="market-surface overflow-hidden" aria-label="Параметры запуска">
      <button type="button" className="market-config-summary" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span className="market-icon-tile"><Settings2 className="h-4 w-4" /></span>
        <span className="min-w-0 flex-1 text-left">
          <span className="market-section-label">Параметры запуска · версия {job.revision}</span>
          <strong>{draft.targetUniqueCards} карточек · {draft.desiredWorkers} workers · профиль {draft.profile}</strong>
        </span>
        {dirty && <span className="market-unsaved-mark">есть изменения</span>}
        <ChevronDown className={`h-4 w-4 text-zinc-500 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && <form
        className="market-config-grid"
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
      >
        <label>
          <span>Цель, карточек</span>
          <input type="number" min="1" max="10000" value={draft.targetUniqueCards} disabled={busy} onChange={(event) => onChange('targetUniqueCards', event.target.value)} />
        </label>
        <label>
          <span>Исполнители</span>
          <input type="number" min="1" value={draft.desiredWorkers} disabled={busy} onChange={(event) => onChange('desiredWorkers', event.target.value)} />
        </label>
        <label className="min-w-0">
          <span>Профиль</span>
          <input type="text" maxLength={100} value={draft.profile} disabled={busy} onChange={(event) => onChange('profile', event.target.value)} />
        </label>
        <label>
          <span>Лимит запросов</span>
          <input type="number" min="1" placeholder="Без лимита" value={draft.requestBudget} disabled={busy} onChange={(event) => onChange('requestBudget', event.target.value)} />
        </label>
        <label>
          <span>Лимит времени, с</span>
          <input type="number" min="1" placeholder="Без лимита" value={draft.timeBudgetSeconds} disabled={busy} onChange={(event) => onChange('timeBudgetSeconds', event.target.value)} />
        </label>
        <div className="flex items-end justify-end gap-1">
          <IconButton title="Сбросить изменения параметров" onClick={onReset} disabled={busy || !dirty}>
            <RotateCcw className="h-3.5 w-3.5" />
          </IconButton>
          <button type="submit" className="market-primary-action h-9" disabled={busy || !dirty}>
            {busy ? 'Сохраняем...' : 'Применить'}
          </button>
        </div>
      </form>}
      {error && <div className="market-error-banner" role="alert">{error}</div>}
    </section>
  )
}
