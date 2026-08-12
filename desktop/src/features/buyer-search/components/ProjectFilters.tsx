import { RotateCcw, Search } from 'lucide-react'

export interface BuyerProjectFilterState {
  text: string
  minBudget: string
  maxBudget: string
  maxOffers: string
  minScore: string
  minBuyerHiredPercent: string
  maxAgeHours: string
  categoryId: string
  hasAttachments: boolean
  unseen: boolean
  proposalState: string
  conversationState: string
}

export const DEFAULT_PROJECT_FILTERS: BuyerProjectFilterState = {
  text: '', minBudget: '', maxBudget: '', maxOffers: '', minScore: '',
  minBuyerHiredPercent: '', maxAgeHours: '', categoryId: '', hasAttachments: false,
  unseen: false, proposalState: '', conversationState: '',
}

interface Props {
  value: BuyerProjectFilterState
  onChange(value: BuyerProjectFilterState): void
}

export function ProjectFilters({ value, onChange }: Props) {
  const patch = (next: Partial<BuyerProjectFilterState>) => onChange({ ...value, ...next })
  return (
    <aside className="buyer-filters">
      <div className="buyer-filter-heading"><strong>Фильтры</strong><button type="button" title="Сбросить фильтры" onClick={() => onChange(DEFAULT_PROJECT_FILTERS)}><RotateCcw /></button></div>
      <label className="buyer-search-input"><Search /><input value={value.text} onChange={(event) => patch({ text: event.target.value })} placeholder="Название или описание" /></label>
      <div className="buyer-filter-group">
        <span>Бюджет, ₽</span>
        <div className="buyer-filter-pair"><input value={value.minBudget} onChange={(event) => patch({ minBudget: event.target.value })} placeholder="от" /><input value={value.maxBudget} onChange={(event) => patch({ maxBudget: event.target.value })} placeholder="до" /></div>
      </div>
      <label className="buyer-filter-field"><span>Откликов не больше</span><input value={value.maxOffers} onChange={(event) => patch({ maxOffers: event.target.value })} placeholder="Любое" /></label>
      <label className="buyer-filter-field"><span>Оценка от</span><input value={value.minScore} onChange={(event) => patch({ minScore: event.target.value })} placeholder="0" /></label>
      <label className="buyer-filter-field"><span>Найм заказчика от, %</span><input value={value.minBuyerHiredPercent} onChange={(event) => patch({ minBuyerHiredPercent: event.target.value })} placeholder="0" /></label>
      <label className="buyer-filter-field"><span>Опубликован не более, ч</span><input value={value.maxAgeHours} onChange={(event) => patch({ maxAgeHours: event.target.value })} placeholder="Любая свежесть" /></label>
      <label className="buyer-filter-field"><span>ID рубрики</span><input value={value.categoryId} onChange={(event) => patch({ categoryId: event.target.value })} placeholder="Все рубрики" /></label>
      <label className="buyer-filter-field"><span>Отклик</span><select value={value.proposalState} onChange={(event) => patch({ proposalState: event.target.value })}><option value="">Любой</option><option value="draft">Черновик</option><option value="queued">Подготовлен</option><option value="sent">Отправлен</option></select></label>
      <label className="buyer-filter-field"><span>Диалог</span><select value={value.conversationState} onChange={(event) => patch({ conversationState: event.target.value })}><option value="">Любой</option><option value="active">Активный</option><option value="unread">Непрочитанный</option><option value="closed">Закрытый</option></select></label>
      <label className="buyer-filter-check"><input type="checkbox" checked={value.hasAttachments} onChange={(event) => patch({ hasAttachments: event.target.checked })} /><span>Есть вложения</span></label>
      <label className="buyer-filter-check"><input type="checkbox" checked={value.unseen} onChange={(event) => patch({ unseen: event.target.checked })} /><span>Только непросмотренные</span></label>
    </aside>
  )
}
