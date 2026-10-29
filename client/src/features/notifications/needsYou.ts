/**
 * "Needs you" (D12): requests that wait for the viewer, pinned above the
 * Notifications page and the bell panel.
 *
 * - The list comes from ``GET /api/notifications/needs-you`` — read or not,
 *   a request stays until someone handles it (reading never un-pins it).
 * - **3 at a time** plus "Show N more"; two or more items about one space
 *   become one entry ("3 requests wait for you in Salon") with "Review".
 * - **Safe one-tap:** Approve / Decline only on a single join request with
 *   nothing unusual (no ``flags``: near the edge, an unpaired household, an
 *   admin grant, an age limit). The server holds the same rule: Approve
 *   is sent as ``via: inline`` and refused with ``409 REVIEW_REQUIRED``
 *   when the request is flagged after all — the item then turns into
 *   "Review" with the server's reasons. An invite gets "View invite" — the invite
 *   card asks a local community's location consent, so never a one-tap
 *   join — plus Decline. A game invite gets
 *   "Play" (opens the app, which takes the invite in) / Decline (their app
 *   hears it). Everything else gets "Review".
 * - **Decline waits 5 s** behind an Undo toast before it is sent (a
 *   federated decline can't be taken back); leaving the page sends it at
 *   once. Approve goes straight away.
 * - When anyone handles an item, the server's ``notification.unread_count``
 *   frame names it in ``handled`` and every device drops it; a tap that
 *   lost that race hears who won ("Søren already handled this").
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { dismissToast, showToast } from '@/components/Toast'
import type { Notification } from '@/types'

/** The types the server pins (``ACTION_ITEM_TYPES``). */
export const NEEDS_YOU_TYPES: ReadonlySet<string> = new Set([
  'space_join_requested',
  'moderation_pending',
  'space_report',
  'space_invite',
  'pairing_request',
  'app_challenge',
  'app_request',
])

/** Entries shown before "Show N more". */
export const NEEDS_YOU_CAP = 3
/** How long a Decline can be undone before it is sent. */
export const DECLINE_UNDO_MS = 5000

/** The pending items, newest first (the server's order). */
export const needsYou = signal<Notification[]>([])
/** Items an action is under way for (hidden until it lands or fails). */
export const acting = signal<ReadonlySet<string>>(new Set())

export type NeedsYouEntry =
  | { kind: 'single', key: string, row: Notification }
  | { kind: 'group', key: string, spaceId: string, rows: Notification[] }

export type InlineAction = 'decide' | 'invite' | 'game' | 'review'

function itemKey(n: Notification): string {
  return n.object_key || n.id
}

/** The entries to show: rows grouped per space (two or more in one space
 *  become one entry), newest first, without the ones being acted on. */
export function needsYouEntries(
  rows: Notification[],
  hidden: ReadonlySet<string> = new Set(),
): NeedsYouEntry[] {
  const visible = rows.filter(n => !hidden.has(itemKey(n)))
  const bySpace = new Map<string, Notification[]>()
  for (const n of visible) {
    if (!n.space_id) continue
    const list = bySpace.get(n.space_id) ?? []
    list.push(n)
    bySpace.set(n.space_id, list)
  }
  const out: NeedsYouEntry[] = []
  const done = new Set<string>()
  for (const n of visible) {
    const group = n.space_id ? bySpace.get(n.space_id) : undefined
    if (group && group.length > 1) {
      if (done.has(n.space_id!)) continue
      done.add(n.space_id!)
      out.push({ kind: 'group', key: `space:${n.space_id}`, spaceId: n.space_id!, rows: group })
    } else {
      out.push({ kind: 'single', key: itemKey(n), row: n })
    }
  }
  return out
}

/** Items the server refused a one-tap approval of (``409
 *  REVIEW_REQUIRED``), by item key, with the flags it named. They show
 *  "Review" from then on, even after a reload of the list. */
export const mustReview = signal<ReadonlyMap<string, readonly string[]>>(new Map())

/** Why *n* can't be decided with one tap: the server's answer to a refused
 *  tap, else the row's own flags. */
export function flagsOf(n: Notification): readonly string[] {
  return mustReview.value.get(itemKey(n)) ?? n.params?.flags ?? []
}

/** What an item offers inline (the safety rules above). */
export function inlineAction(n: Notification): InlineAction {
  if (n.type === 'space_invite') return 'invite'
  if (n.type === 'app_challenge' && n.object_key?.startsWith('app_session:')) return 'game'
  if (
    n.type === 'space_join_requested'
    && n.object_key?.startsWith('join_request:')
    && !!n.space_id
    && flagsOf(n).length === 0
  ) return 'decide'
  return 'review'
}

/** Where "Review" goes: the row's own page (the space's review queue,
 *  Connections), for a group the space's review queue. */
export function reviewHref(entry: NeedsYouEntry): string {
  if (entry.kind === 'single') return entry.row.link_url || '/notifications'
  return entry.rows.find(r => r.link_url)?.link_url || `/spaces/${entry.spaceId}/moderation`
}

/** The id after ``<kind>:`` in an object key. */
function idOf(n: Notification): string {
  const key = n.object_key ?? ''
  return key.slice(key.indexOf(':') + 1)
}

/** Load the pending items. */
export async function loadNeedsYou(): Promise<void> {
  try {
    const data = await api.get('/api/notifications/needs-you') as { items?: Notification[] }
    needsYou.value = Array.isArray(data?.items) ? data.items : []
  } catch { /* keep what's shown: the next frame or visit retries */ }
}

/** Someone handled these items (``handled`` on a WS frame): drop them. */
export function dropHandled(keys: readonly string[]): void {
  if (keys.length === 0) return
  const gone = new Set(keys)
  needsYou.value = needsYou.value.filter(n => !gone.has(itemKey(n)))
  if (keys.some(k => mustReview.value.has(k))) {
    const next = new Map(mustReview.value)
    for (const k of keys) next.delete(k)
    mustReview.value = next
  }
}

function setActing(key: string, on: boolean): void {
  const next = new Set(acting.value)
  if (on) next.add(key)
  else next.delete(key)
  acting.value = next
}

/** A refusal because someone else answered first: "Søren already handled
 *  this" (not an error). */
function lostRace(err: unknown): boolean {
  const e = err as { status?: unknown, extra?: { params?: unknown } } | null
  if (!e || e.status !== 409) return false
  const params = (e.extra?.params ?? {}) as Record<string, unknown>
  const by = typeof params.by === 'string' ? params.by.trim() : ''
  showToast(
    by ? t('notifications.needs_you.already', { name: by }) : t('notifications.needs_you.already_anon'),
    'info',
  )
  return true
}

/** A one-tap approval the server refused because the request needs the
 *  review page (``409 REVIEW_REQUIRED``, ``params.flags`` "age,edge"):
 *  the item stays and turns into "Review", with the server's reasons. */
function reviewRequired(err: unknown, key: string): boolean {
  const e = err as { status?: unknown, code?: unknown, extra?: { params?: unknown } } | null
  if (!e || e.status !== 409 || e.code !== 'REVIEW_REQUIRED') return false
  const params = (e.extra?.params ?? {}) as Record<string, unknown>
  const raw = typeof params.flags === 'string' ? params.flags : ''
  const flags = raw.split(',').map(f => f.trim()).filter(Boolean)
  const next = new Map(mustReview.value)
  // An empty list would read as "one tap" again: keep it non-empty.
  next.set(key, flags.length > 0 ? flags : ['review'])
  mustReview.value = next
  showToast(t('notifications.needs_you.review_required'), 'info')
  return true
}

async function settle(key: string, send: () => Promise<unknown>, done: string): Promise<void> {
  try {
    await send()
    dropHandled([key])
    showToast(done, 'success')
  } catch (err: unknown) {
    if (reviewRequired(err, key)) { /* stays, now as "Review" */ }
    else if (lostRace(err)) dropHandled([key])
    else showToast(t('notifications.needs_you.failed'), 'error')
  } finally {
    setActing(key, false)
    void loadNeedsYou()
  }
}

/** Approve a single join request now — as a one-tap (``via: inline``)
 *  approval, which the server refuses for a request that needs a closer
 *  look (it works the flags out itself). */
export function approve(n: Notification): Promise<void> {
  const key = itemKey(n)
  setActing(key, true)
  return settle(
    key,
    () => api.post(`/api/spaces/${n.space_id}/join-requests/${idOf(n)}/approve`, { via: 'inline' }),
    t('notifications.needs_you.approved'),
  )
}

/** ``app_session:<app_id>/<session_id>`` → its two parts. */
function gameOf(n: Notification): { appId: string, sessionId: string } {
  const id = idOf(n)
  const cut = id.lastIndexOf('/')
  return { appId: id.slice(0, cut), sessionId: id.slice(cut + 1) }
}

/** Where "Play" goes: that game in its app (``/apps/<id>?session=<sid>``)
 *  — the app host takes in that one invite and opens the app into it.
 *  Built from the row's subject, so rows stored before this work too. */
export function playHref(n: Notification): string {
  const { appId, sessionId } = gameOf(n)
  return `/apps/${encodeURIComponent(appId)}?session=${encodeURIComponent(sessionId)}`
}

/** Decline a join request, an invite (looked up by its space) or a game
 *  invite. */
async function sendDecline(n: Notification): Promise<void> {
  // keepalive: a decline sent as the page closes still arrives.
  if (n.type === 'space_join_requested') {
    await api.post(`/api/spaces/${n.space_id}/join-requests/${idOf(n)}/deny`, undefined, { keepalive: true })
    return
  }
  if (n.type === 'app_challenge') {
    const { appId, sessionId } = gameOf(n)
    await api.post(
      `/api/apps/${encodeURIComponent(appId)}/sessions/${encodeURIComponent(sessionId)}/decline`,
      undefined,
      { keepalive: true },
    )
    return
  }
  const spaceId = idOf(n)
  const local = await api.get('/api/local_invites') as { invitation_id: string, space_id: string }[]
  const mine = local.find(i => i.space_id === spaceId)
  if (mine) {
    await api.post(`/api/local_invites/${encodeURIComponent(mine.invitation_id)}/decline`)
    return
  }
  const remote = await api.get('/api/remote_invites') as { invite_token: string, space_id: string }[]
  const theirs = remote.find(i => i.space_id === spaceId)
  if (!theirs) throw Object.assign(new Error('invite gone'), { status: 409 })
  await api.post(`/api/remote_invites/${encodeURIComponent(theirs.invite_token)}/decline`)
}

/** What the undo toast and the confirmation say, per type: [while it can
 *  still be undone, once it was sent]. A join request uses the default. */
const DECLINE_TEXT: Record<string, readonly [string, string]> = {
  space_invite: ['notifications.needs_you.declining_invite', 'notifications.needs_you.invite_declined'],
  app_challenge: ['notifications.needs_you.declining_game', 'notifications.needs_you.game_declined'],
}

/** Declines waiting out their undo window, by item key. */
const waiting = new Map<string, { commit: () => void, toast: number }>()

/**
 * Decline *n* after :data:`DECLINE_UNDO_MS` unless Undo is pressed: the
 * item leaves the list at once, the request is sent when the toast runs
 * out (or :func:`flushDeclines` runs first — leaving the page).
 */
export function declineWithUndo(n: Notification): void {
  const key = itemKey(n)
  if (waiting.has(key)) return
  setActing(key, true)
  let settled = false
  let toast = -1
  const commit = () => {
    if (settled) return
    settled = true
    waiting.delete(key)
    dismissToast(toast) // sent early (leaving the page): nothing to undo
    void settle(key, () => sendDecline(n), t(DECLINE_TEXT[n.type]?.[1] ?? 'notifications.needs_you.declined_sent'))
  }
  toast = showToast(
    t(DECLINE_TEXT[n.type]?.[0] ?? 'notifications.needs_you.declining'),
    'info',
    {
      duration: DECLINE_UNDO_MS,
      action: {
        label: t('common.undo'),
        onClick: () => {
          if (settled) return
          settled = true
          waiting.delete(key)
          setActing(key, false)
        },
      },
      onExpire: commit,
    },
  )
  waiting.set(key, { commit, toast })
}

/** Send every decline still in its undo window now (the page is going). */
export function flushDeclines(): void {
  for (const { commit } of [...waiting.values()]) commit()
}

/** Declines still waiting (tests). */
export function waitingDeclines(): number {
  return waiting.size
}
