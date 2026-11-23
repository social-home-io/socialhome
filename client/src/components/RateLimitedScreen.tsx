import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { Wordmark } from '@/components/Wordmark'

/**
 * RateLimitedScreen — the cold start was answered 429 (``/api/me`` or the
 * instance config, :meth:`ApiClient.getPatiently`). Not "signed out", so
 * never the login form and never ``IngressAuthFailed``: it says what
 * happened and counts down to the automatic retry, which honours the
 * server's ``Retry-After``. No button — the retry already runs, and a
 * "try now" would only spend another request inside the same window.
 *
 * Only the title sits in the live region: a countdown announced every
 * second would drown a screen reader.
 */
export function RateLimitedScreen({ until }: { until: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(id)
  }, [until])
  const seconds = Math.max(0, Math.ceil((until - now) / 1_000))
  return (
    <div class="sh-login" role="main">
      <div class="sh-login-hero">
        <Wordmark size={48} tagline={t('login.tagline')} />
      </div>
      <div aria-live="polite">
        <h1 style={{ textAlign: 'center' }}>{t('boot_rate_limited.title')}</h1>
      </div>
      <p class="sh-muted" style={{ maxWidth: '34em', margin: '0 auto var(--sh-space-md)' }}>
        {t('boot_rate_limited.body')}
      </p>
      <p style={{ textAlign: 'center' }}>
        {seconds > 0
          ? t('boot_rate_limited.retry_in', { n: String(seconds) })
          : t('boot_rate_limited.retrying')}
      </p>
    </div>
  )
}
