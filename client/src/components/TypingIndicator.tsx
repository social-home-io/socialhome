/**
 * TypingIndicator — show who is typing (§23.9).
 *
 * Subscribes to two WS frames published by ``TypingService``:
 *
 *   • ``conversation.user_typing`` — DM threads (legacy keyspace).
 *   • ``comment.user_typing``      — comment threads on a feed post.
 *
 * Both share a single in-memory map keyed by ``<scope>|<user_id>`` so
 * indicators across the app don't cross-talk. Consumers filter by the
 * ``scope`` prop, which can be:
 *
 *   • ``conversationId``       — DM thread typing
 *   • ``post:<postId>``        — comment-thread typing
 *
 * The ``sendTyping`` helper carries the right scope to the server: DMs
 * pass a string conversation_id, comment threads pass an object
 * ``{postId, spaceId?}``.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'
import { householdDisplayName } from '@/store/householdUsers'
import { t } from '@/i18n/i18n'

interface TypingState {
  scope: string
  /** ``user_id`` from the WS frame. We resolve this to a friendly
   *  display name at render time via :func:`householdDisplayName` so a
   *  late ``user.profile_updated`` (avatar change, rename) is reflected
   *  without rebroadcasting the typing event. ``sender_username`` from
   *  the frame is kept as a fallback for users not yet in the
   *  household cache (e.g. cross-household friends). */
  userId: string
  fallbackName: string
  ts: number
}

const typingUsers = signal<Map<string, TypingState>>(new Map())

if (typeof window !== 'undefined') {
  ws.on('conversation.user_typing', (evt) => {
    const data = evt.data as {
      conversation_id?: string
      sender_user_id?: string
      sender_username?: string
    }
    const cid = data.conversation_id
    const uid = data.sender_user_id
    if (!cid || !uid) return
    const map = new Map(typingUsers.value)
    map.set(`${cid}|${uid}`, {
      scope: cid,
      userId: uid,
      fallbackName: data.sender_username || uid,
      ts: Date.now(),
    })
    typingUsers.value = map
  })
  // Comment-thread typing — same shape, scope key is ``post:<post_id>``
  // so the in-memory map can carry both DM and comment indicators
  // without cross-talk.
  ws.on('comment.user_typing', (evt) => {
    const data = evt.data as {
      post_id?: string
      sender_user_id?: string
      sender_username?: string
    }
    const pid = data.post_id
    const uid = data.sender_user_id
    if (!pid || !uid) return
    const scope = `post:${pid}`
    const map = new Map(typingUsers.value)
    map.set(`${scope}|${uid}`, {
      scope,
      userId: uid,
      fallbackName: data.sender_username || uid,
      ts: Date.now(),
    })
    typingUsers.value = map
  })
  // Sweep entries older than 3 s — server publishes on each keystroke.
  setInterval(() => {
    const now = Date.now()
    const map = new Map(typingUsers.value)
    let changed = false
    for (const [key, st] of map) {
      if (now - st.ts > 3000) { map.delete(key); changed = true }
    }
    if (changed) typingUsers.value = map
  }, 1000)
}

/** Send a typing event.
 *
 *  - String argument → DM thread (conversation_id).
 *  - Object argument → comment thread on a feed post; ``spaceId`` is
 *    optional (omit / null for household feed posts).
 */
export function sendTyping(
  scope: string | { postId: string; spaceId?: string | null },
): void {
  if (typeof scope === 'string') {
    ws.send('typing', { conversation_id: scope })
  } else {
    ws.send('typing', {
      post_id: scope.postId,
      ...(scope.spaceId ? { space_id: scope.spaceId } : {}),
    })
  }
}

interface TypingIndicatorProps {
  scope?: string
  /** When ``true`` the indicator renders as a chat-bubble row,
   *  styled to sit at the bottom of a messages list (DM thread).
   *  Default ``false`` keeps the older inline-pill render that
   *  comment threads still use. */
  bubble?: boolean
}

export function TypingIndicator({ scope, bubble }: TypingIndicatorProps) {
  const all = Array.from(typingUsers.value.values())
  const users = scope ? all.filter(s => s.scope === scope) : all
  if (users.length === 0) return null
  // Resolve friendly display names lazily so a profile rename shows up
  // without rebroadcasting the typing frame. ``householdDisplayName``
  // already falls back to the username, and the WS-frame
  // ``fallbackName`` covers the cross-household case where the user
  // isn't in the local household cache yet.
  const names = users.map(s => {
    const friendly = householdDisplayName(s.userId)
    // ``householdDisplayName`` returns the bare user_id when the user
    // isn't cached; prefer the WS-provided username in that case.
    return friendly && friendly !== s.userId ? friendly : s.fallbackName
  })
  const label = names.length === 1 ? t('typing.one', { name: names[0] })
    : names.length === 2 ? t('typing.two', { a: names[0], b: names[1] })
    : t('typing.many', { n: String(names.length) })
  const cls = bubble ? 'sh-typing sh-typing--bubble' : 'sh-typing'
  return (
    <div class={cls} aria-live="polite">
      <span class="sh-typing-dots" aria-hidden="true">•••</span>
      <span class="sh-typing-label">{label}</span>
    </div>
  )
}
