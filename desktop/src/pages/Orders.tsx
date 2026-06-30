import { useState } from 'react'
import { Package, Loader2, RefreshCw } from 'lucide-react'
import { useApi } from '../hooks/useApi'
import { api, KworkOrder } from '../lib/api'
import { cn } from '../lib/utils'

const STATUS_TABS = ['all', 'active', 'done', 'cancelled'] as const
type StatusTab = (typeof STATUS_TABS)[number]

const STATUS_LABELS: Record<string, string> = {
  all: 'Все',
  active: 'Активные',
  done: 'Завершённые',
  cancelled: 'Отменённые',
}

const STATUS_BADGE: Record<string, string> = {
  new: 'border-blue-500/30 bg-blue-500/10 text-blue-300',
  active: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  done: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  completed: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  finished: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  cancelled: 'border-red-500/30 bg-red-500/10 text-red-300',
  canceled: 'border-red-500/30 bg-red-500/10 text-red-300',
  expired: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
  failed: 'border-red-500/30 bg-red-500/10 text-red-300',
}

function statusBadgeClass(status: string): string {
  return STATUS_BADGE[status?.toLowerCase()] || 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300'
}

export default function Orders() {
  const [tab, setTab] = useState<StatusTab>('all')
  const { data, loading, refetch } = useApi(() => (api as any).getKworkOrders(tab), [tab])

  const orders: KworkOrder[] = data?.orders || []

  return (
    <div className="space-y-4 p-6 max-lg:p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="page-kicker">kwork orders</div>
          <h1 className="text-xl font-semibold text-white">Заказы</h1>
        </div>
        <button onClick={refetch} className="btn btn-ghost py-1.5">
          <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
        </button>
      </div>

      {/* Status tabs */}
      <div className="flex gap-1.5">
        {STATUS_TABS.map((s) => (
          <button
            key={s}
            onClick={() => setTab(s)}
            className={cn(
              'rounded-md border px-3 py-1 text-xs transition-colors',
              tab === s ? 'border-brand-500/40 bg-brand-600/15 text-white' : 'border-surface-600 text-zinc-400 hover:text-white',
            )}
          >
            {STATUS_LABELS[s]}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-6 w-6 animate-spin text-zinc-600" />
        </div>
      ) : orders.length === 0 ? (
        <div className="card flex flex-col items-center justify-center py-16 text-center">
          <Package className="h-10 w-10 text-zinc-700" />
          <p className="mt-3 text-sm text-zinc-500">Нет заказов</p>
          <p className="text-xs text-zinc-600">У вас ещё ничего не заказали</p>
        </div>
      ) : (
        <div className="card overflow-visible">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-700 text-zinc-500">
                <th className="text-left py-2 px-2">Заказ</th>
                <th className="text-left py-2 px-2 w-20">Статус</th>
                <th className="text-right py-2 px-2 w-20">Цена</th>
                <th className="text-left py-2 px-2 w-24">Клиент</th>
                <th className="text-right py-2 px-2 w-28">Создан</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order, i) => (
                <tr key={order.id || i} className="border-b border-surface-700/40 hover:bg-surface-800/50">
                  <td className="py-2 px-2">
                    <div className="text-zinc-200 truncate max-w-md">{order.name || `#${order.id}`}</div>
                  </td>
                  <td className="py-2 px-2">
                    <span className={cn('badge text-[10px]', statusBadgeClass(order.status || ''))}>
                      {order.status || '—'}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-right text-zinc-400">
                    {order.price ? `${order.price}₽` : '—'}
                  </td>
                  <td className="py-2 px-2 text-zinc-500 truncate max-w-24">{order.user_username || '—'}</td>
                  <td className="py-2 px-2 text-right text-zinc-600">
                    {order.date_create?.slice(5, 16) || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
