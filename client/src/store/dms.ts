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

function append(convo: string, msg: DmMessageLite): void {
  const existing = messagesByConversation.value[convo] ?? []
  if (existing.some((m) => m.message_id === msg.message_id)) return
  messagesByConversation.value = {
    ...messagesByConversation.value,
    [convo]: [...existing, msg],
  }
  inbox.value = { ...inbox.value, [convo]: msg }
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

export async function loadDmUnread(): Promise<void> {
  try {
    const rows = (await api.get('/api/conversations')) as Array<{
      unread?: number; muted_until?: string | null
    }>
    dmUnreadTotal.value = alertingUnread(rows)
  } catch {
    /* auth not ready or transient — leave the prior count visible */
  }
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
    append(d.conversation_id, {
      message_id:      msg.id,
      conversation_id: d.conversation_id,
      sender_user_id:  msg.sender_user_id ?? '',
      sender_display:  d.sender_display,
      content:         msg.content ?? '',
      occurred_at:     msg.created_at,
      edited_at:       msg.edited_at ?? null,
    })
    void loadDmUnread()
  })
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
