import { useState } from 'react'
import { Search, Loader2, AlertTriangle, CheckCircle2, ShieldAlert, Globe } from 'lucide-react'
import { api, OSINTResult } from '../lib/api'
import { cn } from '../lib/utils'

export default function OsintPage() {
  const [form, setForm] = useState({
    username: '',
    email: '',
    phone: '',
    telegram: '',
    description: '',
  })
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<OSINTResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleCheck() {
    if (!form.username.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await api.osintCheck({
        username: form.username.trim(),
        email: form.email || undefined,
        phone: form.phone || undefined,
        telegram: form.telegram || undefined,
        description: form.description || undefined,
      })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  const scoreColor = result
    ? result.reputation_score >= 70
      ? 'text-emerald-400'
      : result.reputation_score >= 40
      ? 'text-yellow-400'
      : 'text-red-400'
    : ''

  return (
    <div className="flex h-full max-lg:flex-col">
      {/* Form panel */}
      <div className="w-72 shrink-0 border-r border-white/10 bg-surface-800/80 p-5 flex flex-col gap-4 max-lg:w-full max-lg:border-b max-lg:border-r-0">
        <div>
          <div className="page-kicker">проверка клиента</div>
          <h1 className="font-semibold text-white text-sm">OSINT — проверка клиента</h1>
        </div>

        <div>
          <label className="label">Username / никнейм *</label>
          <input
            value={form.username}
            onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
            onKeyDown={(e) => e.key === 'Enter' && handleCheck()}
            placeholder="user123"
            className="input"
          />
        </div>

        <div>
          <label className="label">Email</label>
          <input
            value={form.email}
            onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
            placeholder="user@example.com"
            className="input"
            type="email"
          />
        </div>

        <div>
          <label className="label">Телефон</label>
          <input
            value={form.phone}
            onChange={(e) => setForm((f) => ({ ...f, phone: e.target.value }))}
            placeholder="+7..."
            className="input"
          />
        </div>

        <div>
          <label className="label">Telegram</label>
          <input
            value={form.telegram}
            onChange={(e) => setForm((f) => ({ ...f, telegram: e.target.value }))}
            placeholder="@username"
            className="input"
          />
        </div>

        <div>
          <label className="label">Описание проекта (контекст)</label>
          <textarea
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            placeholder="Краткое описание задачи..."
            rows={3}
            className="input resize-none"
          />
        </div>

        <button
          onClick={handleCheck}
          disabled={loading || !form.username.trim()}
          className="btn btn-primary justify-center"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
          Проверить
        </button>

        {error && (
          <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg p-2">
            {error}
          </div>
        )}
      </div>

      {/* Results panel */}
      <div className="flex-1 overflow-y-auto p-6 max-lg:p-4">
        {!result && !loading && (
          <div className="flex flex-col items-center justify-center h-full text-zinc-600">
            <Search className="w-12 h-12 mb-3 opacity-30" />
            <p className="text-sm">Введите никнейм и нажмите «Проверить»</p>
          </div>
        )}

        {loading && (
          <div className="flex items-center justify-center h-full gap-3 text-zinc-400">
            <Loader2 className="w-6 h-6 animate-spin" />
            <span>Сбор OSINT данных...</span>
          </div>
        )}

        {result && (
          <div className="max-w-3xl space-y-6">
            {/* Header */}
            <div className="card flex items-center gap-6">
              <div className="text-center">
                <div className={cn('text-4xl font-bold', scoreColor)}>
                  {result.reputation_score}
                </div>
                <div className="text-xs text-zinc-500 mt-1">Репутация</div>
              </div>
              <div className="flex-1">
                <h2 className="text-lg font-semibold text-white mb-1">{result.username}</h2>
                <p className="text-sm text-zinc-300">{result.summary || '—'}</p>
              </div>
            </div>

            {/* Red flags */}
            {result.red_flags && result.red_flags.length > 0 && (
              <div className="card border-red-500/20">
                <h3 className="text-sm font-medium text-red-400 flex items-center gap-1.5 mb-2">
                  <ShieldAlert className="w-4 h-4" /> Красные флаги
                </h3>
                <ul className="space-y-1">
                  {result.red_flags.map((f, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm text-red-300">
                      <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                      {f}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Positive signals */}
            {result.positive_signals && result.positive_signals.length > 0 && (
              <div className="card border-emerald-500/20">
                <h3 className="text-sm font-medium text-emerald-400 flex items-center gap-1.5 mb-2">
                  <CheckCircle2 className="w-4 h-4" /> Позитивные сигналы
                </h3>
                <ul className="space-y-1">
                  {result.positive_signals.map((s, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm text-emerald-300">
                      <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                      {s}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Contacts */}
            {result.contacts && Object.keys(result.contacts).length > 0 && (
              <div className="card">
                <h3 className="text-sm font-medium text-zinc-300 mb-2">Найденные контакты</h3>
                <div className="space-y-1">
                  {Object.entries(result.contacts).map(([type, values]) => (
                    <div key={type} className="flex items-start gap-2 text-sm">
                      <span className="text-zinc-500 w-20 shrink-0">{type}:</span>
                      <span className="text-zinc-300">{values.join(', ')}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Findings */}
            {result.findings && result.findings.length > 0 && (
              <div className="card">
                <h3 className="text-sm font-medium text-zinc-300 flex items-center gap-1.5 mb-3">
                  <Globe className="w-4 h-4" /> Находки ({result.findings.length})
                </h3>
                <div className="space-y-2">
                  {result.findings.map((f, i) => (
                    <div key={i} className="bg-surface-700/50 rounded-lg p-3 text-sm">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-medium text-zinc-200">{f.title}</span>
                        <span className="text-xs text-zinc-500">{f.source} · {f.kind}</span>
                      </div>
                      {f.snippet && <p className="text-xs text-zinc-400">{f.snippet}</p>}
                      {f.url && (
                        <a href={f.url} target="_blank" rel="noreferrer" className="text-xs text-brand-400 hover:underline">
                          {f.url}
                        </a>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Probiv findings */}
            {result.probiv_findings && result.probiv_findings.length > 0 && (
              <div className="card">
                <h3 className="text-sm font-medium text-zinc-300 flex items-center gap-1.5 mb-3">
                  <ShieldAlert className="w-4 h-4 text-orange-400" />
                  Пробив / утечки ({result.probiv_findings.length})
                </h3>
                <div className="space-y-2">
                  {result.probiv_findings.map((f, i) => (
                    <div key={i} className="bg-surface-700/50 rounded-lg p-3 text-sm">
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-medium text-orange-300">{f.title}</span>
                        <span className="text-xs text-zinc-500">{f.source}</span>
                      </div>
                      {f.snippet && <p className="text-xs text-zinc-400">{f.snippet}</p>}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
