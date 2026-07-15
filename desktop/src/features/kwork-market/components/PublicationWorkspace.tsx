import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Check, ExternalLink, FileCheck2, Image as ImageIcon, Loader2, RefreshCw, Send, ShieldCheck, Sparkles, Wand2 } from 'lucide-react'

import psrLogoUrl from '../../../assets/psr-icon.png'
import { API_BASE, api, type KworkFormControl, type KworkFormManifest, type KworkPublishPreflightResult } from '../../../lib/api'
import { asRecord, asStrings, cleanClusterLabel, marketResultModel, resultNumber, resultValue, type JsonRecord } from '../resultModel'
import type { MarketDraftHandoff, MarketJob, MarketPublishedListing, MarketRecommendation, MarketResults } from '../types'
import { marketJobsApi } from '../api'
import { formatCount, formatTimestamp } from './shared'

interface PublicationWorkspaceProps {
  job: MarketJob
  results: MarketResults | null
  opportunity: JsonRecord | null
}

function controlLabel(control: KworkFormControl): string {
  return control.question || control.label || control.custom_name || control.name
}

function selectedIds(selection: Record<string, unknown>, control: KworkFormControl): number[] {
  const raw = selection[control.name]
  const values = Array.isArray(raw) ? raw : raw === undefined || raw === null || raw === '' ? [] : [raw]
  return values.map(Number).filter(Number.isFinite)
}

function updateControlSelection(
  selection: Record<string, unknown>,
  control: KworkFormControl,
  optionId: number,
  checked: boolean,
): Record<string, unknown> {
  if (!control.multiple) return { ...selection, [control.name]: checked ? optionId : '' }
  const current = new Set(selectedIds(selection, control))
  if (checked) current.add(optionId)
  else current.delete(optionId)
  return { ...selection, [control.name]: [...current] }
}

function handoffStep(handoff: MarketDraftHandoff | null, published: boolean): number {
  if (published) return 4
  if (!handoff) return 0
  if (handoff.state === 'draft_generated') return 3
  if (handoff.state === 'fields_confirmed') return 2
  if (handoff.attribute_manifest_hash) return 1
  return 0
}

function suggestedCoverTitle(value: string): string {
  const text = value.trim()
  const lower = text.toLocaleLowerCase('ru-RU')
  if (lower.includes('логотип') && (lower.includes('кирил') || lower.includes('русск'))) return 'Логотип на русском'
  if (lower.includes('шрифт') && (lower.includes('кирил') || lower.includes('русск'))) return 'Шрифт под кириллицу'
  return text
    .replace(/^(сделаю|разработаю|создам|настрою|адаптирую)\s+/i, '')
    .replace(/\s+(для|под)\s+.{24,}$/i, '')
    .slice(0, 38)
    .trim()
}

function suggestedCoverSubtitle(value: string): string {
  const lower = value.toLocaleLowerCase('ru-RU')
  if (lower.includes('логотип')) return 'Адаптация знака и шрифта'
  if (lower.includes('шрифт')) return 'Шрифт для кириллицы'
  return 'Покажу результат до и после'
}

function resolveAssetUrl(value: unknown): string {
  const url = String(value || '').trim()
  if (!url) return ''
  const localPath = url.replace(/^file:\/\//i, '')
  if (/^[a-z]:[\\/]/i.test(localPath)) {
    const filename = localPath.split(/[\\/]/).filter(Boolean).pop()
    return filename ? `${API_BASE}/api/kwork/autopublish/assets/${encodeURIComponent(filename)}` : ''
  }
  if (url.startsWith('/api/')) return `${API_BASE}${url}`
  if (url.startsWith('api/')) return `${API_BASE}/${url}`
  try {
    return new URL(url, `${API_BASE}/`).toString()
  } catch {
    return url
  }
}

function publishedListingUrl(listing: MarketPublishedListing): string {
  const result = asRecord(listing.publish_result)
  const verify = asRecord(result?.verify_result)
  const save = asRecord(result?.save_result)
  for (const candidate of [verify?.final_url, verify?.url, save?.final_url, save?.url, result?.final_url, result?.url]) {
    const url = String(candidate || '').trim()
    if (url.startsWith('http://') || url.startsWith('https://')) return url
  }
  return ''
}

function publishedListingTitle(listing: MarketPublishedListing): string {
  const payload = asRecord(asRecord(listing.publish_result)?.payload)
  return String(payload?.title || '').trim()
}

export function PublicationWorkspace({ job, results, opportunity }: PublicationWorkspaceProps) {
  const model = useMemo(() => marketResultModel(results), [results])
  const [recommendations, setRecommendations] = useState<MarketRecommendation[]>([])
  const [publishedListings, setPublishedListings] = useState<MarketPublishedListing[]>([])
  const [handoff, setHandoff] = useState<MarketDraftHandoff | null>(null)
  const [selection, setSelection] = useState<Record<string, unknown>>({})
  const [serviceSummary, setServiceSummary] = useState('')
  const [audience, setAudience] = useState('')
  const [price, setPrice] = useState(0)
  const [workTime, setWorkTime] = useState(3)
  const [generateImage, setGenerateImage] = useState(true)
  const [useCompetitorImageAnalysis, setUseCompetitorImageAnalysis] = useState(true)
  const [variantCount, setVariantCount] = useState(1)
  const [selectedVariantIndex, setSelectedVariantIndex] = useState(0)
  const [coverTitle, setCoverTitle] = useState('')
  const [coverSubtitle, setCoverSubtitle] = useState('')
  const [imageResult, setImageResult] = useState<Record<string, any> | null>(null)
  const [publishResult, setPublishResult] = useState<Record<string, any> | null>(null)
  const [preflight, setPreflight] = useState<KworkPublishPreflightResult | null>(null)
  const [confirmationInput, setConfirmationInput] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const autoOpenedRecommendation = useRef<string | null>(null)

  const sourceClusterId = opportunity ? resultValue(opportunity, 'cluster_id', '') : ''
  const sourceCluster = useMemo(
    () => model.clusters.find((cluster) => resultValue(cluster, 'cluster_id', '') === sourceClusterId),
    [model.clusters, sourceClusterId],
  )
  const sourceClusterPrice = useMemo(() => asRecord(asRecord(sourceCluster?.metrics)?.price), [sourceCluster])
  const evidenceIds = useMemo(() => opportunity ? asStrings(opportunity.evidence_ids) : [], [opportunity])
  const manifest = (handoff?.attribute_manifest || null) as KworkFormManifest | null
  const controls = Array.isArray(manifest?.controls) ? manifest.controls : []
  const validation = asRecord(handoff?.validation)
  const unresolved = Array.isArray(validation?.unresolved_required)
    ? validation.unresolved_required.map(String)
    : manifest?.unresolved_required || []
  const publishedForHandoff = Boolean(handoff && publishedListings.some((item) => item.handoff_id === handoff.handoff_id))
  const handoffRecommendation = handoff
    ? recommendations.find((item) => item.recommendation_id === handoff.recommendation_id)
    : undefined
  const effectiveSourceClusterId = handoffRecommendation?.source_cluster_id || sourceClusterId
  const effectiveEvidenceIds = handoffRecommendation?.evidence_ids?.length ? handoffRecommendation.evidence_ids : evidenceIds
  const step = handoffStep(handoff, publishedForHandoff)
  const draftVariants = useMemo(() => {
    const raw = handoff?.draft?.variants
    if (Array.isArray(raw)) {
      const variants = raw.map(asRecord).filter((item): item is JsonRecord => Boolean(item))
      if (variants.length) return variants
    }
    return handoff?.draft ? [handoff.draft] : []
  }, [handoff?.draft])
  const activeDraft = draftVariants[selectedVariantIndex] || handoff?.draft || {}
  const durableImage = asRecord(activeDraft.cover_image)
  const displayedImage = imageResult || durableImage || null
  const coverAssetUrl = resolveAssetUrl(displayedImage?.asset_url || activeDraft.cover_image_path)
  const portfolioAssets = Array.isArray(activeDraft.portfolio_assets)
    ? activeDraft.portfolio_assets.map(asRecord).filter((item): item is JsonRecord => Boolean(item))
    : []

  const loadDurableState = useCallback(async () => {
    const [recommendationResponse, publishedResponse] = await Promise.all([
      marketJobsApi.listRecommendations(job.job_id),
      marketJobsApi.listPublishedListings(job.job_id),
    ])
    setRecommendations(recommendationResponse.items)
    setPublishedListings(publishedResponse.items)
  }, [job.job_id])

  useEffect(() => {
    void loadDurableState().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
  }, [loadDurableState])

  useEffect(() => {
    if (!opportunity) return
    const label = cleanClusterLabel(opportunity.label)
    const suggestedPrice = resultNumber(sourceClusterPrice, 'median', resultNumber(model.price, 'p50', 500))
    setServiceSummary(label)
    setAudience(`Клиенты, которым нужна услуга: ${label.toLocaleLowerCase('ru-RU')}`)
    setPrice(Math.max(1, Math.round(suggestedPrice || 500)))
    setCoverTitle(suggestedCoverTitle(label))
    setCoverSubtitle(suggestedCoverSubtitle(label))
    setHandoff(null)
    setSelection({})
    setImageResult(null)
    setSelectedVariantIndex(0)
    setPublishResult(null)
    setPreflight(null)
    setConfirmationInput('')
    setError(null)
    autoOpenedRecommendation.current = null
  }, [model.price, opportunity, sourceClusterPrice])

  const matchingRecommendation = useMemo(
    () => recommendations.find((item) => item.source_cluster_id === sourceClusterId && item.state !== 'rejected'),
    [recommendations, sourceClusterId],
  )

  useEffect(() => {
    if (!matchingRecommendation || handoff || busy || autoOpenedRecommendation.current === matchingRecommendation.recommendation_id) return
    autoOpenedRecommendation.current = matchingRecommendation.recommendation_id
    void openRecommendation(matchingRecommendation)
  }, [busy, handoff, matchingRecommendation])

  useEffect(() => {
    if (!handoff) return
    setSelection(handoff.attribute_selection || {})
    const options = asRecord(handoff.draft?.generation_options)
    if (options?.variant_count) setVariantCount(Math.max(1, Math.min(3, Number(options.variant_count) || 1)))
    if (typeof options?.use_competitor_image_analysis === 'boolean') {
      setUseCompetitorImageAnalysis(options.use_competitor_image_analysis)
    }
  }, [handoff])

  useEffect(() => {
    setSelectedVariantIndex((current) => Math.min(current, Math.max(0, draftVariants.length - 1)))
  }, [draftVariants.length])

  useEffect(() => {
    if (!Object.keys(activeDraft).length) return
    if (activeDraft.cover_text) setCoverTitle(String(activeDraft.cover_text))
    else if (activeDraft.title) setCoverTitle(suggestedCoverTitle(String(activeDraft.title)))
    if (activeDraft.cover_subtitle) setCoverSubtitle(String(activeDraft.cover_subtitle))
    setImageResult(asRecord(activeDraft.cover_image) || null)
    setPreflight(null)
    setConfirmationInput('')
  }, [activeDraft, selectedVariantIndex])

  async function run<T>(key: string, action: () => Promise<T>): Promise<T | null> {
    setBusy(key)
    setError(null)
    try {
      return await action()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      return null
    } finally {
      setBusy(null)
    }
  }

  async function startHandoff() {
    if (!opportunity || !serviceSummary.trim()) return
    if (!evidenceIds.length) {
      setError('У этой возможности нет durable evidence IDs, поэтому связанный контур публикации создать нельзя.')
      return
    }
    const created = await run('start', () => marketJobsApi.createRecommendation(job.job_id, {
      category_id: job.scope.category_id,
      classifier_id: job.scope.classifier_id ?? null,
      service_summary: serviceSummary.trim(),
      price: Math.max(1, Math.round(price)),
      work_time: Math.max(1, Math.round(workTime)),
      source_cluster_id: sourceClusterId || null,
      terra_result: {
        verdict: 'recommend',
        confidence: Math.max(0, Math.min(1, Number(opportunity.confidence ?? 0) / 100)),
        reasons: [resultValue(opportunity, 'why')],
        evidence_ids: evidenceIds,
        recommended_offer: {
          service_summary: serviceSummary.trim(),
          price: Math.max(1, Math.round(price)),
          work_time: Math.max(1, Math.round(workTime)),
        },
        rejected_cases: [],
      },
    }))
    if (!created) return
    const confirmed = await run('start', () => marketJobsApi.confirmRecommendation(job.job_id, created.recommendation.recommendation_id, undefined, true))
    if (!confirmed?.handoff) return
    setHandoff(confirmed.handoff)
    await loadDurableState()
  }

  async function openRecommendation(recommendation: MarketRecommendation) {
    setServiceSummary(recommendation.service_summary)
    setAudience(`Клиенты, которым нужна услуга: ${recommendation.service_summary.toLocaleLowerCase('ru-RU')}`)
    setPrice(recommendation.price)
    setWorkTime(recommendation.work_time)
    setCoverTitle(suggestedCoverTitle(recommendation.service_summary))
    setCoverSubtitle(suggestedCoverSubtitle(recommendation.service_summary))
    const confirmed = await run('open', () => marketJobsApi.confirmRecommendation(job.job_id, recommendation.recommendation_id, undefined, true))
    if (!confirmed?.handoff) return
    setHandoff(confirmed.handoff)
  }

  async function saveSelection(confirm: boolean) {
    if (!handoff?.attribute_manifest_hash) return
    const response = await run(confirm ? 'confirm-fields' : 'save-fields', () =>
      marketJobsApi.updateDraftHandoffSelection(
        job.job_id,
        handoff.handoff_id,
        handoff.attribute_manifest_hash!,
        selection,
        confirm,
      ),
    )
    if (response?.handoff) setHandoff(response.handoff)
  }

  async function saveAndRefreshManifest() {
    if (!handoff?.attribute_manifest_hash) return
    const saved = await run('manifest', () =>
      marketJobsApi.updateDraftHandoffSelection(job.job_id, handoff.handoff_id, handoff.attribute_manifest_hash!, selection, false),
    )
    if (!saved?.handoff) return
    const refreshed = await run('manifest', () => marketJobsApi.refreshDraftHandoffManifest(job.job_id, handoff.handoff_id))
    if (refreshed?.handoff) setHandoff(refreshed.handoff)
  }

  async function autoFillFields() {
    if (!handoff?.attribute_manifest_hash || !controls.length) return
    setBusy('auto-fields')
    setError(null)
    try {
      let currentHandoff = handoff
      let currentSelection = { ...selection }
      for (let round = 0; round < 2; round += 1) {
        const currentManifest = currentHandoff.attribute_manifest as KworkFormManifest
        const currentControls = Array.isArray(currentManifest?.controls) ? currentManifest.controls : []
        let dynamicSelectionChanged = false
        for (const control of currentControls) {
          if (control.disabled || !control.options?.length || selectedIds(currentSelection, control).length) continue
          const suggestion = await api.suggestKworkAttribute(job.scope.category_id, {
            classifier_id: job.scope.classifier_id ?? undefined,
            category_name: job.scope.category_name,
            classifier_name: job.scope.classifier_name,
            service_summary: serviceSummary,
            audience,
            control,
            controls: currentControls.slice(0, 12),
            selection: currentSelection,
            market_context: {
              source_job_id: job.job_id,
              source_cluster_id: effectiveSourceClusterId,
              evidence_ids: effectiveEvidenceIds,
            },
            mode: job.scope.classifier_name || job.scope.category_name || '',
            use_llm: false,
            lang: 'ru',
          })
          const nextSelection = suggestion.selection || currentSelection
          const nextIds = selectedIds(nextSelection, control)
          if (nextIds.some((id) => control.options.some((option) => Number(option.id) === id && option.has_child))) {
            dynamicSelectionChanged = true
          }
          currentSelection = nextSelection
        }

        const saved = await marketJobsApi.updateDraftHandoffSelection(
          job.job_id,
          currentHandoff.handoff_id,
          currentHandoff.attribute_manifest_hash!,
          currentSelection,
          false,
        )
        currentHandoff = saved.handoff
        if (!dynamicSelectionChanged) break
        const refreshed = await marketJobsApi.refreshDraftHandoffManifest(job.job_id, currentHandoff.handoff_id)
        currentHandoff = refreshed.handoff
        currentSelection = { ...currentHandoff.attribute_selection }
      }
      setSelection(currentSelection)
      setHandoff(currentHandoff)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(null)
    }
  }

  async function generateDraft() {
    if (!handoff) return
    const response = await run('draft', () => marketJobsApi.generateDraftHandoff(job.job_id, handoff.handoff_id, {
      audience,
      use_llm: true,
      generate_image: generateImage,
      cover_text: coverTitle || suggestedCoverTitle(serviceSummary),
      cover_subtitle: coverSubtitle,
      cover_text_overlay: true,
      use_competitor_image_analysis: useCompetitorImageAnalysis,
      variant_count: variantCount,
      cover_prompt_provider: 'openai',
      cover_vision_provider: 'openai',
      market_context: {
        source_job_id: job.job_id,
        source_cluster_id: effectiveSourceClusterId,
        evidence_ids: effectiveEvidenceIds,
      },
    }))
    if (!response) return
    setSelectedVariantIndex(0)
    setHandoff(response.handoff)
    setImageResult(response.image ?? null)
    setPublishResult(null)
    setPreflight(null)
  }

  async function dryRunPublish() {
    if (!handoff) return
    const response = await run('dry-run', () => marketJobsApi.publishDraftHandoff(job.job_id, handoff.handoff_id, {
      dry_run: true,
      variant_index: selectedVariantIndex,
    }))
    if (response) setPublishResult(response.publish)
  }

  async function prepareLivePublish() {
    if (!handoff?.draft || !Object.keys(activeDraft).length) return
    const response = await run('preflight', () => api.preflightKworkPublish(activeDraft))
    if (!response) return
    setPreflight(response)
    setPublishResult(response)
    setConfirmationInput('')
    if (!response.ok) setError(response.preflight?.detail || 'Предпроверка публикации не прошла.')
  }

  async function confirmLivePublish() {
    if (!handoff || !preflight?.token) return
    const phrase = preflight.confirmation_phrase || 'ОПУБЛИКОВАТЬ'
    if (confirmationInput.trim() !== phrase) {
      setError('Фраза подтверждения не совпадает.')
      return
    }
    const response = await run('publish', () => marketJobsApi.publishDraftHandoff(job.job_id, handoff.handoff_id, {
      dry_run: false,
      confirm_token: preflight.token,
      confirmation: phrase,
      variant_index: selectedVariantIndex,
    }))
    if (!response) return
    setPublishResult(response.publish)
    setPreflight(null)
    setConfirmationInput('')
    await loadDurableState()
    if (!response.publish?.ok) setError(String(response.publish?.detail || 'Kwork не подтвердил публикацию.'))
  }

  return (
    <section className="market-surface market-publication-workspace" id="market-publication-workspace">
      <div className="market-surface-head flex-wrap">
        <div className="flex items-center gap-3">
          <img src={psrLogoUrl} alt="PSR" className="market-publication-logo" />
          <div><div className="market-section-label">Связь с публикацией</div><h3 className="mt-1 text-base font-semibold text-white">Из рыночной ниши в карточку Kwork</h3></div>
        </div>
        <div className="market-publication-steps">
          {['Рекомендация', 'Поля Kwork', 'Черновик', 'Публикация'].map((label, index) => (
            <span key={label} className={step > index ? 'is-complete' : step === index ? 'is-current' : ''}><i>{step > index ? <Check className="h-3 w-3" /> : index + 1}</i>{label}</span>
          ))}
        </div>
      </div>

      <div className="market-publication-body">
        {!opportunity && !handoff && <div className="market-empty-state">Выберите возможность выше, чтобы связать её с будущей карточкой.</div>}

        {opportunity && !handoff && (
          <div className="market-publication-proposal">
            <div className="min-w-0">
              <div className="market-section-label">Источник · {sourceClusterId || 'AI opportunity'}</div>
              <h4>{cleanClusterLabel(opportunity.label)}</h4>
              <p>{resultValue(opportunity, 'why')}</p>
              <div className="mt-3 font-mono text-[10px] text-zinc-600">{evidenceIds.join(' · ')}</div>
            </div>
            <div className="market-publication-form">
              <label><span>Услуга</span><input value={serviceSummary} onChange={(event) => setServiceSummary(event.target.value)} /></label>
              <label><span>Аудитория</span><input value={audience} onChange={(event) => setAudience(event.target.value)} /></label>
              <div className="grid grid-cols-2 gap-2"><label><span>Цена, ₽</span><input type="number" min="1" value={price} onChange={(event) => setPrice(Number(event.target.value) || 1)} /></label><label><span>Срок, дней</span><input type="number" min="1" value={workTime} onChange={(event) => setWorkTime(Number(event.target.value) || 1)} /></label></div>
              <button type="button" className="market-primary-action h-9" disabled={busy !== null || !evidenceIds.length} onClick={() => void startHandoff()}>{busy === 'start' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}Создать связанный контур</button>
            </div>
          </div>
        )}

        {handoff && (
          <div className="market-publication-flow">
            <div className="market-publication-context">
              <div><span>Рекомендация</span><strong>{handoff.service_summary}</strong></div>
              <div><span>Источник</span><strong className="font-mono">{effectiveSourceClusterId || '-'}</strong></div>
              <div><span>Доказательства</span><strong>{formatCount(effectiveEvidenceIds.length)} карточки</strong></div>
              <div><span>Цена / срок</span><strong>{formatCount(handoff.price)} ₽ · {handoff.work_time} дн.</strong></div>
              <div><span>Состояние</span><strong>{handoff.state}</strong></div>
            </div>

            {handoff.state === 'mapping' && (
              <div className="market-publication-section">
                <div className="market-publication-section-head">
                  <div><div className="market-section-label">Шаг 2</div><h4>Обязательные поля Kwork</h4></div>
                  <div className="flex flex-wrap gap-2">
                    <button type="button" className="market-compact-action" disabled={busy !== null || !controls.length} onClick={() => void autoFillFields()}>{busy === 'auto-fields' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}Подобрать поля</button>
                    <button type="button" className="market-compact-action" disabled={busy !== null} onClick={() => void saveAndRefreshManifest()}>{busy === 'manifest' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}Обновить форму</button>
                  </div>
                </div>
                <div className="market-manifest-grid">
                  {controls.map((control) => (
                    <div key={control.name} className="market-manifest-control">
                      <div className="flex items-center justify-between gap-2"><strong>{controlLabel(control)}{control.required ? ' *' : ''}</strong><span>{control.multiple ? 'несколько' : control.type}</span></div>
                      {control.options?.length ? (
                        <div className="market-manifest-options">
                          {control.options.map((option) => {
                            const checked = selectedIds(selection, control).includes(Number(option.id))
                            return <label key={`${control.name}-${option.id}`} className={checked ? 'is-selected' : ''}><input type={control.multiple ? 'checkbox' : 'radio'} name={control.name} checked={checked} disabled={control.disabled || option.disabled} onChange={(event) => setSelection((current) => updateControlSelection(current, control, Number(option.id), event.target.checked))} /><span>{option.label || `#${option.id}`}</span>{option.has_child && <em>ещё поля</em>}</label>
                          })}
                        </div>
                      ) : (
                        <input className="market-manifest-input" value={String(selection[control.name] ?? control.value ?? '')} disabled={control.disabled} placeholder={control.placeholder || control.name} onChange={(event) => setSelection((current) => ({ ...current, [control.name]: event.target.value }))} />
                      )}
                    </div>
                  ))}
                  {!controls.length && <div className="market-empty-state">Для этой категории дополнительных полей нет.</div>}
                </div>
                {!!unresolved.length && <div className="market-notice">Не заполнены обязательные поля: {unresolved.join(', ')}</div>}
                <div className="market-publication-actions"><button type="button" className="market-primary-action h-9" disabled={busy !== null} onClick={() => void saveSelection(true)}>{busy === 'confirm-fields' ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}Проверить и подтвердить поля</button></div>
              </div>
            )}

            {handoff.state === 'fields_confirmed' && (
              <div className="market-publication-section">
                <div className="market-publication-section-head">
                  <div><div className="market-section-label">Шаг 3</div><h4>Генерация карточек и обложек</h4></div>
                  <div className="market-generation-options">
                    <div className="market-variant-picker" aria-label="Количество карточек">
                      {[1, 2, 3].map((count) => (
                        <button key={count} type="button" className={variantCount === count ? 'is-active' : ''} onClick={() => setVariantCount(count)}>{count}</button>
                      ))}
                    </div>
                    <label className={`market-inline-toggle ${useCompetitorImageAnalysis ? 'is-on' : ''}`}><input type="checkbox" checked={useCompetitorImageAnalysis} onChange={(event) => setUseCompetitorImageAnalysis(event.target.checked)} />Анализ конкурентов</label>
                    <label className={`market-inline-toggle ${generateImage ? 'is-on' : ''}`}><input type="checkbox" checked={generateImage} onChange={(event) => setGenerateImage(event.target.checked)} />Обложка</label>
                  </div>
                </div>
                {generateImage && (
                  <div className="market-cover-controls">
                    <label><span>Заголовок на обложке</span><input value={coverTitle} maxLength={38} onChange={(event) => setCoverTitle(event.target.value)} /></label>
                    <label><span>Короткий подзаголовок</span><input value={coverSubtitle} maxLength={46} onChange={(event) => setCoverSubtitle(event.target.value)} /></label>
                  </div>
                )}
                <div className="market-publication-actions"><button type="button" className="market-primary-action h-9" disabled={busy !== null} onClick={() => void generateDraft()}>{busy === 'draft' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}Сгенерировать {variantCount} {variantCount === 1 ? 'карточку' : 'карточки'}</button></div>
              </div>
            )}

            {handoff.state === 'draft_generated' && (
              <div className="market-publication-section">
                <div className="market-publication-section-head"><div><div className="market-section-label">Шаг 3–4</div><h4>Предпросмотр и публикация</h4></div><span className="market-tone-label market-tone-positive">draft сохранён</span></div>
                {draftVariants.length > 1 && (
                  <div className="market-draft-variants">
                    {draftVariants.map((variant, index) => (
                      <button key={`${String(variant.variant_index ?? index)}-${String(variant.title || index)}`} type="button" className={selectedVariantIndex === index ? 'is-active' : ''} onClick={() => setSelectedVariantIndex(index)}>
                        <span>Карточка {index + 1}</span>
                        <strong>{String(variant.title || handoff.service_summary)}</strong>
                      </button>
                    ))}
                  </div>
                )}
                <div className="market-draft-preview">
                  <div className="market-draft-cover">
                    {coverAssetUrl ? <img src={coverAssetUrl} alt="Обложка карточки" /> : <div><ImageIcon className="h-7 w-7" /><span>{generateImage ? 'Обложка сохранена в durable draft' : 'Без обложки'}</span></div>}
                  </div>
                  <div className="min-w-0"><span className="market-section-label">Название</span><h5>{String(activeDraft.title || handoff.service_summary)}</h5><span className="market-section-label mt-4">Описание</span><p>{String(activeDraft.description || '')}</p></div>
                </div>
                {!!portfolioAssets.length && (
                  <div className="market-portfolio-preview">
                    <div className="market-portfolio-preview-head"><span>Портфолио для Kwork</span><strong>{portfolioAssets.length} работ готовы к загрузке</strong></div>
                    <div className="market-portfolio-preview-grid">
                      {portfolioAssets.map((asset, index) => {
                        const url = resolveAssetUrl(asset.asset_url || asset.path)
                        return <figure key={`${String(asset.path || asset.asset_url)}-${index}`}>{url ? <img src={url} alt={String(asset.title || `Работа ${index + 1}`)} /> : <ImageIcon className="h-6 w-6" />}<figcaption><b>{String(asset.title || `Работа ${index + 1}`)}</b><span>{String(asset.subtitle || '')}</span></figcaption></figure>
                      })}
                    </div>
                  </div>
                )}
                <div className="market-cover-regenerate">
                  <div className="market-generation-options">
                    <div className="market-variant-picker" aria-label="Количество карточек при перегенерации">
                      {[1, 2, 3].map((count) => (
                        <button key={count} type="button" className={variantCount === count ? 'is-active' : ''} onClick={() => setVariantCount(count)}>{count}</button>
                      ))}
                    </div>
                    <label className={`market-inline-toggle ${useCompetitorImageAnalysis ? 'is-on' : ''}`}><input type="checkbox" checked={useCompetitorImageAnalysis} onChange={(event) => setUseCompetitorImageAnalysis(event.target.checked)} />Анализ конкурентов</label>
                  </div>
                  <div className="market-cover-controls">
                    <label><span>Заголовок на обложке</span><input value={coverTitle} maxLength={38} onChange={(event) => setCoverTitle(event.target.value)} /></label>
                    <label><span>Короткий подзаголовок</span><input value={coverSubtitle} maxLength={46} onChange={(event) => setCoverSubtitle(event.target.value)} /></label>
                  </div>
                  <button type="button" className="market-compact-action" disabled={busy !== null} onClick={() => void generateDraft()}>{busy === 'draft' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}Перегенерировать обложку и текст</button>
                </div>
                <div className="market-publication-actions"><button type="button" className="market-compact-action" disabled={busy !== null} onClick={() => void dryRunPublish()}>{busy === 'dry-run' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileCheck2 className="h-3.5 w-3.5" />}Dry-run payload</button><button type="button" className="market-primary-action h-9" disabled={busy !== null} onClick={() => void prepareLivePublish()}>{busy === 'preflight' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}Подготовить live publish</button></div>
              </div>
            )}

            {preflight?.ok && preflight.token && (
              <div className="market-publish-confirmation"><strong>Подтверждение live-публикации</strong><p>Введите фразу <b>{preflight.confirmation_phrase || 'ОПУБЛИКОВАТЬ'}</b>. После этого карточка будет отправлена в Kwork.</p><div><input value={confirmationInput} onChange={(event) => setConfirmationInput(event.target.value)} placeholder={preflight.confirmation_phrase || 'ОПУБЛИКОВАТЬ'} /><button type="button" className="market-primary-action h-9" disabled={busy !== null} onClick={() => void confirmLivePublish()}>{busy === 'publish' && <Loader2 className="h-4 w-4 animate-spin" />}Опубликовать</button></div></div>
            )}

            {publishResult && <details className="market-publication-result"><summary>{publishResult.ok ? 'Проверка публикации пройдена' : 'Результат публикации'}</summary><pre>{JSON.stringify(publishResult, null, 2)}</pre></details>}
          </div>
        )}

        {busy && <div className="market-publication-progress"><Loader2 className="h-3.5 w-3.5 animate-spin" /><span>{busy === 'draft' ? 'Генерирую текст и обложку. Поля уже сохранены, страницу можно не трогать.' : busy === 'auto-fields' ? 'Подбираю значения по услуге и выбранной рубрике.' : 'Синхронизирую состояние с Kwork.'}</span></div>}
        {error && <div className="market-error-banner">{error}</div>}

        {!!recommendations.length && !handoff && (
          <div className="market-publication-history"><div className="market-section-label">Сохранённые рекомендации</div>{recommendations.map((recommendation) => <button key={recommendation.recommendation_id} type="button" disabled={busy !== null || recommendation.state === 'rejected'} onClick={() => void openRecommendation(recommendation)}><span><strong>{recommendation.service_summary}</strong><small>{formatCount(recommendation.price)} ₽ · {recommendation.state} · {formatTimestamp(recommendation.updated_at)}</small></span><span className="font-mono">{recommendation.source_cluster_id || '-'}</span></button>)}</div>
        )}

        {!!publishedListings.length && (
          <div className="market-published-listings">
            <div className="market-section-label">Опубликованные карточки из этого анализа</div>
            {publishedListings.map((listing) => {
              const url = publishedListingUrl(listing)
              const title = publishedListingTitle(listing)
              return (
                <div key={listing.published_listing_id}>
                  <Check className="h-4 w-4 text-emerald-300" />
                  <span><strong>{title || `Kwork ${listing.kwork_id || 'ID проверяется'}`}</strong><small>Kwork {listing.kwork_id || '-'} · кластер {listing.source_cluster_id || '-'} · {formatTimestamp(listing.created_at)}</small></span>
                  {url && <a href={url} target="_blank" rel="noreferrer" title="Открыть опубликованную карточку"><ExternalLink className="h-4 w-4" /></a>}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </section>
  )
}
