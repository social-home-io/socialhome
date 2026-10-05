/**
 * Web Push subscribe / unsubscribe for this browser (§23.33, §25.3).
 *
 * Backend contract (``socialhome/routes/push.py``):
 *   * ``GET    /api/push/vapid_public_key``   → ``{public_key}`` (base64url)
 *   * ``POST   /api/push/subscribe``          ← ``PushSubscription.toJSON()`` + ``id``
 *   * ``DELETE /api/push/subscribe/{sub_id}`` → 204 / 404
 *
 * The server never echoes a subscription's ``endpoint`` back (it is in
 * ``SENSITIVE_FIELDS``), so the browser can't look its row up later. We
 * therefore pick the row id ourselves — a SHA-256 of the endpoint — and
 * send it on subscribe; unsubscribe recomputes it from the live
 * ``PushSubscription`` and deletes exactly that row.
 */
import { api, ApiError } from '@/api'
import { t } from '@/i18n/i18n'

/** True when this browser can do Web Push at all. */
export function webPushSupported(): boolean {
  return typeof navigator !== 'undefined'
    && 'serviceWorker' in navigator
    && typeof window !== 'undefined'
    && 'PushManager' in window
    && typeof Notification !== 'undefined'
}

/** Stable server-side row id for a push endpoint. */
export async function pushSubscriptionId(endpoint: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(endpoint),
  )
  const hex = Array.from(new Uint8Array(digest))
    .map(b => b.toString(16).padStart(2, '0')).join('')
  return `sub-${hex.slice(0, 32)}`
}

function base64UrlToBytes(b64url: string): Uint8Array {
  const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
  const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

/** The browser's current push subscription, if any. */
export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!webPushSupported()) return null
  const reg = await navigator.serviceWorker.getRegistration()
  return (await reg?.pushManager?.getSubscription()) ?? null
}

/** Ask for permission, subscribe this browser and register it with the
 *  server. Returns false when the user declined permission; throws on
 *  any other failure so the caller can tell the user. */
export async function enableWebPush(): Promise<boolean> {
  if (!webPushSupported()) throw new Error(t('push.unsupported'))
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') return false
  // Relative URL — resolves against ``document.baseURI`` so the worker
  // loads from the ingress prefix under HA Supervisor, ``/sw.js`` otherwise.
  const reg = await navigator.serviceWorker.register('sw.js')
  await navigator.serviceWorker.ready
  const { public_key } = await api.get('/api/push/vapid_public_key') as { public_key: string }
  const sub = await reg.pushManager.getSubscription()
    ?? await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToBytes(public_key) as BufferSource,
    })
  const json = sub.toJSON()
  await api.post('/api/push/subscribe', {
    ...json,
    id: await pushSubscriptionId(sub.endpoint),
  })
  return true
}

/** Remove this browser's subscription on the server, then unsubscribe it
 *  locally. A 404 means the server row is already gone — fine. */
export async function disableWebPush(): Promise<void> {
  const sub = await currentPushSubscription()
  if (!sub) return
  const id = await pushSubscriptionId(sub.endpoint)
  try {
    await api.delete(`/api/push/subscribe/${encodeURIComponent(id)}`)
  } catch (err: unknown) {
    if (!(err instanceof ApiError && err.status === 404)) throw err
  }
  await sub.unsubscribe()
}
