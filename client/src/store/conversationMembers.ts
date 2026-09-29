/**
 * Per-conversation member cache for @-mentions in DMs / group DMs
 * (§23.42).
 *
 * One ``GET /api/conversations/{id}/members`` per conversation (the DM
 * thread seeds it from the roster it already fetched for its header), then
 * the mention picker filters client-side — typing never hits the network.
 * Each row carries the server-issued ``mention`` token, so the SPA inserts
 * and highlights only what the household resolves.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { mentionTokenSet, type MentionMember } from '@/utils/mentions'

export const conversationMembers = signal<Record<string, Map<string, MentionMember>>>({})

const loaded = new Set<string>()

/** Store a roster fetched elsewhere (the thread header's own fetch). */
export function setConversationMembers(
  convId: string,
  rows: readonly MentionMember[],
): void {
  loaded.add(convId)
  const m = new Map<string, MentionMember>()
  for (const r of rows) m.set(r.user_id, r)
  conversationMembers.value = { ...conversationMembers.value, [convId]: m }
}

export async function loadConversationMembers(convId: string): Promise<void> {
  if (loaded.has(convId)) return
  loaded.add(convId)
  try {
    const rows = await api.get(`/api/conversations/${convId}/members`) as MentionMember[]
    setConversationMembers(convId, rows)
  } catch {
    loaded.delete(convId)
  }
}

export function invalidateConversationMembers(convId: string): void {
  loaded.delete(convId)
}

export interface ConversationMentionRender {
  mentions: ReadonlySet<string>
  selfMention: string | null
}

const _tokenCache = new WeakMap<Map<string, MentionMember>, Set<string>>()

/** Lower-cased member tokens of ``convId`` (empty until the roster loads —
 *  nothing is highlighted rather than guessing) and the viewer's own
 *  token. Reads the signal, so a caller re-renders when the roster lands.
 *  ``@here`` is never a chat mention. */
export function conversationMentionRender(
  convId: string | null | undefined,
): ConversationMentionRender {
  const roster = convId ? conversationMembers.value[convId] : undefined
  if (!roster) return { mentions: new Set(), selfMention: null }
  let tokens = _tokenCache.get(roster)
  if (!tokens) {
    tokens = mentionTokenSet(roster.values())
    _tokenCache.set(roster, tokens)
  }
  const me = currentUser.value?.user_id
  return {
    mentions: tokens,
    selfMention: (me && roster.get(me)?.mention) || null,
  }
}
