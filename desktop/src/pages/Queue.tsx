import { useState, useCallback } from 'react'
import {
  CheckCircle2, XCircle, Clock, Pencil, ExternalLink,
  RefreshCw, Filter, ChevronDown, ChevronUp, Send, DollarSign
} from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, Candidate } from '../lib/api'
import { cn, STATUS_BG, STATUS_LABEL, PLATFORM_LABEL, RISK_COLOR, RISK_LABEL, fmtTs } from '../lib/utils'

const QUEUE_STATUSES = 'queued,auto_ready,snoozed'
const ALL_STATUSES = 'queued,auto_ready,snoozed,manual_sent,auto_sent,draft,skipped,error'

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cn('badge', STATUS_BG[status] ?? 'bg-zinc-500/15 text-zinc-300 border-zinc-500/30')}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function CandidateRow({
  c,
  onAction,
  selected,
  onClick,
}: {
  c: Candidate
  onAction: (id: number, action: string) => void
  selected: boolean
  onClick: () => void
}) {
  return (
    <tr
      onClick={onClick}
      className={cn(
        'border-b border-surface-700/50 cursor-pointer transition-colors text-sm',
        selected ? 'bg-brand-600/10' : 'hover:bg-surface-700/30'
      )}
    >
      <td className="px-3 py-2 text-zinc-500 text-xs">{c.candidate_id}</td>
      <td className="px-3 py-2">
        <div className="font-medium text-white line-clamp-1 max-w-xs">{c.title}</div>
        <div className="text-xs text-zinc-500">{PLATFORM_LABEL[c.platform] ?? c.platform}</div>
      </td>
      <td className="px-3 py-2"><StatusBadge status={c.status} /></td>
      <td className="px-3 py-2 text-center">
        {c.ai_score != null ? (
          <span className={cn(
            'text-sm font-bold',
            c.ai_score >= 8 ? 'text-emerald-400' : c.ai_score >= 6 ? 'text-yellow-400' : 'text-red-400'
          )}>{c.ai_score}</span>
        ) : <span className="text-zinc-600">—</span>}
      </td>
      <td className="px-3 py-2 text-center">
        {c.vet_score != null ? (
          <span className="text-sm text-amber-200">{c.vet_score}</span>
        ) : <span className="text-zinc-600">—</span>}
      </td>
      <td className="px-3 py-2 text-center">
        {c.risk_level && (
          <span className={cn('text-xs font-medium', RISK_COLOR[c.risk_level])}>
            {RISK_LABEL[c.risk_level] ?? c.risk_level}
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-right text-xs text-zinc-500">{fmtTs(c.updated_at)}</td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-1 justify-end" onClick={(e) => e.stopPropagation()}>
          {(c.status === 'queued' || c.status === 'auto_ready') && (
            <button
              onClick={() => onAction(c.candidate_id, 'approve')}
              className="btn btn-success py-0.5 px-2 text-xs"
              title="Отправить отклик"
            >
              <Send className="w-3 h-3" />
            </button>
          )}
          {c.status !== 'skipped' && c.status !== 'auto_sent' && c.status !== 'manual_sent' && (
            <button
              onClick={() => onAction(c.candidate_id, 'skip')}
              className="btn btn-danger py-0.5 px-2 text-xs"
              title="Пропустить"
            >
              <XCircle className="w-3 h-3" />
            </button>
          )}
          <button
            onClick={() => onAction(c.candidate_id, 'snooze')}
            className="btn btn-ghost py-0.5 px-2 text-xs"
            title="Отложить на 60 минут"
          >
            <Clock className="w-3 h-3" />
          </button>
          {c.url && (
            <a href={c.url} target="_blank" rel="noreferrer" className="btn btn-ghost py-0.5 px-2 text-xs">
              <ExternalLink className="w-3 h-3" />
            </a>
          )}
        </div>
      </td>
    </tr>
  )
}

function CandidateDetail({
  c,
  onApprove,
  onClose,
}: {
  c: Candidate
  onApprove: (text: string, price: string) => void
  onClose: () => void
}) {
  const [text, setText] = useState(c.proposal_text ?? '')
  const [price, setPrice] = useState(c.chosen_price ?? String(c.budget ?? ''))
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  async function handleSaveText() {
    setSaving(true)
    try {
      await api.editText(c.candidate_id, text)
      setMsg('Текст сохранён')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Ошибка')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card h-full flex flex-col gap-3 overflow-hidden">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-white text-sm line-clamp-1">{c.title}</h2>
        <button onClick={onClose} className="btn btn-ghost py-1 px-2 text-xs">✕</button>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="space-y-1">
          <div className="flex gap-2"><span className="text-zinc-500">Платформа:</span><span>{PLATFORM_LABEL[c.platform] ?? c.platform}</span></div>
          <div className="flex gap-2"><span className="text-zinc-500">Статус:</span><StatusBadge status={c.status} /></div>
          <div className="flex gap-2"><span className="text-zinc-500">AI оценка:</span><span className="text-brand-400">{c.ai_score ?? '—'}</span></div>
          <div className="flex gap-2"><span className="text-zinc-500">Vet оценка:</span><span className="text-amber-200">{c.vet_score ?? '—'}</span></div>
          <div className="flex gap-2"><span className="text-zinc-500">Отклики:</span><span>{c.offers_count ?? '—'}</span></div>
        </div>
        <div className="space-y-1">
          <div className="flex gap-2"><span className="text-zinc-500">Бюджет:</span><span>{c.budget ?? '—'} {c.currency}</span></div>
          <div className="flex gap-2">
            <span className="text-zinc-500">Риск:</span>
            <span className={RISK_COLOR[c.risk_level ?? '']}>{RISK_LABEL[c.risk_level ?? ''] ?? c.risk_level ?? '—'}</span>
          </div>
          <div className="flex gap-2"><span className="text-zinc-500">Запрос:</span><span className="text-zinc-400">{c.search_query ?? '—'}</span></div>
          {c.url && (
            <a href={c.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-brand-400 hover:underline">
              <ExternalLink className="w-3 h-3" /> Открыть
            </a>
          )}
        </div>
      </div>

      {c.vet_red_flags && c.vet_red_flags.length > 0 && (
        <div className="text-xs">
          <span className="text-red-400 font-medium">Красные флаги: </span>
          {c.vet_red_flags.map((f, i) => (
            <span key={i} className="text-zinc-300">{f}{i < c.vet_red_flags!.length - 1 ? ', ' : ''}</span>
          ))}
        </div>
      )}

      {/* Price */}
      <div>
        <label className="label">Цена</label>
        <div className="flex gap-2">
          <input
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            placeholder={String(c.budget ?? 'авто')}
            className="input flex-1"
          />
          <button onClick={() => api.editPrice(c.candidate_id, price)} className="btn btn-ghost">
            <DollarSign className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Proposal text */}
      <div className="flex-1 flex flex-col min-h-0">
        <label className="label">Текст отклика</label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          className="input flex-1 resize-none font-mono text-xs min-h-32"
        />
      </div>

      {msg && <p className="text-xs text-emerald-400">{msg}</p>}

      <div className="flex gap-2">
        <button onClick={handleSaveText} disabled={saving} className="btn btn-ghost flex-1">
          <Pencil className="w-3.5 h-3.5" /> Сохранить текст
        </button>
        <button onClick={() => onApprove(text, price)} className="btn btn-success flex-1">
          <Send className="w-3.5 h-3.5" /> Отправить
        </button>
      </div>
    </div>
  )
}

export default function Queue() {
  const [showAll, setShowAll] = useState(false)
  const [platformFilter, setPlatformFilter] = useState('')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<Candidate | null>(null)
  const [actionMsg, setActionMsg] = useState('')

  const statusFilter = showAll ? ALL_STATUSES : QUEUE_STATUSES

  const { data, loading, refetch } = useApi(
    () => api.getCandidates({
      status: statusFilter,
      platform: platformFilter || undefined,
      page,
      page_size: 40,
    }),
    [statusFilter, platformFilter, page]
  )

  const handleAction = useCallback(async (id: number, action: string) => {
    try {
      if (action === 'approve') {
        const result = await api.approveCandidate(id)
        setActionMsg(result.message)
      } else if (action === 'skip') {
        await api.skipCandidate(id)
        setActionMsg('Пропущено')
      } else if (action === 'snooze') {
        await api.snoozeCandidate(id, 60)
        setActionMsg('Отложено на 60 мин')
      }
      refetch()
      setTimeout(() => setActionMsg(''), 3000)
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : 'Ошибка')
    }
  }, [refetch])

  const handleApprove = useCallback(async (text: string, price: string) => {
    if (!selected) return
    try {
      const result = await api.approveCandidate(selected.candidate_id, {
        proposal_text: text,
        chosen_price: price || undefined,
      })
      setActionMsg(result.message)
      setSelected(null)
      refetch()
    } catch (e) {
      setActionMsg(e instanceof Error ? e.message : 'Ошибка')
    }
  }, [selected, refetch])

  const items = data?.items ?? []
  const total = data?.total ?? 0

  return (
    <div className="flex h-full">
      {/* Table side */}
      <div className={cn('flex flex-col', selected ? 'flex-1' : 'w-full')}>
        {/* Toolbar */}
        <div className="toolbar flex items-center gap-3 overflow-x-auto px-4 py-3">
          <div>
            <div className="page-kicker">ручное подтверждение</div>
            <h1 className="font-semibold text-white text-sm">Очередь</h1>
          </div>
          <span className="badge bg-brand-600/20 text-brand-300 border-brand-500/30">{total}</span>
          <div className="min-w-3 flex-1" />
          <select
            value={platformFilter}
            onChange={(e) => { setPlatformFilter(e.target.value); setPage(0) }}
            className="input w-36 shrink-0 py-1"
          >
            <option value="">Все платформы</option>
            <option value="kwork">Kwork</option>
            <option value="freelance_ru">Freelance.ru</option>
            <option value="hh_ru">HH.ru</option>
          </select>
          <button
            onClick={() => { setShowAll(!showAll); setPage(0) }}
            className={cn('btn shrink-0 py-1 text-xs', showAll ? 'btn-primary' : 'btn-ghost')}
          >
            <Filter className="w-3.5 h-3.5" />
            {showAll ? 'Все' : 'В очереди'}
          </button>
          <button onClick={refetch} className="btn btn-ghost shrink-0 py-1">
            <RefreshCw className={cn('w-4 h-4', loading && 'animate-spin')} />
          </button>
        </div>

        {actionMsg && (
          <div className="mx-4 mt-2 px-3 py-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded-lg text-xs text-emerald-300">
            {actionMsg}
          </div>
        )}

        {/* Table */}
        <div className="flex-1 overflow-auto">
          <table className="w-full text-xs">
            <thead className="table-head sticky top-0">
              <tr className="text-zinc-500">
                <th className="px-3 py-2 text-left w-10">#</th>
                <th className="px-3 py-2 text-left">Проект</th>
                <th className="px-3 py-2 text-left w-28">Статус</th>
                <th className="px-3 py-2 text-center w-16">AI</th>
                <th className="px-3 py-2 text-center w-16">Vet</th>
                <th className="px-3 py-2 text-center w-16">Риск</th>
                <th className="px-3 py-2 text-right w-20">Обновлён</th>
                <th className="px-3 py-2 text-right w-32">Действия</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={8} className="py-12 text-center text-zinc-500">Загрузка...</td></tr>
              ) : items.length === 0 ? (
                <tr><td colSpan={8} className="py-12 text-center text-zinc-500">Нет кандидатов</td></tr>
              ) : (
                items.map((c) => (
                  <CandidateRow
                    key={c.candidate_id}
                    c={c}
                    onAction={handleAction}
                    selected={selected?.candidate_id === c.candidate_id}
                    onClick={() => setSelected(selected?.candidate_id === c.candidate_id ? null : c)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {total > 40 && (
          <div className="flex items-center justify-between px-4 py-2 border-t border-surface-600 text-xs text-zinc-500">
            <span>Показано {page * 40 + 1}–{Math.min((page + 1) * 40, total)} из {total}</span>
            <div className="flex gap-1">
              <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0} className="btn btn-ghost py-0.5 px-2">
                <ChevronUp className="w-3.5 h-3.5" />
              </button>
              <button onClick={() => setPage(p => p + 1)} disabled={(page + 1) * 40 >= total} className="btn btn-ghost py-0.5 px-2">
                <ChevronDown className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Detail panel */}
      {selected && (
        <div className="w-96 shrink-0 border-l border-surface-600 p-3 overflow-y-auto max-xl:w-80">
          <CandidateDetail
            c={selected}
            onApprove={handleApprove}
            onClose={() => setSelected(null)}
          />
        </div>
      )}
    </div>
  )
}
