import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—'
  try {
    const dt = new Date(ts)
    const diff = Date.now() - dt.getTime()
    const secs = Math.floor(diff / 1000)
    if (secs < 60) return `${secs} сек назад`
    const mins = Math.floor(secs / 60)
    if (mins < 60) return `${mins} мин назад`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours} ч назад`
    const days = Math.floor(hours / 24)
    return `${days} дн назад`
  } catch {
    return ts
  }
}

export function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString('ru-RU')
}

export const STATUS_COLOR: Record<string, string> = {
  queued:      'text-yellow-400',
  auto_ready:  'text-emerald-400',
  snoozed:     'text-stone-300',
  auto_sent:   'text-emerald-500',
  manual_sent: 'text-emerald-400',
  draft:       'text-zinc-400',
  skipped:     'text-zinc-500',
  error:       'text-red-400',
  parsed:      'text-zinc-300',
  filtered:    'text-brand-300',
  scored:      'text-orange-300',
  vetted:      'text-amber-200',
}

export const STATUS_BG: Record<string, string> = {
  queued:      'bg-yellow-500/15 text-yellow-300 border-yellow-500/30',
  auto_ready:  'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  snoozed:     'bg-stone-500/15 text-stone-300 border-stone-500/30',
  auto_sent:   'bg-emerald-600/15 text-emerald-300 border-emerald-600/30',
  manual_sent: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  draft:       'bg-zinc-500/15 text-zinc-300 border-zinc-500/30',
  skipped:     'bg-zinc-600/10 text-zinc-400 border-zinc-600/20',
  error:       'bg-red-500/15 text-red-300 border-red-500/30',
  parsed:      'bg-zinc-500/10 text-zinc-300 border-zinc-500/20',
  filtered:    'bg-brand-500/15 text-brand-200 border-brand-500/30',
  scored:      'bg-orange-500/15 text-orange-200 border-orange-500/30',
  vetted:      'bg-amber-500/15 text-amber-200 border-amber-500/30',
}

export const STATUS_LABEL: Record<string, string> = {
  queued:      'в очереди',
  auto_ready:  'готово',
  snoozed:     'отложено',
  auto_sent:   'авто-отпр.',
  manual_sent: 'отправлено',
  draft:       'черновик',
  skipped:     'пропуск',
  error:       'ошибка',
  parsed:      'спарсено',
  filtered:    'фильтр',
  scored:      'AI оценка',
  vetted:      'проверено',
}

export const PLATFORM_LABEL: Record<string, string> = {
  kwork:        'Kwork',
  freelance_ru: 'Freelance.ru',
  hh_ru:        'HH.ru',
}

export const RISK_COLOR: Record<string, string> = {
  low:    'text-emerald-400',
  medium: 'text-yellow-400',
  high:   'text-red-400',
}

export const RISK_LABEL: Record<string, string> = {
  low: 'низкий',
  medium: 'средний',
  high: 'высокий',
}
