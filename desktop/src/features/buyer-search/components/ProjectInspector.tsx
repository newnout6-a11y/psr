import { ExternalLink, FileText, Heart, Paperclip, Sparkles, X } from 'lucide-react'

import { BuyerConversationWorkspace } from '../BuyerConversationWorkspace'
import { BuyerOutreachWorkspace } from '../BuyerOutreachWorkspace'
import { buyerSearchApi } from '../api'
import type { BuyerSearchProjectDetail } from '../types'

interface Props {
  runId: string
  project: BuyerSearchProjectDetail | null
  loading: boolean
  tab: 'details' | 'proposal' | 'conversation'
  accountId?: string
  onTab(tab: Props['tab']): void
  onClose(): void
  onFavorite(): void
}

export function ProjectInspector({ runId, project, loading, tab, accountId = '', onTab, onClose, onFavorite }: Props) {
  return (
    <aside className="buyer-inspector">
      <header><div><span>Карточка проекта</span><strong>{project?.title || 'Проект не выбран'}</strong></div><button type="button" title="Закрыть инспектор" onClick={onClose}><X /></button></header>
      {!project ? <div className="buyer-inspector-empty"><FileText /><span>Выберите проект в ленте</span></div> : loading ? <div className="buyer-inspector-empty">Загрузка…</div> : (
        <>
          <nav className="buyer-inspector-tabs">
            <button type="button" className={tab === 'details' ? 'is-active' : ''} onClick={() => onTab('details')}>Подробности</button>
            <button type="button" className={tab === 'proposal' ? 'is-active' : ''} onClick={() => onTab('proposal')}>Подготовить отклик</button>
            <button type="button" className={tab === 'conversation' ? 'is-active' : ''} onClick={() => onTab('conversation')}>Диалог</button>
          </nav>
          <div className="buyer-inspector-scroll">
            {tab === 'details' && (
              <div className="buyer-inspector-details">
                <div className="buyer-inspector-actions"><button type="button" onClick={onFavorite}><Heart />{project.shortlist_state === 'shortlisted' ? 'В избранном' : 'В избранное'}</button>{project.canonical_url && <a href={project.canonical_url} target="_blank" rel="noreferrer">Kwork <ExternalLink /></a>}</div>
                <section><h4>Описание</h4><p>{project.description || project.description_excerpt || 'Описание недоступно.'}</p></section>
                <section><h4>Заказчик</h4><dl><div><dt>Пользователь</dt><dd>{project.buyer_username || '—'}</dd></div><div><dt>Нанимает</dt><dd>{project.buyer_hired_percent == null ? '—' : `${project.buyer_hired_percent}%`}</dd></div><div><dt>Опубликовано</dt><dd>{project.buyer_projects_count ?? '—'}</dd></div><div><dt>Открыто</dt><dd>{project.buyer_active_projects_count ?? '—'}</dd></div></dl></section>
                {project.scores?.length ? <section><h4><Sparkles /> Оценка</h4>{project.scores.map((score, index) => <div className="buyer-score" key={score.score_id || index}><strong>{score.total_score == null ? '—' : `${Math.round(score.total_score)}/100`}</strong><p>{score.rationale || 'Пояснение не сохранено.'}</p></div>)}</section> : null}
                {project.matches?.length ? <section><h4>Совпавшие запросы</h4><div className="buyer-match-list">{project.matches.map((match, index) => <span key={match.query_id || index}>{match.query_text || match.normalized_query_text}</span>)}</div></section> : null}
                {project.attachments?.length ? <section><h4><Paperclip /> Вложения</h4>{project.attachments.map((attachment) => <a className="buyer-attachment" key={attachment.attachment_id} href={attachment.object_ref ? buyerSearchApi.attachmentPreviewUrl(runId, project.project_id, attachment.attachment_id) : undefined} target="_blank" rel="noreferrer"><FileText /><span>{attachment.filename || 'Файл'}<small>{attachment.detected_type || attachment.state || 'не обработан'}</small></span></a>)}</section> : null}
                {project.shortlist?.notes?.length ? <section><h4>Заметки</h4>{project.shortlist.notes.map((note, index) => <p className="buyer-note" key={index}>{note.body}</p>)}</section> : null}
              </div>
            )}
            {tab === 'proposal' && <BuyerOutreachWorkspace runId={runId} projectId={project.project_id} senderAccountRegistrationId={accountId} className="buyer-embedded-workspace" />}
            {tab === 'conversation' && <BuyerConversationWorkspace accountRegistrationId={accountId || null} projectId={project.project_id} className="buyer-embedded-workspace" />}
          </div>
        </>
      )}
    </aside>
  )
}
