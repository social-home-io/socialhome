/**
 * DMs store — driven by `dm.message`, `dm.message_reaction` and
 * `conversation.user_typing` WS frames.
 *
 * DmInboxPage reads :data:`inbox` (latest message per conversation).
 * DmThreadPage reads :data:`messagesByConversation[conversationId]`
 * and :data:`typingByConversation[conversationId]` — both update
 * without manual polling thanks to the WS subscription wired below.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { isMuteActive, type MuteDuration } from '@/utils/mute'
import { createLiveThrottle } from '@/utils/liveThrottle'
import { currentUser } from '@/store/auth'
import type { Conversation } from '@/types'

export interface DmReaction {
  user_id: string
  emoji:   string
}

export interface DmMessageLite {
  message_id:      string
  conversation_id: string
  sender_user_id:  string
  sender_display?: string
  content:         string
  occurred_at?:    string
  edited_at?:      string | null
  reactions?:      DmReaction[]
}

export interface DmReactionPatch {
  message_id:      string
  conversation_id: string
  emoji:           string
  user_id:         string
  action:          'add' | 'remove'
}

export interface TypingIndicator {
  conversation_id: string
  user_id:         string
  until:           number
}

export const inbox = signal<Record<string, DmMessageLite>>({})
export const messagesByConversation = signal<Record<string, DmMessageLite[]>>({})
export const typingByConversation = signal<Record<string, TypingIndicator[]>>({})

/** Total unread DMs across every conversation the user is a member
 *  of. Refreshed by :class:`DmInboxPage` after each
 *  ``GET /api/conversations`` (initial fetch + post-WS refetches),
 *  consumed by the sidebar to render the Chats badge. Stays a plain
 *  signal — derived state lives at the producer's boundary so a
 *  reload page that never mounts the inbox still sees zero, never a
 *  stale count. */
export const dmUnreadTotal = signal<number>(0)

/** ``false`` when the message was already there (a reconnect replay). */
function append(convo: string, msg: DmMessageLite): boolean {
  const existing = messagesByConversation.value[convo] ?? []
  if (existing.some((m) => m.message_id === msg.message_id)) return false
  messagesByConversation.value = {
    ...messagesByConversation.value,
    [convo]: [...existing, msg],
  }
  inbox.value = { ...inbox.value, [convo]: msg }
  return true
}


/** Apply a reaction add / remove to the cached copy of a message,
 *  deduping so a duplicate ``add`` frame (reconnect / multi-session)
 *  doesn't stack the same (user, emoji) twice and a ``remove`` for a
 *  reaction that isn't present is a no-op. Mirrors
 *  ``DmThreadPage``'s ``offReaction`` handler — the working reference. */
function applyReaction(patch: DmReactionPatch): void {
  const list = messagesByConversation.value[patch.conversation_id]
  if (!list) return
  const idx = list.findIndex((m) => m.message_id === patch.message_id)
  if (idx < 0) return
  const current = list[idx].reactions ?? []
  const exists = current.some(
    (r) => r.user_id === patch.user_id && r.emoji === patch.emoji,
  )
  let nextReactions: DmReaction[]
  if (patch.action === 'remove') {
    if (!exists) return
    nextReactions = current.filter(
      (r) => !(r.user_id === patch.user_id && r.emoji === patch.emoji),
    )
  } else {
    if (exists) return
    nextReactions = [...current, { user_id: patch.user_id, emoji: patch.emoji }]
  }
  const next = list.slice()
  next[idx] = { ...next[idx], reactions: nextReactions }
  messagesByConversation.value = {
    ...messagesByConversation.value,
    [patch.conversation_id]: next,
  }
}

function addTyping(convo: string, userId: string, ttlSeconds = 6): void {
  const until = Date.now() + ttlSeconds * 1000
  const existing = (typingByConversation.value[convo] ?? []).filter(
    (t) => t.user_id !== userId && t.until > Date.now(),
  )
  typingByConversation.value = {
    ...typingByConversation.value,
    [convo]: [...existing, { conversation_id: convo, user_id: userId, until }],
  }
}

/** The Chats badge total: unread across every conversation the viewer
 *  hasn't muted. A muted conversation keeps its own per-row count in the
 *  inbox but doesn't pull the viewer back with a badge. */
export function alertingUnread(
  rows: ReadonlyArray<{ unread?: number; muted_until?: string | null }> | null | undefined,
  now: number = Date.now(),
): number {
  let sum = 0
  for (const r of rows ?? []) {
    if (isMuteActive(r.muted_until, now)) continue
    sum += Math.max(0, r.unread ?? 0)
  }
  return sum
}

/** Mute (``duration``) or with ``null`` unmute a conversation for the
 *  viewer. Resolves the new ``muted_until`` (``null`` = unmuted). */
export async function setConversationMute(
  convId: string,
  duration: MuteDuration | null,
): Promise<string | null> {
  let until: string | null = null
  if (duration === null) {
    await api.delete(`/api/conversations/${convId}/mute`)
  } else {
    const body = await api.put(
      `/api/conversations/${convId}/mute`, { duration },
    ) as { muted_until: string | null }
    until = body.muted_until ?? null
  }
  // The badge excludes muted rows — re-sum so it moves right away.
  void loadDmUnread()
  return until
}

/** A group member's own notification level (§23.42). */
export type ConversationNotifLevel = 'all' | 'mentions'

/** Set the viewer's own level for a group; resolves the saved level. */
export async function setConversationNotifLevel(
  convId: string,
  level: ConversationNotifLevel,
): Promise<ConversationNotifLevel> {
  const body = await api.put(
    `/api/conversations/${convId}/notif-prefs`, { level },
  ) as { level?: string }
  return body.level === 'mentions' ? 'mentions' : 'all'
}

/** The fields of a ``GET /api/conversations`` row this store keeps live. */
export interface ConversationRowLite {
  id?: string
  unread?: number
  muted_until?: string | null
  last_message_at?: string | null
}

/** The viewer's conversations as ``GET /api/conversations`` last answered,
 *  kept live from ``dm.message`` frames: a message from someone else
 *  counts one unread on its row and moves it to the top; reading a
 *  thread zeroes it. The Chats inbox renders it; the badge sums it. */
export const conversationRows = signal<Conversation[]>([])

/** Whether ``conversationRows`` came from the server yet. */
let rowsLoaded = false

/** Take a fresh ``GET /api/conversations`` answer: the rows and the badge. */
export function applyConversationRows(rows: ConversationRowLite[]): void {
  conversationRows.value = rows as Conversation[]
  rowsLoaded = true
  dmUnreadTotal.value = alertingUnread(rows)
}

function patchRows(next: Conversation[]): void {
  conversationRows.value = next
  dmUnreadTotal.value = alertingUnread(next)
}

/** The viewer read ``convId`` (the thread marks read): its count is zero
 *  now, before the server's answer. */
export function noteConversationRead(convId: string): void {
  const rows = conversationRows.value
  const i = rows.findIndex(r => r.id === convId)
  if (i < 0 || !rows[i].unread) return
  const next = rows.slice()
  next[i] = { ...rows[i], unread: 0 }
  patchRows(next)
}

/** A new message in ``convId``: one more unread on its row (not for the
 *  viewer's own), the row to the top. ``false`` when the row isn't known
 *  — a new conversation, or the list isn't loaded yet. */
function noteIncoming(convId: string, mine: boolean, at: string | undefined): boolean {
  const rows = conversationRows.value
  const i = rows.findIndex(r => r.id === convId)
  if (!rowsLoaded || i < 0) return false
  const row: Conversation = {
    ...rows[i],
    unread: mine ? rows[i].unread : (rows[i].unread ?? 0) + 1,
    last_message_at: at ?? rows[i].last_message_at,
  }
  patchRows([row, ...rows.slice(0, i), ...rows.slice(i + 1)])
  return true
}

export async function loadDmUnread(): Promise<void> {
  try {
    const rows = (await api.get('/api/conversations')) as ConversationRowLite[]
    applyConversationRows(rows)
  } catch {
    /* auth not ready or transient — leave the prior count visible */
  }
}

/** A full refetch the frames can't replace (a conversation we don't have
 *  a row for), at most once per {@link DM_LIST_REFRESH_GAP_MS} with one
 *  trailing call — a burst of new conversations costs two GETs, not one
 *  per message. Paused while the tab is hidden. */
export const DM_LIST_REFRESH_GAP_MS = 5_000
let listRefresh = createLiveThrottle(() => { void loadDmUnread() }, {
  minGapMs: DM_LIST_REFRESH_GAP_MS, pauseWhileHidden: true,
})

/** Test seam: forget the rows and the refresh budget. */
export function resetDmStoreForTests(): void {
  listRefresh.cancel()
  listRefresh = createLiveThrottle(() => { void loadDmUnread() }, {
    minGapMs: DM_LIST_REFRESH_GAP_MS, pauseWhileHidden: true,
  })
  conversationRows.value = []
  rowsLoaded = false
}

/** A ``dm.*`` frame for a system chat (``system_scope`` set). */
export function isSystemChatFrame(data: unknown): boolean {
  const scope = (data as { system_scope?: unknown } | null)?.system_scope
  return typeof scope === 'string' && scope !== ''
}

export function wireDmWs(): void {
  ws.on('dm.message', (e) => {
    // The real frame nests the message: ``{type, conversation_id,
    // sender_display, message: {id, sender_user_id, content, ...}}`` —
    // the id is ``message.id`` (NOT ``message_id``). The previous code
    // treated ``e.data`` as the message and guarded on a field that
    // never exists, so the handler was dead. Match
    // ``DmThreadPage``'s working ``offNewMsg`` shape.
    const d = e.data as {
      conversation_id?: string
      system_scope?: string | null
      sender_display?: string
      message?: {
        id?: string
        sender_user_id?: string
        content?: string
        created_at?: string
        edited_at?: string | null
      }
    }
    const msg = d.message
    if (!d.conversation_id || !msg?.id) return
    // A system chat (the household chat, a space's chat) rides the DM
    // frames but lives on its own tab: never in the inbox, never in the
    // Chats badge (the server leaves it out of ``GET /api/conversations``
    // too, so a refetch here would only cost a request).
    if (isSystemChatFrame(d)) return
    const added = append(d.conversation_id, {
      message_id:      msg.id,
      conversation_id: d.conversation_id,
      sender_user_id:  msg.sender_user_id ?? '',
      sender_display:  d.sender_display,
      content:         msg.content ?? '',
      occurred_at:     msg.created_at,
      edited_at:       msg.edited_at ?? null,
    })
    if (!added) return
    // Counted from the frame — no list refetch per message. Only a
    // conversation without a row yet needs the server's list.
    const mine = !!msg.sender_user_id && msg.sender_user_id === currentUser.value?.user_id
    if (!noteIncoming(d.conversation_id, mine, msg.created_at)) listRefresh.trigger()
  })
  // A conversation someone started with the viewer: its row comes from
  // the server (members, name), on the same refetch budget.
  ws.on('dm.conversation.created', () => { listRefresh.trigger() })
  ws.on('dm.message_reaction', (e) => {
    // The frame is a patch: ``{conversation_id, message_id, user_id,
    // emoji, action}``. The previous code cloned the array but never
    // applied the change — the reaction strip never updated. Apply the
    // add / remove for real (dedup on add, no-op on absent remove).
    const r = e.data as unknown as DmReactionPatch
    if (!r?.conversation_id || !r?.message_id || !r?.user_id || !r?.emoji) {
      return
    }
    applyReaction(r)
  })
  ws.on('conversation.user_typing', (e) => {
    const t = e.data as unknown as { conversation_id: string, user_id: string, ttl?: number }
    if (!t?.conversation_id || !t?.user_id) return
    addTyping(t.conversation_id, t.user_id, t.ttl ?? 6)
  })
}
