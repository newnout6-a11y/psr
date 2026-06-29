import { useState } from 'react'
import { Wallet, TrendingUp, Clock, CheckCircle2 } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, EarningsSummary, EarningRow } from '../lib/api'
import { fmtTs } from '../lib/utils'

export default function Earnings() {
  const { data: summary, loading: sumLoading, refetch } = useApi(() => (api as any).getEarnings(), [])
  const { data: list, loading: listLoading } = useApi(() => (api as any).getEarningsList(50), [])

  const s = summary as EarningsSummary | null
  const rows = (list as EarningRow[]) || []

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-white flex items-center gap-2">
        <Wallet className="w-5 h-5" /> Доход
      </h1>

      <div className="grid grid-cols-3 gap-3">
        <div className="card p-4">
          <div className="text-xs text-zinc-500 mb-1">Всего</div>
          <div className="text-2xl font-bold text-white">{s?.total_amount?.toLocaleString() ?? '—'} ₽</div>
          <div className="text-xs text-zinc-500 mt-1">{s?.total ?? 0} записей</div>
        </div>
        <div className="card p-4">
          <div className="text-xs text-zinc-500 mb-1 flex items-center gap-1"><CheckCircle2 className="w-3 h-3" /> Выплачено</div>
          <div className="text-2xl font-bold text-emerald-400">{s?.paid_amount?.toLocaleString() ?? '—'} ₽</div>
          <div className="text-xs text-zinc-500 mt-1">{s?.paid_count ?? 0} выплат</div>
        </div>
        <div className="card p-4">
          <div className="text-xs text-zinc-500 mb-1 flex items-center gap-1"><Clock className="w-3 h-3" /> Ожидает</div>
          <div className="text-2xl font-bold text-yellow-400">{s?.pending_amount?.toLocaleString() ?? '—'} ₽</div>
          <div className="text-xs text-zinc-500 mt-1">{s?.pending_count ?? 0} в ожидании</div>
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="px-4 py-2 border-b border-surface-700/50 flex items-center justify-between">
          <span className="text-sm font-medium text-white">История выплат</span>
          <button onClick={refetch} className="btn btn-ghost text-xs">Обновить</button>
        </div>
        {listLoading ? (
          <div className="p-4 text-zinc-500 text-sm">Загрузка...</div>
        ) : rows.length === 0 ? (
          <div className="p-4 text-zinc-500 text-sm">Нет записей</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-zinc-500 border-b border-surface-700/50">
                <th className="px-3 py-2 text-left">ID</th>
                <th className="px-3 py-2 text-left">Проект</th>
                <th className="px-3 py-2 text-left">Платформа</th>
                <th className="px-3 py-2 text-right">Сумма</th>
                <th className="px-3 py-2 text-center">Статус</th>
                <th className="px-3 py-2 text-right">Дата</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.earning_id} className="border-b border-surface-700/30">
                  <td className="px-3 py-2 text-zinc-500">#{r.earning_id}</td>
                  <td className="px-3 py-2 text-white truncate max-w-xs">{r.project_id}</td>
                  <td className="px-3 py-2 text-zinc-400">{r.platform}</td>
                  <td className="px-3 py-2 text-right text-white font-medium">{r.amount?.toLocaleString()} {r.currency}</td>
                  <td className="px-3 py-2 text-center">
                    <span className={r.status === 'paid' ? 'text-emerald-400' : 'text-yellow-400'}>
                      {r.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-xs text-zinc-500">{fmtTs(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
