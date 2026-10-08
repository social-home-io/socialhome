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
  } catch {
    if (currentSpace === spaceId) spaceChatError.value = spaceId
  }
}

/** Patch the cached summary (unread, mute, level) without a refetch. */
export function patchSpaceChat(patch: Partial<SystemChatSummary>): void {
  if (!spaceChat.value) return
  spaceChat.value = { ...spaceChat.value, ...patch }
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
