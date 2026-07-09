export const KWORK_VERIFICATION_URL = 'https://kwork.ru/'

export type KworkVerificationOpenResult = {
  ok?: boolean
  imported?: number
  reused?: boolean
}

type ElectronApi = {
  openKworkVerification?: (url?: string) => Promise<KworkVerificationOpenResult>
}

const STRONG_ROBOT_CHECK_RE =
  /(manual_verification_required|not_access\.php|captcha_required\s*[:=]\s*true|manual_verification_required\s*[:=]\s*true|Kwork requires a manual|requires a manual SmartCaptcha|ручн\w*\s+провер|подтвердите,\s*что\s*вы\s*не\s*робот|я\s*не\s*робот|robot check|bot check|manual robot check)/i

const NEGATIVE_ROBOT_CHECK_RE =
  /(manual_verification_required\s*[:=]\s*false|captcha_required\s*[:=]\s*false|getCaptchaStatus\s*=\s*true|api_flag_only|ignored|ignore|игнор|не требует|not required|no manual captcha|did not show a manual challenge|without web challenge evidence|вероятно нет заказов)/i

const NORMAL_KWORK_NEW_PAGE_RE =
  /Kwork\s+manual_verification_required:\s*status=200\s+url=https?:\/\/(?:www\.)?kwork\.ru\/new\b/i

export function isKworkManualVerificationSignal(message: unknown): boolean {
  const text = String(message ?? '')
  if (!text) return false
  if (NORMAL_KWORK_NEW_PAGE_RE.test(text)) return false
  if (NEGATIVE_ROBOT_CHECK_RE.test(text)) return false
  if (STRONG_ROBOT_CHECK_RE.test(text)) return true
  return /kwork/i.test(text) && /(manual verification required|manual captcha challenge|подтвердите.*не робот)/i.test(text)
}

export async function openKworkVerificationWindow(url = KWORK_VERIFICATION_URL) {
  const electronApi = (window as Window & { electronAPI?: ElectronApi }).electronAPI
  if (electronApi?.openKworkVerification) {
    return electronApi.openKworkVerification(url)
  }
  window.open(url, '_blank', 'noopener,noreferrer')
  return { ok: true, imported: 0, reused: false }
}

export function notifyKworkVerificationNeeded() {
  if (!('Notification' in window)) return
  const title = 'Kwork просит ручную проверку'
  const body = 'Открой проверку в PSR и пройди капчу вручную: галочка, слайдер или картинка.'

  if (Notification.permission === 'granted') {
    new Notification(title, { body })
    return
  }
  if (Notification.permission === 'default') {
    Notification.requestPermission().then((permission) => {
      if (permission === 'granted') {
        new Notification(title, { body })
      }
    }).catch(() => {})
  }
}
