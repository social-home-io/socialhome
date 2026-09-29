/**
 * OfflineIndicator — connectivity banner (§23.21).
 *
 * Two distinct "you're cut off" states, one banner slot:
 *
 * 1. **Browser offline** (``navigator.onLine`` false) — shown at once;
 *    nothing on the network is reachable.
 * 2. **Server unreachable** — the browser is online but the realtime
 *    socket (``connectionState`` in ``ws.ts``) has been down for longer
 *    than :data:`UNREACHABLE_GRACE_MS`. The grace period swallows the
 *    routine sub-second reconnects (backend restart, proxy idle
 *    timeout) so the banner doesn't flicker. "Retry now" skips the
 *    socket's exponential backoff. When the socket reopens after the
 *    banner was visible, a brief "Reconnected" toast confirms recovery.
 *
 * Only mounted inside the authenticated shell, so it never competes
 * with the haos ``IngressAuthFailed`` screen (that one replaces the
 * whole shell when the ingress handshake fails).
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import { connectionState, ws } from '@/ws'
import { t } from '@/i18n/i18n'
import { showToast } from './Toast'

export const isOnline = signal(typeof navigator !== 'undefined' ? navigator.onLine : true)

if (typeof window !== 'undefined') {
  window.addEventListener('online', () => { isOnline.value = true })
  window.addEventListener('offline', () => { isOnline.value = false })
}

/** How long the socket must stay down before the banner appears. */
export const UNREACHABLE_GRACE_MS = 4000

export function OfflineIndicator() {
  const state = connectionState.value
  const down = state === 'connecting' || state === 'reconnecting'
  const [unreachable, setUnreachable] = useState(false)
  // Latched while the banner is (or was) on screen so the recovery
  // toast only fires when the user actually saw the outage.
  const shownRef = useRef(false)

  useEffect(() => {
    if (!down) {
      // ``peek`` — the effect keys on ``down`` alone so a
      // connecting → reconnecting hop doesn't restart the grace timer.
      if (shownRef.current && connectionState.peek() === 'open') {
        showToast(t('connection.reconnected'), 'success')
      }
      shownRef.current = false
      setUnreachable(false)
      return
    }
    const id = setTimeout(() => {
      shownRef.current = true
      setUnreachable(true)
    }, UNREACHABLE_GRACE_MS)
    return () => clearTimeout(id)
  }, [down])

  // Back online after a network drop: the socket may be sitting in a
  // long backoff wait — reconnect now instead of up to 60 s later.
  useEffect(() => {
    const onOnline = () => ws.retryNow()
    window.addEventListener('online', onOnline)
    return () => window.removeEventListener('online', onOnline)
  }, [])

  if (!isOnline.value) {
    return (
      <div class="sh-offline-banner" role="alert">
        {t('connection.offline')}
      </div>
    )
  }
  if (!unreachable) return null
  return (
    <div class="sh-offline-banner sh-offline-banner--server" role="alert">
      <span>{t('connection.unreachable')}</span>
      <button
        type="button"
        class="sh-offline-banner__retry"
        onClick={() => ws.retryNow()}
      >
        {t('connection.retry')}
      </button>
    </div>
  )
}
