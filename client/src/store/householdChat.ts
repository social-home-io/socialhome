/**
 * Household chat store — the feed's Chat tab.
 *
 * The household chat is a hidden system group conversation of every
 * local user (``system_scope = 'household'``). Its summary comes from
 * ``GET /api/household/chat``; its messages, reads, reactions, mute and
 * level use the ordinary ``/api/conversations/{id}/...`` routes. The DM
 * inbox and the Chats badge never see it (``store/dms.ts`` skips frames
 * that carry a ``system_scope``).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

export interface HouseholdChatSummary {
  /** ``false`` while the household turned the chat off. */
  enabled: boolean
  conversation_id: string | null
  unread: number
  notif_level: 'all' | 'mentions' | null
  muted_until: string | null
}

/** The latest summary; ``null`` before the first answer. */
export const householdChat = signal<HouseholdChatSummary | null>(null)
/** The last summary fetch failed (the Chat tab shows a retry). */
export const householdChatError = signal(false)

/** Fetch the summary. A failure keeps the previous summary and flags
 *  :data:`householdChatError`. */
export async function loadHouseholdChat(): Promise<void> {
  try {
    const body = await api.get('/api/household/chat') as Partial<HouseholdChatSummary> | null
    householdChat.value = {
      enabled: body?.enabled === true,
      conversation_id: body?.conversation_id ?? null,
      unread: Math.max(0, body?.unread ?? 0),
      notif_level: body?.notif_level === 'mentions'
        ? 'mentions'
        : body?.notif_level === 'all' ? 'all' : null,
      muted_until: body?.muted_until ?? null,
    }
    householdChatError.value = false
  } catch {
    householdChatError.value = true
  }
}

/** Patch the cached summary (unread, mute, level) without a refetch. */
export function patchHouseholdChat(patch: Partial<HouseholdChatSummary>): void {
  if (!householdChat.value) return
  householdChat.value = { ...householdChat.value, ...patch }
}

/** The ``data`` of a ``dm.*`` frame for the household chat — frames for
 *  a system chat carry ``system_scope`` (and ``space_id`` for a space). */
export function isHouseholdChatFrame(data: unknown): boolean {
  return (data as { system_scope?: unknown } | null)?.system_scope === 'household'
}
