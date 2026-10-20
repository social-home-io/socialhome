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
import { personLabel } from '@/utils/names'

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

/**
 * Resolve a user_id to its best-available display label.
 *
 * Priority: ``display_name`` → ``username`` → "Someone" — never the
 * raw ``user_id`` (an unknown or not-yet-loaded user is not shown by
 * id). Returns ``''`` for falsy input so callers can short-circuit. Use
 * this everywhere a feed surface needs to render a human-readable name
 * from the bare ``author_user_id`` / ``last_editor_user_id`` /
 * ``locked_by`` id strings the API hands back — the single source of
 * truth.
 */
export function householdDisplayName(userId: string | null | undefined): string {
  if (!userId) return ''
  const u = householdUsers.value.get(userId)
  return personLabel(u?.display_name, u?.username)
}

/**
 * Resolve a user_id to its server-synthesised ``picture_url`` if cached,
 * else ``null``. Pair with :func:`householdDisplayName` when feeding an
 * ``Avatar`` — the component renders a name-derived initial when the
 * picture is missing.
 */
export function householdPictureUrl(userId: string | null | undefined): string | null {
  if (!userId) return null
  return householdUsers.value.get(userId)?.picture_url ?? null
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
