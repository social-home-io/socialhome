/**
 * Presence store — household member presence driven by
 * `presence.updated` WS frames (§22).
 *
 * Carries two orthogonal signals:
 *   • Physical presence (``state`` / ``zone_name`` / GPS) from
 *     ``presence.updated``.
 *   • Session presence (``is_online`` / ``is_idle`` / ``last_seen_at``)
 *     from ``user.online`` / ``user.idle`` / ``user.offline``.
 *
 * The store is keyed by ``username`` for `presence.updated` lookups
 * but session-presence frames carry ``user_id`` only — we maintain a
 * secondary ``user_id → username`` index so both kinds of frame can
 * patch the same row.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface PresenceEntry {
  username:      string
  user_id?:      string
  state:         string
  zone_name?:    string | null
  latitude?:     number | null
  longitude?:    number | null
  is_online?:    boolean
  is_idle?:      boolean
  last_seen_at?: string | null
}

export const presence = signal<Record<string, PresenceEntry>>({})

function patchByUserId(
  user_id: string | undefined,
  patch: Partial<PresenceEntry>,
): void {
  if (!user_id) return
  const map = presence.value
  // Locate the entry by user_id. Bootstrapped /api/presence rows carry
  // both username + user_id, so this lookup hits in steady state.
  for (const username of Object.keys(map)) {
    if (map[username].user_id === user_id) {
      presence.value = { ...map, [username]: { ...map[username], ...patch } }
      return
    }
  }
  // Unknown user_id — drop the frame. The next /api/presence fetch (or
  // the user's first physical-presence update) will repopulate the row.
}

export function wirePresenceWs(): void {
  ws.on('presence.updated', (e) => {
    const data = e.data as unknown as PresenceEntry
    if (!data?.username) return
    presence.value = {
      ...presence.value,
      [data.username]: { ...presence.value[data.username], ...data },
    }
  })
  ws.on('user.online', (e) => {
    const data = e.data as { user_id?: string }
    patchByUserId(data.user_id, { is_online: true, is_idle: false })
  })
  ws.on('user.idle', (e) => {
    const data = e.data as { user_id?: string }
    patchByUserId(data.user_id, { is_online: true, is_idle: true })
  })
  ws.on('user.offline', (e) => {
    const data = e.data as { user_id?: string; last_seen_at?: string | null }
    patchByUserId(data.user_id, {
      is_online:    false,
      is_idle:      false,
      last_seen_at: data.last_seen_at ?? null,
    })
  })
}
