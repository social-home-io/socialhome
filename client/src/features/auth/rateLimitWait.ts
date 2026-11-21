import { t } from '@/i18n/i18n'

/**
 * The "too many tries, wait N minutes" line for a 429 from the login or
 * password-reset throttle (only wrong passwords / links count there).
 *
 * ``retryAfterS`` is the wait in seconds — the body's ``retry_after`` or
 * the ``Retry-After`` header — rounded up to whole minutes. When it is
 * missing or odd, ``fallback`` (the page's plain "wait a few minutes").
 */
export function rateLimitWaitMessage(retryAfterS: unknown, fallback: string): string {
  const s = typeof retryAfterS === 'string' && /^\d+$/.test(retryAfterS.trim())
    ? Number(retryAfterS)
    : retryAfterS
  if (typeof s !== 'number' || !Number.isFinite(s) || s <= 0) return fallback
  const n = Math.ceil(s / 60)
  return n <= 1
    ? t('auth.rate_limited_wait_one')
    : t('auth.rate_limited_wait', { n: String(n) })
}
