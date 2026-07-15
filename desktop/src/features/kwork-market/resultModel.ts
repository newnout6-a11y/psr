import type { MarketResults } from './types'

export type JsonRecord = Record<string, unknown>

export function asRecord(value: unknown): JsonRecord | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : undefined
}

export function asRecords(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(asRecord).filter((item): item is JsonRecord => Boolean(item)) : []
}

export function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : []
}

export function resultValue(record: JsonRecord | undefined, key: string, fallback = '-'): string {
  const raw = record?.[key]
  return raw === null || raw === undefined || raw === '' ? fallback : String(raw)
}

export function resultNumber(record: JsonRecord | undefined, key: string, fallback = 0): number {
  const parsed = Number(record?.[key])
  return Number.isFinite(parsed) ? parsed : fallback
}

export function formatMoney(raw: unknown): string {
  if (raw === null || raw === undefined || raw === '') return '-'
  const number = Number(raw)
  return Number.isFinite(number) ? `${new Intl.NumberFormat('ru-RU').format(number)} ₽` : String(raw)
}

export function verdictLabel(raw: unknown): string {
  const labels: Record<string, string> = {
    promising_for_entry: 'Перспективно для входа',
    popular_crowded: 'Спрос есть, рынок плотный',
    do_not_take: 'Не брать',
    mixed: 'Смешанный результат',
  }
  return labels[String(raw)] ?? String(raw || '-')
}

export function verdictTone(raw: unknown): 'positive' | 'warning' | 'negative' | 'neutral' {
  if (raw === 'promising_for_entry') return 'positive'
  if (raw === 'popular_crowded' || raw === 'mixed') return 'warning'
  if (raw === 'do_not_take') return 'negative'
  return 'neutral'
}

export function aiStatusLabel(raw: unknown): string {
  const labels: Record<string, string> = {
    ok: 'Готов',
    disabled: 'Выключен',
    unavailable: 'Ошибка провайдера',
    not_configured: 'Провайдер не настроен',
    insufficient_data: 'Недостаточно данных',
  }
  return labels[String(raw)] ?? String(raw || 'Ожидается')
}

export function cleanClusterLabel(raw: unknown): string {
  const cleaned = String(raw || '')
    .replace(/\b(?:strong|bull|mdash|ndash|quot|amp|nbsp|laquo|raquo)\b/gi, ' ')
    .replace(/[_|]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!cleaned) return 'Группа без названия'
  return cleaned.charAt(0).toLocaleUpperCase('ru-RU') + cleaned.slice(1)
}

export function marketResultModel(results: MarketResults | null) {
  const metrics = asRecord(results?.metrics)
  const analysis = asRecord(results?.analysis)
  const execution = asRecord(results?.execution)
  const enrichment = asRecord(analysis?.enrichment_selection)
  const semantic = asRecord(analysis?.semantic_analysis)
  const aiVerdict = asRecord(analysis?.ai_verdict)
  const price = asRecord(metrics?.price_distribution)
  const sellers = asRecord(metrics?.seller_repetition_in_observed_cards)
  const shardPrice = asRecord(metrics?.price_distribution_by_shard)

  return {
    metrics,
    analysis,
    execution,
    enrichment,
    semantic,
    aiVerdict,
    price,
    sellers,
    clusters: asRecords(semantic?.clusters),
    opportunities: asRecords(aiVerdict?.opportunities),
    repeatedSellers: asRecords(sellers?.top_repeated_sellers),
    shardRows: asRecords(shardPrice?.shards),
  }
}
