export const KWORK_VERIFICATION_URL = 'https://kwork.ru/'

export type KworkVerificationOpenResult = {
  ok?: boolean
  imported?: number
  reused?: boolean
}

type ElectronApi = {
  openKworkVerification?: (url?: string) => Promise<KworkVerificationOpenResult>
}

const ROBOT_CHECK_RE =
  /(manual_verification_required|smartcaptcha|smart-captcha|smartcaptcha\.cloud\.yandex\.ru|captcha-api\.yandex|smart-token|не робот|я не робот|подтвердите, что вы не робот|большой нагрузк|автоматическ(?:ие|ими)? скрипт|robot check|bot check|manual robot check)/i

const NEGATIVE_ROBOT_CHECK_RE =
  /(captcha_required\s*[:=]\s*false|getCaptchaStatus\s*=\s*true|ignored|ignore|игнор|не требует|not required)/i

export function isKworkManualVerificationSignal(message: unknown): boolean {
  const text = String(message ?? '')
  if (!text) return false
  if (NEGATIVE_ROBOT_CHECK_RE.test(text)) return false
  if (ROBOT_CHECK_RE.test(text)) return true
  return /kwork/i.test(text) && /(robot check|bot check|manual verification|подтвердите.*не робот)/i.test(text)
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
