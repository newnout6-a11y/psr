import { useState, useEffect, useRef } from 'react'
import { Trash2, PauseCircle, PlayCircle, Download } from 'lucide-react'
import { useWebSocket } from '../hooks/useWebSocket'
import { cn } from '../lib/utils'
import { api } from '../lib/api'

interface LogEntry {
  id: number
  seq?: number
  level: string
  message: string
  ts: string
}

const LEVEL_COLOR: Record<string, string> = {
  DEBUG:    'text-zinc-500',
  INFO:     'text-zinc-300',
  SUCCESS:  'text-emerald-400',
  WARNING:  'text-yellow-400',
  ERROR:    'text-red-400',
  CRITICAL: 'text-red-500 font-bold',
}

const LEVEL_FILTERS = ['ALL', 'INFO', 'SUCCESS', 'WARNING', 'ERROR']

let _id = 0
function nextId() { return ++_id }

function toLogEntry(msg: Record<string, unknown>): LogEntry {
  const seq = typeof msg.seq === 'number' ? msg.seq : undefined
  return {
    id: seq ?? nextId(),
    seq,
    level: String(msg.level ?? 'INFO'),
    message: String(msg.message ?? ''),
    ts: typeof msg.ts === 'string' ? msg.ts : new Date().toLocaleTimeString(),
  }
}

export default function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [paused, setPaused] = useState(false)
  const [levelFilter, setLevelFilter] = useState('ALL')
  const [search, setSearch] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const pausedRef = useRef(paused)
  pausedRef.current = paused

  useWebSocket('ws://127.0.0.1:7788/ws/logs', (msg) => {
    if (msg.type === 'logs_snapshot') {
      const items = Array.isArray(msg.items) ? msg.items : []
      setLogs(items.map((item) => toLogEntry(item as Record<string, unknown>)))
      return
    }
    if (msg.type === 'logs_cleared') {
      setLogs([])
      return
    }
    if (msg.type !== 'log') return
    if (pausedRef.current) return
    const entry = toLogEntry(msg)
    setLogs((prev) => {
      if (entry.seq !== undefined && prev.some((item) => item.seq === entry.seq)) {
        return prev
      }
      const next = [...prev, entry]
      return next.length > 2000 ? next.slice(next.length - 2000) : next
    })
  })

  useEffect(() => {
    if (!paused) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [logs, paused])

  const filtered = logs.filter((l) => {
    if (levelFilter !== 'ALL' && l.level !== levelFilter) return false
    if (search && !l.message.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  function handleDownload() {
    const text = filtered.map((l) => `[${l.ts}] ${l.level} | ${l.message}`).join('\n')
    const blob = new Blob([text], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `psr-logs-${Date.now()}.txt`
    a.click()
  }

  async function handleClear() {
    setLogs([])
    try {
      await api.clearLogs()
    } catch {}
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="toolbar flex shrink-0 items-center gap-3 overflow-x-auto px-4 py-2.5">
        <div>
          <div className="page-kicker">поток</div>
          <span className="text-sm font-medium text-white">Логи</span>
        </div>
        <span className="badge bg-zinc-500/15 text-zinc-400 border-zinc-500/30">{logs.length}</span>
        <div className="flex-1" />

        {/* Level filter */}
        <div className="flex gap-1">
          {LEVEL_FILTERS.map((lvl) => (
            <button
              key={lvl}
              onClick={() => setLevelFilter(lvl)}
              className={cn(
                'px-2 py-0.5 rounded text-xs font-medium transition-colors',
                levelFilter === lvl
                  ? 'bg-brand-600/30 text-brand-300 border border-brand-500/40'
                  : 'text-zinc-500 hover:text-zinc-300'
              )}
            >
              {lvl}
            </button>
          ))}
        </div>

        {/* Search */}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск..."
          className="input w-44 py-1 text-xs"
        />

        <button onClick={() => setPaused((p) => !p)} className="btn btn-ghost py-1">
          {paused
            ? <PlayCircle className="w-4 h-4 text-emerald-400" />
            : <PauseCircle className="w-4 h-4" />
          }
        </button>
        <button onClick={handleDownload} className="btn btn-ghost py-1" title="Скачать логи">
          <Download className="w-4 h-4" />
        </button>
        <button onClick={handleClear} className="btn btn-ghost py-1" title="Очистить">
          <Trash2 className="w-4 h-4" />
        </button>
      </div>

      {/* Log area */}
      <div className="flex-1 overflow-y-auto bg-surface-900/70 p-3 font-mono text-xs">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-full text-zinc-600">
            Ожидание логов... (запустите цикл)
          </div>
        ) : (
          filtered.map((l) => (
            <div key={l.id} className="flex gap-2 hover:bg-surface-800/50 px-1 py-0.5 rounded">
              <span className="text-zinc-600 shrink-0 w-16">{l.ts}</span>
              <span className={cn('w-16 shrink-0 font-bold', LEVEL_COLOR[l.level] ?? 'text-zinc-400')}>
                {l.level}
              </span>
              <span className={cn('flex-1 break-all', LEVEL_COLOR[l.level] ?? 'text-zinc-300')}>
                {l.message}
              </span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {paused && (
        <div className="shrink-0 px-4 py-1.5 bg-yellow-500/10 border-t border-yellow-500/20 text-xs text-yellow-300 text-center">
          Пауза — новые логи не отображаются
        </div>
      )}
    </div>
  )
}
