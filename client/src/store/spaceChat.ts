/**
 * Space chat store — the Chat view of a space's Feed tab.
 *
 * Every space has a hidden system group conversation of its members
 * (``system_scope = 'space'``). Its summary comes from
 * ``GET /api/spaces/{id}/chat`` (``enabled: false`` for a follower and
 * while the space's admins turned chat off; 404 for a non-member); its
 * messages, reads, reactions, deletes, mute and level use the ordinary
 * ``/api/conversations/{id}/...`` routes. One space page is open at a
 * time, so the store holds that space's summary only — a late answer for
 * a space the viewer already left is dropped.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { isWriterRole, type SpaceRole } from '@/features/spaces/spaceRoles'
import { isMuteActive } from '@/utils/mute'
import { parseChatSummary, type SystemChatSummary } from './householdChat'

/** The open space's summary; ``null`` before the first answer. */
export const spaceChat = signal<SystemChatSummary | null>(null)
/** Which space :data:`spaceChat` belongs to. */
export const spaceChatSpace = signal<string | null>(null)
/** The space whose last summary fetch failed (its Chat view shows a
 *  retry); ``null`` = none. */
export const spaceChatError = signal<string | null>(null)

let currentSpace: string | null = null

/** Start over for ``spaceId`` (the space page opened another space). */
export function resetSpaceChat(spaceId: string | null): void {
  currentSpace = spaceId
  spaceChat.value = null
  spaceChatSpace.value = null
  spaceChatError.value = null
}

/** The summary when it belongs to ``spaceId``, else ``null`` — the page
 *  never shows another space's chat for the render before its reset. */
export function spaceChatOf(spaceId: string): SystemChatSummary | null {
  return spaceChatSpace.value === spaceId ? spaceChat.value : null
}

/** Fetch ``spaceId``'s summary. A failure keeps the previous summary and
 *  names the space in :data:`spaceChatError`; an answer for another space
 *  is dropped. */
export async function loadSpaceChat(spaceId: string): Promise<void> {
  currentSpace = spaceId
  try {
    const body = await api.get(`/api/spaces/${spaceId}/chat`) as Partial<SystemChatSummary> | null
    if (currentSpace !== spaceId) return
    spaceChat.value = parseChatSummary(body)
    spaceChatSpace.value = spaceId
    spaceChatError.value = null
    syncChatUnreadFromSummary(spaceId, spaceChat.value)
  } catch {
    if (currentSpace === spaceId) spaceChatError.value = spaceId
  }
}

/** Patch the cached summary (unread, mute, level) without a refetch. */
export function patchSpaceChat(patch: Partial<SystemChatSummary>): void {
  if (!spaceChat.value) return
  spaceChat.value = { ...spaceChat.value, ...patch }
  if (spaceChatSpace.value) syncChatUnreadFromSummary(spaceChatSpace.value, spaceChat.value)
}

// ── Per-space unread for the spaces list ──────────────────────────────

/** One space's chat as the spaces list's unread dot needs it
 *  (``GET /api/spaces/chat-unread``): the honest count (``0`` while
 *  muted, only @-mentions at ``mentions``) and the level / mute that
 *  live frames are judged by. */
export interface SpaceChatUnread {
  unread: number
  notif_level: 'all' | 'mentions'
  muted_until: string | null
}

/** space id → its chat's unread; a space without a chat seat (a
 *  follower, chat off, never opened) is absent. */
export const spaceChatUnread = signal<Record<string, SpaceChatUnread>>({})

function parseUnreadRow(raw: unknown): SpaceChatUnread | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Partial<Record<keyof SpaceChatUnread, unknown>>
  const n = typeof r.unread === 'number' && Number.isFinite(r.unread) ? r.unread : 0
  return {
    unread: Math.max(0, Math.floor(n)),
    notif_level: r.notif_level === 'all' ? 'all' : 'mentions',
    muted_until: typeof r.muted_until === 'string' ? r.muted_until : null,
  }
}

/** The read of the counts in flight, and whether one more is owed. */
let unreadInflight: Promise<void> | null = null
let unreadAgain = false

/** Fetch every space's chat unread in one call. A failure keeps what
 *  the list already shows. */
export function loadSpaceChatUnread(): Promise<void> {
  // Coalesced: one request at a time. A call (or a live frame) while one
  // is in flight asks for exactly one more read after it — that answer
  // may predate what triggered the call, and the newer read reconciles a
  // bump the older answer would have overwritten.
  if (unreadInflight) {
    unreadAgain = true
    return unreadInflight
  }
  unreadInflight = (async () => {
    try {
      do {
        unreadAgain = false
        if (!await fetchSpaceChatUnread()) break
      } while (unreadAgain)
    } finally {
      unreadInflight = null
      unreadAgain = false
    }
  })()
  return unreadInflight
}

/** One read of ``GET /api/spaces/chat-unread``; ``false`` on a failure
 *  (the list keeps the last answer). */
async function fetchSpaceChatUnread(): Promise<boolean> {
  try {
    const body = await api.get('/api/spaces/chat-unread') as { spaces?: unknown } | null
    const raw = body?.spaces
    if (!raw || typeof raw !== 'object') return true
    const next: Record<string, SpaceChatUnread> = {}
    for (const [id, row] of Object.entries(raw as Record<string, unknown>)) {
      const parsed = parseUnreadRow(row)
      if (parsed) next[id] = parsed
    }
    spaceChatUnread.value = next
    return true
  } catch {
    return false
  }
}

/** A ``dm.message_deleted`` frame: whether the spaces list must re-read
 *  its counts. The frame carries no sender nor whether the message was
 *  still unread, so a space chat that shows a count re-reads it (a
 *  deleted unread message must stop counting); one at 0 can't go lower. */
export function spaceChatDeleteNeedsReload(data: unknown): boolean {
  const d = data as { system_scope?: unknown; space_id?: unknown } | null
  if (d?.system_scope !== 'space' || typeof d.space_id !== 'string') return false
  return (spaceChatUnread.value[d.space_id]?.unread ?? 0) > 0
}

/** The unread count the spaces list shows for ``spaceId`` (0 = no dot). */
export function spaceChatUnreadOf(spaceId: string): number {
  const row = spaceChatUnread.value[spaceId]
  if (!row || isMuteActive(row.muted_until)) return 0
  return row.unread
}

/** The viewer is looking at ``spaceId``'s chat: its dot goes. */
export function clearSpaceChatUnread(spaceId: string): void {
  const row = spaceChatUnread.value[spaceId]
  if (!row || row.unread === 0) return
  spaceChatUnread.value = { ...spaceChatUnread.value, [spaceId]: { ...row, unread: 0 } }
}

/** Keep the list's row in step with a fresher summary of the open
 *  space (its count, level and mute). */
function syncChatUnreadFromSummary(spaceId: string, chat: SystemChatSummary | null): void {
  const map = spaceChatUnread.value
  if (!chat?.enabled) {
    if (!(spaceId in map)) return
    const { [spaceId]: _gone, ...rest } = map
    spaceChatUnread.value = rest
    return
  }
  const muted = isMuteActive(chat.muted_until)
  spaceChatUnread.value = {
    ...map,
    [spaceId]: {
      unread: muted ? 0 : chat.unread,
      notif_level: chat.notif_level === 'all' ? 'all' : 'mentions',
      muted_until: chat.muted_until,
    },
  }
}

/** A live ``dm.message`` frame for the spaces list. Returns ``'bumped'``
 *  when it raised a space's count, ``'unknown'`` for a space chat the
 *  list has no row for yet (the caller refetches: a first message may
 *  have just created the chat and its seat), else ``'ignored'`` — not a
 *  space chat, the viewer's own message, a muted chat, or chatter at
 *  "Only @mentions" that doesn't mention the viewer. */
export function applySpaceChatUnreadFrame(
  data: unknown,
  myUserId: string | null | undefined,
): 'bumped' | 'unknown' | 'ignored' {
  const d = data as {
    system_scope?: unknown
    space_id?: unknown
    mentions_you?: unknown
    message?: { sender_user_id?: unknown }
  } | null
  if (d?.system_scope !== 'space' || typeof d.space_id !== 'string') return 'ignored'
  if (myUserId && d.message?.sender_user_id === myUserId) return 'ignored'
  const row = spaceChatUnread.value[d.space_id]
  if (!row) return 'unknown'
  if (isMuteActive(row.muted_until)) return 'ignored'
  if (row.notif_level === 'mentions' && d.mentions_you !== true) return 'ignored'
  spaceChatUnread.value = {
    ...spaceChatUnread.value,
    [d.space_id]: { ...row, unread: row.unread + 1 },
  }
  // A read in flight may answer from before this message: read again.
  if (unreadInflight) unreadAgain = true
  return 'bumped'
}

/** The ``data`` of a ``dm.*`` frame for ``spaceId``'s chat. */
export function isSpaceChatFrame(data: unknown, spaceId: string): boolean {
  const d = data as { system_scope?: unknown; space_id?: unknown } | null
  return d?.system_scope === 'space' && d.space_id === spaceId
}

/** The two views of a space's Feed tab. */
export type SpaceFeedView = 'posts' | 'chat'

/** The views to offer, in switch order. Chat needs the space's ``chat``
 *  feature on (absent = on, the backend default), a viewer the member
 *  list named as a writer (owner / admin / moderator / member — never a
 *  follower or a non-member), and a summary that doesn't say "off".
 *  A summary still loading counts as on, so the switch doesn't jump in
 *  once the answer lands. */
export function spaceFeedViews(
  features: { chat?: boolean } | undefined,
  role: SpaceRole | undefined,
  chat: Pick<SystemChatSummary, 'enabled'> | null,
): SpaceFeedView[] {
  if (features?.chat === false) return ['posts']
  if (!isWriterRole(role)) return ['posts']
  if (chat !== null && !chat.enabled) return ['posts']
  return ['posts', 'chat']
}

/** The view a ``?view=`` query value asks for. */
export function spaceFeedViewFromQuery(v: unknown): SpaceFeedView {
  return v === 'chat' ? 'chat' : 'posts'
}
