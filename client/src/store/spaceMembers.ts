/**
 * Per-space member cache (§4.1.6).
 *
 * Loaded lazily on the first space-feed render; refreshed on
 * ``space.member.profile_updated`` WS frames so every open tab sees
 * display-name / picture changes in real time.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { currentUser } from '@/store/auth'
import type { SpaceMemberProfile } from '@/types'
import { mentionTokenSet } from '@/utils/mentions'

export const spaceMembers = signal<Record<string, Map<string, SpaceMemberProfile>>>({})

const loaded = new Set<string>()

export async function loadSpaceMembers(spaceId: string): Promise<void> {
  if (loaded.has(spaceId)) return
  loaded.add(spaceId)
  try {
    const rows = await api.get(
      `/api/spaces/${spaceId}/members`,
    ) as SpaceMemberProfile[]
    const m = new Map<string, SpaceMemberProfile>()
    for (const r of rows) m.set(r.user_id, r)
    spaceMembers.value = { ...spaceMembers.value, [spaceId]: m }
  } catch {
    loaded.delete(spaceId)
  }
}

export interface SpaceMentionRender {
  mentions: ReadonlySet<string>
  selfMention: string | null
}

const _tokenCache = new WeakMap<Map<string, SpaceMemberProfile>, Set<string>>()

/** What the renderers need to highlight @-mentions in ``spaceId``: the
 *  lower-cased member tokens (empty until the roster loads — nothing is
 *  highlighted rather than guessing) and the viewer's own token. Reads
 *  the signal, so a component calling it re-renders when the roster lands. */
export function spaceMentionRender(spaceId: string | null | undefined): SpaceMentionRender {
  const roster = spaceId ? spaceMembers.value[spaceId] : undefined
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

export function invalidateSpaceMembers(spaceId: string): void {
  loaded.delete(spaceId)
}

ws.on('space.member.profile_updated', (e) => {
  const d = e.data as {
    space_id: string
    user_id: string
    space_display_name: string | null
    picture_hash: string | null
    /** Pre-signed URL from the server so the SPA can drop it straight
     *  into ``<img src>`` without knowing the signing scheme. */
    picture_url: string | null
  }
  if (!d.space_id || !d.user_id) return
  const current = spaceMembers.value[d.space_id]
  if (!current) return
  const prev = current.get(d.user_id)
  if (!prev) return
  const next = new Map(current)
  next.set(d.user_id, {
    ...prev,
    space_display_name: d.space_display_name,
    picture_hash: d.picture_hash,
    picture_url: d.picture_url,
  })
  spaceMembers.value = { ...spaceMembers.value, [d.space_id]: next }
})
