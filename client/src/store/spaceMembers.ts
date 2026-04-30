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
import type { SpaceMemberProfile } from '@/types'

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
