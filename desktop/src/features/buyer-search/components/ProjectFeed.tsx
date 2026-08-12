import { Briefcase, Clock3, Eye, FileText, Heart, MessageCircle, Paperclip, Send, UserRound } from 'lucide-react'

import type { BuyerSearchProject } from '../types'

function money(value: number | null | undefined) {
  return value == null ? 'Бюджет не указан' : `${new Intl.NumberFormat('ru-RU').format(value)} ₽`
}

function age(seconds: number | null | undefined) {
  if (seconds == null) return 'время не указано'
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} мин назад`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч назад`
  return `${Math.round(seconds / 86400)} дн назад`
}

interface Props {
  items: BuyerSearchProject[]
  mode?: 'projects' | 'shortlist' | 'outreach'
  selectedId: string | null
  checkedIds: string[]
  loading: boolean
  onOpen(project: BuyerSearchProject): void
  onToggle(projectId: string): void
  onFavorite(project: BuyerSearchProject): void
}

function proposalLabel(state: string | null) {
  return ({ draft: 'Черновик', queued: 'Подготовлен', sent: 'Отправлен', sending: 'Отправляется', accepted: 'Принят', failed: 'Ошибка' } as Record<string, string>)[state || ''] || 'Отклик не подготовлен'
}

export function ProjectFeed({ items, mode = 'projects', selectedId, checkedIds, loading, onOpen, onToggle, onFavorite }: Props) {
  if (loading) return <div className="buyer-feed-state">Загружаем проекты…</div>
  if (!items.length) return <div className="buyer-feed-state"><Briefcase /><strong>{mode === 'outreach' ? 'Отклики пока не подготовлены' : mode === 'shortlist' ? 'В избранном пока пусто' : 'Проекты не найдены'}</strong><span>{mode === 'outreach' ? 'Откройте проект и перейдите в инспекторе к подготовке отклика.' : mode === 'shortlist' ? 'Отмечайте проекты сердцем, чтобы вернуться к ним позже.' : 'Измените фильтры или запустите новый поиск.'}</span></div>
  const checked = new Set(checkedIds)
  return (
    <div className="buyer-project-feed">
      {items.map((project) => {
        const score = project.final_score ?? project.preliminary_score
        return (
          <article className={`buyer-project-row ${selectedId === project.project_id ? 'is-selected' : ''}`} key={project.project_id} onClick={() => onOpen(project)}>
            <div className="buyer-project-select" onClick={(event) => event.stopPropagation()}>{mode !== 'outreach' && <input type="checkbox" aria-label={`Выбрать ${project.title}`} checked={checked.has(project.project_id)} onChange={() => onToggle(project.project_id)} />}</div>
            <div className="buyer-project-main">
              <div className="buyer-project-title"><div><h3>{project.title}</h3>{project.unseen && <span>Новое</span>}</div><button type="button" className={project.shortlist_state === 'shortlisted' ? 'is-active' : ''} title="В избранное" onClick={(event) => { event.stopPropagation(); onFavorite(project) }}><Heart /></button></div>
              <p>{project.description_excerpt || 'Описание проекта пока недоступно.'}</p>
              <div className="buyer-project-meta">
                {project.category_id && <span><Briefcase />Рубрика {project.category_id}</span>}
                <span><Clock3 />{age(project.age_seconds)}</span>
                {(project.attachment_count || 0) > 0 && <span><Paperclip />{project.attachment_count}</span>}
                {project.views != null && <span><Eye />{project.views}</span>}
                {project.matched_query_count > 0 && <span><FileText />{project.matched_query_count} совп.</span>}
              </div>
              <div className="buyer-client-line"><UserRound /><strong>{project.buyer_username || 'Заказчик Kwork'}</strong><span>найм {project.buyer_hired_percent == null ? '—' : `${project.buyer_hired_percent}%`}</span>{project.buyer_projects_count != null && <span>{project.buyer_projects_count} проектов</span>}{project.buyer_active_projects_count != null && <span>{project.buyer_active_projects_count} открыто</span>}</div>
              {mode === 'outreach' && <div className="buyer-outreach-status"><span className={`is-${project.proposal_state || 'none'}`}>{proposalLabel(project.proposal_state)}</span>{project.conversation_state && <span>Диалог: {project.conversation_state}</span>}</div>}
            </div>
            <div className="buyer-project-aside">
              <strong>{money(project.budget_max ?? project.budget_min)}</strong>
              <span><MessageCircle />{project.offers ?? '—'} откликов</span>
              {score != null && <b>{Math.round(score)}<small>/100</small></b>}
              {mode === 'outreach' && <button type="button" className="buyer-outreach-open" onClick={(event) => { event.stopPropagation(); onOpen(project) }}><Send />Открыть отклик</button>}
            </div>
          </article>
        )
      })}
    </div>
  )
}
