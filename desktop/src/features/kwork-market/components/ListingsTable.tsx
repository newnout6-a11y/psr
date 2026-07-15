import { useMemo, useState } from 'react'
import { Search } from 'lucide-react'

import type { MarketListing } from '../types'
import { formatCount, formatTimestamp } from './shared'

export function ListingsTable({ listings, loading, onLoadMore, hasMore }: { listings: MarketListing[]; loading?: boolean; onLoadMore(): void; hasMore: boolean }) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase('ru-RU')
    if (!needle) return listings
    return listings.filter((listing) => `${listing.listing_key} ${listing.title ?? ''} ${listing.seller_key ?? ''}`.toLocaleLowerCase('ru-RU').includes(needle))
  }, [listings, query])

  return (
    <section className="market-surface overflow-hidden">
      <div className="market-surface-head flex-wrap">
        <div><div className="market-section-label">Датасет</div><h2 className="mt-1 text-base font-semibold text-white">Собранные карточки</h2></div>
        <label className="market-search-field"><Search className="h-3.5 w-3.5" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Название или продавец" /></label>
      </div>
      <div className="max-h-[38rem] overflow-auto">
        <table className="market-data-table min-w-[820px]">
          <thead><tr><th>Карточка</th><th>Продавец</th><th className="text-right">Цена</th><th className="text-right">Наблюдений</th><th>Последнее наблюдение</th></tr></thead>
          <tbody>
            {filtered.map((listing) => (
              <tr key={listing.listing_id}>
                <td className="max-w-[34rem]"><div className="truncate text-zinc-200">{listing.title || 'Название не получено'}</div><div className="market-row-note font-mono">#{listing.listing_key}</div></td>
                <td className="font-mono">{listing.seller_key || '—'}</td>
                <td className="text-right font-mono text-zinc-300">{listing.price === null || listing.price === undefined ? '—' : `${formatCount(listing.price)} ₽`}</td>
                <td className="text-right font-mono">{listing.observation_count}</td>
                <td>{formatTimestamp(listing.last_seen_at)}</td>
              </tr>
            ))}
            {!filtered.length && <tr><td colSpan={5}><div className="market-empty-state">По запросу карточки не найдены.</div></td></tr>}
          </tbody>
        </table>
      </div>
      <div className="market-table-footer"><span>Показано {filtered.length} из {listings.length}</span>{hasMore && <button type="button" onClick={onLoadMore} disabled={loading} className="market-text-action">{loading ? 'Загрузка…' : 'Загрузить ещё 100'}</button>}</div>
    </section>
  )
}
