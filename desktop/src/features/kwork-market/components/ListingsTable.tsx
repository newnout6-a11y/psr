import type { MarketListing } from '../types'
import { formatCount, formatTimestamp } from './shared'

export function ListingsTable({ listings, loading, onLoadMore, hasMore }: { listings: MarketListing[]; loading?: boolean; onLoadMore(): void; hasMore: boolean }) {
  return (
    <section className="factory-panel overflow-hidden">
      <div className="table-head flex items-center justify-between px-4 py-3"><h2 className="text-sm font-medium text-white">Карточки</h2><span className="mono-label">показано {listings.length}</span></div>
      <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-left text-xs"><thead className="text-zinc-500"><tr><th className="px-4 py-2 font-medium">Карточка</th><th className="px-3 py-2 font-medium">Продавец</th><th className="px-3 py-2 text-right font-medium">Цена</th><th className="px-3 py-2 text-right font-medium">Наблюдений</th><th className="px-3 py-2 font-medium">Последнее наблюдение</th></tr></thead><tbody>{listings.map((listing) => <tr key={listing.listing_id} className="border-t border-surface-700/60 hover:bg-surface-800/45"><td className="max-w-96 truncate px-4 py-2 text-zinc-200"><span className="mr-2 font-mono text-zinc-600">#{listing.listing_key}</span>{listing.title || 'Название не получено'}</td><td className="px-3 py-2 text-zinc-400">{listing.seller_key || '—'}</td><td className="px-3 py-2 text-right text-zinc-300">{listing.price === null || listing.price === undefined ? '—' : formatCount(listing.price)}</td><td className="px-3 py-2 text-right text-zinc-400">{listing.observation_count}</td><td className="px-3 py-2 text-zinc-500">{formatTimestamp(listing.last_seen_at)}</td></tr>)}{!listings.length && <tr><td colSpan={5} className="px-4 py-8 text-center text-zinc-500">Принятых карточек пока нет.</td></tr>}</tbody></table></div>
      {hasMore && <div className="border-t border-surface-700/60 px-4 py-2 text-right"><button type="button" onClick={onLoadMore} disabled={loading} className="btn btn-ghost text-xs">{loading ? 'Загрузка…' : 'Показать ещё'}</button></div>}
    </section>
  )
}
