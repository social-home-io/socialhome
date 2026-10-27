/**
 * Notifications store — driven by `notification.new` and
 * `notification.unread_count` WS frames (§21).
 *
 * The bell badge in the top bar reads :data:`unreadCount`; the
 * notifications page reads :data:`recent`. Both update without
 * polling thanks to the WS subscription wired below.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'

export interface NotificationLite {
  notification_id: string
  notif_type:      string
  title:           string
  /** Optional deep-link target carried by the ``notification.new``
   *  WS frame so the bell can render unread items as anchors —
   *  tapping a ``dm_message`` row jumps straight into the thread,
   *  which auto-clears the row server-side via the read-receipt
   *  hook. */
  link_url?:       string | null
  occurred_at?:    string
  /** The frame refreshed a row that was already unread (a chat burst
   *  collapses into one row): move it up, never count it again. */
  bumped?:         boolean
}

export const recent      = signal<NotificationLite[]>([])
export const unreadCount = signal<number>(0)

export function wireNotificationsWs(): void {
  ws.on('notification.new', (e) => {
    const n = e.data as unknown as NotificationLite
    if (n.bumped) {
      // The same unread row with a fresh title: to the top, the badge as
      // is. The server's count did not move — after a reload ``recent``
      // is empty, so the id check alone counted every message of a burst.
      const rest = recent.value.filter((x) => x.notification_id !== n.notification_id)
      recent.value = [n, ...rest].slice(0, 50)
      return
    }
    // Dedup by id — a duplicate frame (reconnect / multi-session) must
    // not double the badge or insert a second row. Mirrors feed.ts's
    // ``.some(p => p.id === post.id)`` guard.
    if (recent.value.some((x) => x.notification_id === n.notification_id)) return
    recent.value = [n, ...recent.value].slice(0, 50)
    unreadCount.value = unreadCount.value + 1
  })
  ws.on('notification.unread_count', (e) => {
    const c = (e.data as { unread_count: number }).unread_count
    if (typeof c === 'number') unreadCount.value = c
  })
}

/** Pages seen since the last flush, sent together in one call. */
let seenQueue: string[] = []
let seenTimer: ReturnType<typeof setTimeout> | null = null
const SEEN_FLUSH_MS = 250
/** The server takes at most this many pages per call. */
const SEEN_BATCH_MAX = 20

/**
 * The page now shows ``link``'s thing (a post, a moment, a highlight, a
 * space's feed): every unread notification of the viewer that opens it
 * reads as read, on every device (the server answers with a
 * ``notification.unread_count`` frame). ``link`` is the in-app path a
 * notification carries — ``/post/p1``, ``/spaces/s1?post=p1``,
 * ``/momentum/m1``, ``/highlights/h1``, ``/spaces/s1`` — and a picked-out
 * comment or reply needs no call of its own.
 *
 * Call it only once the thing is on screen, never for something the
 * viewer did not get to see. Calls within a short window go out as one
 * request; a failure is silent (the row just stays unread).
 */
export function markSeen(link: string): void {
  if (!seenQueue.includes(link)) seenQueue.push(link)
  if (seenTimer === null) seenTimer = setTimeout(() => { void flushSeen() }, SEEN_FLUSH_MS)
}

/** The link a post's notifications open: its permalink for a household
 *  post, the space with ``?post=`` for a space post (the server's
 *  ``notification_links``). */
export function postLink(postId: string, spaceId: string | null): string {
  return spaceId
    ? `/spaces/${spaceId}?post=${encodeURIComponent(postId)}`
    : `/post/${postId}`
}

/** Send the queued pages now (the timer's job; exported for tests). */
export async function flushSeen(): Promise<void> {
  if (seenTimer !== null) clearTimeout(seenTimer)
  seenTimer = null
  const links = seenQueue.slice(0, SEEN_BATCH_MAX)
  seenQueue = seenQueue.slice(SEEN_BATCH_MAX)
  if (seenQueue.length > 0) {
    seenTimer = setTimeout(() => { void flushSeen() }, SEEN_FLUSH_MS)
  }
  if (links.length === 0) return
  try {
    await api.post('/api/notifications/read-by-link', { links })
  } catch { /* a hint: the row stays unread, nothing else depends on it */ }
}
