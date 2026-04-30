/**
 * Household-level user cache (§23).
 *
 * Mirrors the :mod:`spaceMembers` store but at household scope: maps
 * ``user_id`` → the :class:`User` row returned by ``GET /api/users``,
 * including the server-synthesised ``picture_url``. Any component that
 * renders an avatar outside a space context can look up the URL here.
 *
 * Refreshes on ``user.profile_updated`` WS frames so the bell stays
 * live when another tab changes the avatar.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import type { User } from '@/types'

export const householdUsers = signal<Map<string, User>>(new Map())

let loaded = false

export async function loadHouseholdUsers(): Promise<void> {
  if (loaded) return
  loaded = true
  try {
    const rows = await api.get('/api/users') as User[]
    const m = new Map<string, User>()
    for (const r of rows) m.set(r.user_id, r)
    householdUsers.value = m
  } catch {
    loaded = false
  }
}

export function invalidateHouseholdUsers(): void {
  loaded = false
}

ws.on('user.profile_updated', (e) => {
  const d = e.data as {
    user_id: string
    username: string
    display_name: string
    bio: string | null
    picture_hash: string | null
    /** Pre-signed URL — sent by the server so the SPA can drop it
     *  straight into ``<img src>`` without knowing the signing
     *  scheme. ``null`` when no picture is set. */
    picture_url: string | null
  }
  if (!d.user_id) return
  const prev = householdUsers.value.get(d.user_id)
  if (!prev) return
  const next = new Map(householdUsers.value)
  next.set(d.user_id, {
    ...prev,
    display_name: d.display_name,
    bio: d.bio,
    picture_hash: d.picture_hash,
    picture_url: d.picture_url,
  })
  householdUsers.value = next
})
