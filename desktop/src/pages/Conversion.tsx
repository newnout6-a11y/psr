import { useState } from 'react'
import { RefreshCw, TrendingUp } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import {
  api,
  ConversionAggregate,
  ConversionByNicheRow,
  ConversionByProviderRow,
  ConversionByPromptVariantRow,
  ConversionByQueuePositionRow,
  ConversionByResponseTimeRow,
  ConversionClassificationRow,
} from '../lib/api'
import { fmtNum } from '../lib/utils'

// ──────────────────────────────────────────────────────────────────────────────
// Phase 3 — Conversion / Feedback loop tab.
//
// Делает один запрос к агрегатору `/api/dashboard/conversion`, потому что
// все 7 таблиц всегда показываются вместе и нет смысла дробить на 7
// fetch'ей. Если backend ответит 0/[]/пустые поля (например, БД ещё не
// мигрирована или нет отправок за период) — таб корректно покажет
// «Нет данных» вместо ошибки.

const CLASSIFICATION_LABEL: Record<string, string> = {
  interested: 'заинтересован',
  negotiating: 'торгуется',
  rejected: 'отказ',
  asks_price: 'спрашивает цену',
  asks_portfolio: 'просит портфолио',
  unclassified: 'без классификации',
}

function MetricCard({
  label,
  value,
  sub,
}: {
  label: string
  value: number | string
  sub?: string
}) {
  const display = typeof value === 'number' ? fmtNum(value) : String(value)
  return (
    <div className="card scanline flex flex-col gap-1 min-w-0">
      <span className="mono-label truncate">{label}</span>
      <span className="text-2xl font-semibold text-white">{display}</span>
      {sub && <span className="text-xs text-zinc-500">{sub}</span>}
    </div>
  )
}

function fmtPct(value: number | undefined | null): string {
  if (value === undefined || value === null) return '—'
  return `${value.toFixed(1)}%`
}

function fmtMoney(value: number | undefined | null): string {
  if (!value) return '0 ₽'
  return `${fmtNum(Math.round(value))} ₽`
}

function ProviderTable({ rows }: { rows: ConversionByProviderRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">Провайдер</th>
          <th className="text-right pb-2">Отправлено</th>
          <th className="text-right pb-2">Ответы</th>
          <th className="text-right pb-2">Reply %</th>
          <th className="text-right pb-2">Выиграно</th>
          <th className="text-right pb-2">Win %</th>
          <th className="text-right pb-2">Доход</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.provider}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium">{row.provider}</td>
            <td className="text-right">{row.sent}</td>
            <td className="text-right text-emerald-400">{row.replied}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.reply_rate)}</td>
            <td className="text-right text-emerald-400">{row.won}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.win_rate)}</td>
            <td className="text-right text-zinc-300">{fmtMoney(row.revenue)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function NicheTable({ rows }: { rows: ConversionByNicheRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">Ниша (search query)</th>
          <th className="text-right pb-2">Отправлено</th>
          <th className="text-right pb-2">Ответы</th>
          <th className="text-right pb-2">Reply %</th>
          <th className="text-right pb-2">Выиграно</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.niche}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium truncate max-w-[280px]">{row.niche}</td>
            <td className="text-right">{row.sent}</td>
            <td className="text-right text-emerald-400">{row.replied}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.reply_rate)}</td>
            <td className="text-right text-emerald-400">{row.won}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function QueueTable({ rows }: { rows: ConversionByQueuePositionRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">Позиция в очереди откликов</th>
          <th className="text-right pb-2">Отправлено</th>
          <th className="text-right pb-2">Ответы</th>
          <th className="text-right pb-2">Reply %</th>
          <th className="text-right pb-2">Выиграно</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.queue_bucket}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium">{row.queue_bucket}</td>
            <td className="text-right">{row.sent}</td>
            <td className="text-right text-emerald-400">{row.replied}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.reply_rate)}</td>
            <td className="text-right text-emerald-400">{row.won}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ResponseTimeTable({ rows }: { rows: ConversionByResponseTimeRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">Время ответа клиента</th>
          <th className="text-right pb-2">Реплаев</th>
          <th className="text-right pb-2">Выиграно</th>
          <th className="text-right pb-2">Win %</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.response_bucket}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium">{row.response_bucket}</td>
            <td className="text-right text-emerald-400">{row.replies}</td>
            <td className="text-right">{row.won}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.win_rate)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ClassificationTable({ rows }: { rows: ConversionClassificationRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">Класс реплая</th>
          <th className="text-right pb-2">Реплаев</th>
          <th className="text-right pb-2">Выиграно</th>
          <th className="text-right pb-2">Win %</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.classification}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium">
              {CLASSIFICATION_LABEL[row.classification] ?? row.classification}
            </td>
            <td className="text-right text-emerald-400">{row.replies}</td>
            <td className="text-right">{row.won}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.win_rate)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PromptVariantTable({ rows }: { rows: ConversionByPromptVariantRow[] }) {
  if (!rows.length) {
    return <p className="text-zinc-500 text-sm">Нет данных</p>
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-zinc-500 border-b border-surface-700">
          <th className="text-left pb-2">A/B вариант</th>
          <th className="text-right pb-2">Отправлено</th>
          <th className="text-right pb-2">Ответы</th>
          <th className="text-right pb-2">Reply %</th>
          <th className="text-right pb-2">Выиграно</th>
          <th className="text-right pb-2">Win %</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={row.prompt_variant}
            className="border-b border-surface-700/50 text-zinc-300"
          >
            <td className="py-1.5 font-medium">{row.prompt_variant}</td>
            <td className="text-right">{row.sent}</td>
            <td className="text-right text-emerald-400">{row.replied}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.reply_rate)}</td>
            <td className="text-right text-emerald-400">{row.won}</td>
            <td className="text-right text-zinc-400">{fmtPct(row.win_rate)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function Conversion() {
  const [days, setDays] = useState(30)

  const { data, loading, error, refetch } = useApi<ConversionAggregate>(
    () => api.getConversion(days),
    [days],
  )

  const summary = data?.summary

  return (
    <div className="space-y-6 p-6 max-lg:p-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="page-kicker">фидбек-петля</div>
          <h1 className="text-xl font-semibold text-white">Конверсия</h1>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="input w-28 py-1.5"
          >
            {[7, 14, 30, 60, 90, 180].map((d) => (
              <option key={d} value={d}>
                {d} дней
              </option>
            ))}
          </select>
          <button onClick={refetch} className="btn btn-ghost py-1.5">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {error && (
        <div className="card border border-red-500/40 text-red-300 text-sm">
          Не удалось загрузить статистику конверсии: {error}
          <div className="mt-2 text-xs text-zinc-500">
            Если бэкенд только что обновился — миграция БД могла ещё не пройти.
            Перезапусти Python-бэкенд (Layout → Stop → Start) и попробуй ещё раз.
          </div>
        </div>
      )}

      {/* KPI cards */}
      <div className="grid grid-cols-6 gap-3 max-2xl:grid-cols-3 max-lg:grid-cols-2">
        <MetricCard label="Отправлено" value={summary?.sent ?? 0} />
        <MetricCard
          label="Реплаев"
          value={summary?.replied ?? 0}
          sub={summary ? `${summary.reply_rate.toFixed(1)}% reply rate` : undefined}
        />
        <MetricCard
          label="Выиграно"
          value={summary?.won ?? 0}
          sub={summary ? `${summary.win_rate.toFixed(1)}% win rate` : undefined}
        />
        <MetricCard label="Доход" value={fmtMoney(summary?.revenue)} />
        <MetricCard
          label="Период"
          value={`${data?.days ?? days} дн.`}
        />
        <MetricCard
          label="Статус"
          value={loading ? 'грузим…' : 'готово'}
          sub={loading ? undefined : 'обновлено'}
        />
      </div>

      {/* Provider + Niche */}
      <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> По LLM-провайдеру
          </h2>
          <ProviderTable rows={data?.by_provider ?? []} />
        </div>

        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> По нише
          </h2>
          <NicheTable rows={data?.by_niche ?? []} />
        </div>
      </div>

      {/* Queue + Response time */}
      <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> По очереди откликов
          </h2>
          <QueueTable rows={data?.by_queue_position ?? []} />
        </div>

        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> По времени отклика
          </h2>
          <ResponseTimeTable rows={data?.by_response_time ?? []} />
        </div>
      </div>

      {/* Classification + A/B */}
      <div className="grid grid-cols-2 gap-4 max-xl:grid-cols-1">
        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> Классификация реплаев
          </h2>
          <ClassificationTable rows={data?.classification_breakdown ?? []} />
        </div>

        <div className="card">
          <h2 className="text-sm font-medium text-zinc-300 mb-4 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-brand-400" /> A/B prompt variant
          </h2>
          <PromptVariantTable rows={data?.by_prompt_variant ?? []} />
        </div>
      </div>
    </div>
  )
}
