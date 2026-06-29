import { useState, useMemo } from 'react'
import { Filter, Trash2, Search } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, SkippedCandidate } from '../lib/api'
import { cn } from '../lib/utils'

const REASON_LABELS: Record<string, string> = {
  'keyword filter': 'ключевые слова',
  'honeypot': 'honeypot',
  'nlp spam': 'NLP спам',
  'nlp irrelevant': 'NLP нерелевант',
  'already sent': 'уже отправлен',
  'already queued': 'уже в очереди',
  'already auto_sent': 'уже авто-отправлен',
  'already manual_sent': 'уже отправлен вручную',
  'already draft': 'черновик',
  'already sending': 'отправляется',
  'already scored': 'оценён',
  'already vetted': 'проветтирован',
  'already auto_ready': 'auto_ready',
  'already filtered': 'отфильтрован',
  'already parsed': 'распарсен',
  'already skipped': 'уже пропущен',
  'client blacklisted': 'ЧС клиента',
  'cycle auto-send limit reached': 'лимит откликов',
  'outside_work_hours': 'вне рабочих часов',
  'skipped by operator': 'оператор пропустил',
}

function reasonLabel(reason: string): string {
  if (!reason) return '—'
  const lower = reason.toLowerCase()
  for (const [key, label] of Object.entries(REASON_LABELS)) {
    if (lower.includes(key)) return label
  }
  if (lower.startsWith('score ')) return 'AI-скоринг'
  if (lower.startsWith('vet score')) return 'веттинг'
  return reason.slice(0, 80)
}

function reasonColor(reason: string): string {
  const lower = (reason || '').toLowerCase()
  if (lower.includes('blacklist')) return 'text-red-400'
  if (lower.includes('keyword') || lower.includes('honeypot')) return 'text-orange-400'
  if (lower.includes('nlp') || lower.includes('spam')) return 'text-yellow-400'
  if (lower.startsWith('score') || lower.includes('ai')) return 'text-blue-400'
  if (lower.includes('vet')) return 'text-purple-400'
  if (lower.includes('already')) return 'text-zinc-400'
  if (lower.includes('limit') || lower.includes('hours')) return 'text-amber-400'
  if (lower.includes('operator')) return 'text-pink-400'
  return 'text-zinc-400'
}

export default function Skipped() {
  const { data, loading, refetch } = useApi(() => (api as any).getSkipped(200), [])
  const [reasonFilter, setReasonFilter] = useState<string>('')
  const [search, setSearch] = useState('')

  const items: SkippedCandidate[] = data?.items ?? []

  const byReason = useMemo(() => {
    const m: Record<string, number> = {}
    for (const e of items) {
      const label = reasonLabel(e.decision_reason)
      m[label] = (m[label] || 0) + 1
    }
    return Object.entries(m).sort((a, b) => b[1] - a[1])
  }, [items])

  const filtered = useMemo(() => {
    let result = items
    if (reasonFilter) {
      result = result.filter(e => reasonLabel(e.decision_reason) === reasonFilter)
    }
    if (search.trim()) {
      const q = search.toLowerCase()
      result = result.filter(e =>
        e.title.toLowerCase().includes(q) ||
        e.project_id.toLowerCase().includes(q) ||
        e.decision_reason.toLowerCase().includes(q) ||
        (e.search_query || '').toLowerCase().includes(q)
      )
    }
    return result
  }, [items, reasonFilter, search])

  return (
    <div className="space-y-4 p-6 max-lg:p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="page-kicker">pipeline</div>
          <h1 className="text-xl font-semibold text-white">Пропущенные заказы</h1>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge border-white/10 bg-white/[0.03] text-stone-300">{items.length} всего</span>
          <button onClick={refetch} className="btn btn-ghost py-1.5">
            <Filter className="w-4 h-4" />
          </button>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-16 text-center">
          <Trash2 className="h-10 w-10 text-zinc-700" />
          <p className="mt-3 text-sm text-zinc-500">Нет пропущенных заказов</p>
          <p className="text-xs text-zinc-600">Пропуски появятся после запуска цикла</p>
        </div>
      ) : (
        <>
          {/* Search + filters */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-600" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="поиск..."
                className="input h-9 w-48 pl-8 text-xs"
              />
            </div>
            <button
              onClick={() => { setReasonFilter(''); setSearch('') }}
              className={cn(
                'rounded-md border px-2 py-1 text-xs transition-colors',
                !reasonFilter && !search ? 'border-brand-500/40 bg-brand-600/15 text-white' : 'border-surface-600 text-zinc-400 hover:text-white'
              )}
            >
              все ({items.length})
            </button>
            {byReason.map(([reason, count]) => (
              <button
                key={reason}
                onClick={() => setReasonFilter(reason === reasonFilter ? '' : reason)}
                className={cn(
                  'rounded-md border px-2 py-1 text-xs transition-colors',
                  reasonFilter === reason ? 'border-brand-500/40 bg-brand-600/15 text-white' : 'border-surface-600 text-zinc-400 hover:text-white'
                )}
              >
                {reason} ({count})
              </button>
            ))}
          </div>

          {/* Table */}
          <div className="card overflow-visible">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-surface-700 text-zinc-500">
                  <th className="text-left py-2 px-2 w-28">Причина</th>
                  <th className="text-left py-2 px-2">Заказ</th>
                  <th className="text-right py-2 px-2 w-20">Бюджет</th>
                  <th className="text-center py-2 px-2 w-14">AI</th>
                  <th className="text-left py-2 px-2 w-24">Платформа</th>
                  <th className="text-left py-2 px-2 w-28">Запрос</th>
                  <th className="text-right py-2 px-2 w-24">Обновлён</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((entry) => (
                  <tr key={entry.candidate_id} className="border-b border-surface-700/40 hover:bg-surface-800/50">
                    <td className="py-2 px-2">
                      <span className={cn('font-medium', reasonColor(entry.decision_reason))}>
                        {reasonLabel(entry.decision_reason)}
                      </span>
                    </td>
                    <td className="py-2 px-2">
                      <div className="text-zinc-200 truncate max-w-md">{entry.title || '—'}</div>
                      <div className="text-zinc-600 text-[10px]">id={entry.project_id.slice(0, 16)}</div>
                    </td>
                    <td className="py-2 px-2 text-right text-zinc-400">
                      {entry.budget && entry.budget !== 'None' ? `${entry.budget}₽` : '—'}
                    </td>
                    <td className="py-2 px-2 text-center text-zinc-500">
                      {entry.ai_score != null ? entry.ai_score : '—'}
                    </td>
                    <td className="py-2 px-2 text-zinc-500">{entry.platform}</td>
                    <td className="py-2 px-2 text-zinc-600 truncate max-w-28">{entry.search_query || '—'}</td>
                    <td className="py-2 px-2 text-right text-zinc-600">{entry.updated_at?.slice(5, 16) || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {filtered.length === 0 && (
              <div className="py-8 text-center text-sm text-zinc-500">Ничего не найдено</div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
