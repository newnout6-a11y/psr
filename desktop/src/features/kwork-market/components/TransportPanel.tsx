import { Globe2, Network } from 'lucide-react'

import type { MarketTransport } from '../types'
import { formatTransportProxyRoute, transportHealthLabel } from './shared'

export function TransportPanel({ transports }: { transports: MarketTransport[] }) {
  const active = transports.filter((transport) => transport.lease_owner).length
  const healthy = transports.filter((transport) => transport.health === 'healthy').length
  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head">
        <div><div className="market-section-label">Сеть</div><h2 className="mt-1 text-base font-semibold text-white">Маршруты</h2></div>
        <span className="font-mono text-xs text-zinc-500">{healthy} / {transports.length} healthy</span>
      </div>
      <div className="market-transport-summary">
        <div><Network className="h-4 w-4" /><span>Занято</span><strong>{active}</strong></div>
        <div><Globe2 className="h-4 w-4" /><span>Свободно</span><strong>{Math.max(0, transports.length - active)}</strong></div>
      </div>
      <div className="max-h-[34rem] overflow-y-auto">
        {transports.map((transport) => (
          <div key={transport.transport_id} className="market-transport-row">
            <span className={`market-health-dot is-${transport.health}`} />
            <div className="min-w-0">
              <div className="flex items-center justify-between gap-3"><strong className="truncate font-mono">{transport.slot ? `slot ${transport.slot}` : transport.transport_id}</strong><span>{transportHealthLabel(transport.health)}</span></div>
              <div className="mt-1 truncate font-mono text-[11px] text-zinc-500">{formatTransportProxyRoute(transport.proxy_url)}</div>
              <div className="mt-1 truncate text-[11px] text-zinc-600">{[transport.profile_name, transport.country].filter(Boolean).join(' · ') || 'Профиль не указан'} · {transport.lease_owner ? `занят ${transport.lease_owner}` : 'свободен'}</div>
            </div>
          </div>
        ))}
        {!transports.length && <div className="market-empty-state">Для прямого подключения управляемые маршруты не создаются.</div>}
      </div>
    </section>
  )
}
