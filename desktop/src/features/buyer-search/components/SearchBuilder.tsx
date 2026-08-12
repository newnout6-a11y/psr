import { ChevronDown, Loader2, Play, Sparkles, X } from 'lucide-react'

import { BuyerTaxonomyPlanner } from '../BuyerTaxonomyPlanner'
import type { BuyerTaxonomySelection } from '../taxonomy-types'
import type { BuyerSearchWorkspaceBuilder } from '../types'

interface Props {
  value: BuyerSearchWorkspaceBuilder
  busy: boolean
  onChange(patch: Partial<BuyerSearchWorkspaceBuilder>): void
  onCreate(): void
}

function numberValue(value: string): number | undefined {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined
}

function queryCandidates(value: BuyerSearchWorkspaceBuilder) {
  const exact = value.exact_queries.map((text) => text.trim()).filter(Boolean)
  const fromBrief = exact.length ? [] : value.brief
    .split(/[\r\n.!?;]+/)
    .map((text) => text.trim())
    .filter((text) => text.length >= 8 && text.length <= 120)
  const seen = new Set<string>()
  return [...exact, ...fromBrief].flatMap((text) => {
    const key = text.toLocaleLowerCase('ru-RU').replace(/\s+/g, ' ')
    if (seen.has(key)) return []
    seen.add(key)
    return [{ text, enabled: true, rationale: exact.includes(text) ? 'Точная фраза' : 'Из описания' }]
  })
}

export function SearchBuilder({ value, busy, onChange, onCreate }: Props) {
  const categories = value.taxonomy_selections

  const handleTaxonomy = (selections: BuyerTaxonomySelection[]) => {
    onChange({
      taxonomy_selections: selections.map((selection) => ({
        category_id: selection.category.category_id,
        name: selection.category.name,
        category_path: selection.category.category_path,
        category_scope: selection.categoryScope,
        kworks_count: selection.kworksCount ?? null,
      })),
    })
  }

  const updatePreview = () => onChange({ preview_queries: queryCandidates(value) })
  const hasSource = value.brief.trim() || value.exact_queries.some((item) => item.trim()) || categories.length

  return (
    <div className="buyer-builder">
      <header className="buyer-page-heading">
        <div>
          <span className="buyer-eyebrow">Новый поиск</span>
          <h1>Настройка поиска</h1>
          <p>Опишите подходящий проект и ограничения. Режим поиска определится автоматически.</p>
        </div>
      </header>

      <div className="buyer-builder-grid">
        <section className="buyer-form-main">
          <label className="buyer-field buyer-field-wide">
            <span>Название поиска</span>
            <input value={value.name} onChange={(event) => onChange({ name: event.target.value })} placeholder="Например, разработка сайтов для малого бизнеса" />
          </label>
          <label className="buyer-field buyer-field-wide">
            <span>Описание подходящего заказа</span>
            <textarea value={value.brief} onChange={(event) => onChange({ brief: event.target.value })} placeholder="Что нужно сделать, какие технологии и задачи подходят, что исключить..." rows={6} />
          </label>
          <label className="buyer-field buyer-field-wide">
            <span>Точные фразы</span>
            <textarea
              value={value.exact_queries.join('\n')}
              onChange={(event) => onChange({ exact_queries: event.target.value.split(/\r?\n/) })}
              placeholder={'создать интернет-магазин\nдоработать сайт на React\nнастроить Telegram-бота'}
              rows={4}
            />
          </label>

          <div className="buyer-field buyer-field-wide">
            <span>Рубрики Kwork</span>
            {categories.length > 0 && (
              <div className="buyer-category-chips">
                {categories.map((category, index) => (
                  <span key={String(category.category_id ?? index)}>
                    {String(category.name || `Рубрика ${category.category_id}`)}
                    <button type="button" title="Убрать рубрику" onClick={() => onChange({ taxonomy_selections: categories.filter((_, itemIndex) => itemIndex !== index) })}><X /></button>
                  </span>
                ))}
              </div>
            )}
            <BuyerTaxonomyPlanner onSelect={handleTaxonomy} disabled={busy} className="buyer-taxonomy" />
          </div>

          <div className="buyer-field-grid">
            <label className="buyer-field"><span>Бюджет от, ₽</span><input inputMode="numeric" value={value.min_budget} onChange={(event) => onChange({ min_budget: event.target.value })} placeholder="Любой" /></label>
            <label className="buyer-field"><span>Бюджет до, ₽</span><input inputMode="numeric" value={value.max_budget} onChange={(event) => onChange({ max_budget: event.target.value })} placeholder="Без лимита" /></label>
            <label className="buyer-field"><span>Макс. откликов</span><input inputMode="numeric" value={value.max_offers} onChange={(event) => onChange({ max_offers: event.target.value })} placeholder="Любое" /></label>
            <label className="buyer-field"><span>Найм заказчика от, %</span><input inputMode="numeric" value={value.min_buyer_hired_percent} onChange={(event) => onChange({ min_buyer_hired_percent: event.target.value })} placeholder="0" /></label>
            <label className="buyer-field"><span>Свежесть, часов</span><input inputMode="numeric" value={value.max_age_hours} onChange={(event) => onChange({ max_age_hours: event.target.value })} placeholder="24" /></label>
            <label className="buyer-field"><span>Цель по проектам</span><input type="number" min={1} max={1000000} value={value.target_projects} onChange={(event) => onChange({ target_projects: Math.max(1, Number(event.target.value) || 1) })} /></label>
          </div>

          <button type="button" className="buyer-advanced-toggle" onClick={() => onChange({ advanced_open: !value.advanced_open })}>
            <ChevronDown className={value.advanced_open ? 'is-open' : ''} /> Дополнительно
          </button>
          {value.advanced_open && (
            <div className="buyer-advanced-grid">
              <label className="buyer-field" title="Количество одновременно работающих discovery-воркеров. Это не число прокси и не скорость запросов в секунду."><span>Воркеры</span><input type="number" min={1} max={30} value={value.workers} onChange={(event) => onChange({ workers: Math.max(1, Math.min(30, Number(event.target.value) || 1)) })} /></label>
              <label className="buyer-field" title="Сколько разных поисковых запросов получает один воркер в начальной волне. Это не запросов в секунду."><span>Запросов на воркер</span><input type="number" min={1} max={10} value={value.query_batch_size} onChange={(event) => onChange({ query_batch_size: Math.max(1, Math.min(10, Number(event.target.value) || 1)) })} /></label>
              <label className="buyer-field" title="ID профиля, который будет использован при явно запрошенной финальной оценке проекта."><span>Профиль финальной оценки</span><input value={value.scoring_profile_id} onChange={(event) => onChange({ scoring_profile_id: event.target.value })} placeholder="По умолчанию" /></label>
              <label className="buyer-check" title="Разрешает отдельную account/VPNTE-bound операцию разбора вложений в инспекторе проекта; файлы не скачиваются автоматически при поиске."><input type="checkbox" checked={value.enrichment_enabled} onChange={(event) => onChange({ enrichment_enabled: event.target.checked })} /><span>Разрешить разбор вложений</span></label>
            </div>
          )}
        </section>

        <aside className="buyer-query-preview">
          <div className="buyer-preview-title"><div><span>План запросов</span><small>{value.preview_queries.filter((item) => item.enabled !== false).length} включено</small></div><button type="button" title="Собрать план запросов" onClick={updatePreview}><Sparkles /></button></div>
          {value.preview_queries.length ? (
            <div className="buyer-preview-list">
              {value.preview_queries.map((query, index) => (
                <div className={query.enabled === false ? 'is-disabled' : ''} key={`${query.text}-${index}`}>
                  <input type="checkbox" checked={query.enabled !== false} onChange={(event) => onChange({ preview_queries: value.preview_queries.map((item, itemIndex) => itemIndex === index ? { ...item, enabled: event.target.checked } : item) })} />
                  <input value={query.text} onChange={(event) => onChange({ preview_queries: value.preview_queries.map((item, itemIndex) => itemIndex === index ? { ...item, text: event.target.value } : item) })} />
                  <button type="button" title="Удалить запрос" onClick={() => onChange({ preview_queries: value.preview_queries.filter((_, itemIndex) => itemIndex !== index) })}><X /></button>
                </div>
              ))}
            </div>
          ) : (
            <div className="buyer-preview-empty"><Sparkles /><span>Соберите план, чтобы проверить и отредактировать фразы до запуска.</span></div>
          )}
          <dl className="buyer-search-summary">
            <div><dt>Бюджет</dt><dd>{numberValue(value.min_budget)?.toLocaleString('ru-RU') || '0'}–{numberValue(value.max_budget)?.toLocaleString('ru-RU') || '∞'} ₽</dd></div>
            <div><dt>Откликов не больше</dt><dd>{value.max_offers || 'без лимита'}</dd></div>
            <div><dt>Рубрики</dt><dd>{categories.length || 'все'}</dd></div>
          </dl>
          <button type="button" className="buyer-primary-action" disabled={busy || !hasSource} onClick={onCreate}>
            {busy ? (
              <><Loader2 className="spin" /> Формируем план и запускаем...</>
            ) : (
              <><Play /> Создать и запустить поиск</>
            )}
          </button>
        </aside>
      </div>
    </div>
  )
}
