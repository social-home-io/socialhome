/**
 * userPreferences store — per-user section visibility toggles (§23.13).
 *
 * Cold-fetches ``/api/me/preferences`` after auth succeeds and listens
 * for ``user.preferences_changed`` WS frames so a settings change on
 * another device is reflected here live (spec §18).
 *
 * Also holds one privacy switch, ``hide_profile_from_followers``.
 *
 * Distinct from :mod:`HouseholdToggles` (admin-set, household-wide)
 * and :mod:`presence` (DND / location-sharing). These flags are owned
 * by the individual user and only sent to that user.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'

export interface UserPreferences {
  user_id: string
  hide_highlights: boolean
  hide_momentum: boolean
  hide_bazaar: boolean
  /** "Show my picture and bio to followers of global spaces" turned off:
   *  followers of a global space this person posts in see the name only. */
  hide_profile_from_followers: boolean
}

const DEFAULT: UserPreferences = {
  user_id: '',
  hide_highlights: false,
  hide_momentum: false,
  hide_bazaar: false,
  hide_profile_from_followers: false,
}

export const userPreferences = signal<UserPreferences>(DEFAULT)

export async function loadUserPreferences(): Promise<void> {
  try {
    userPreferences.value = await api.get('/api/me/preferences') as UserPreferences
  } catch {
    // Not authed yet, or backend unreachable. Defaults stay.
  }
}

export function wireUserPreferencesWs(): void {
  ws.on('user.preferences_changed', (e) => {
    const d = e.data as { user_id: string; changed: Record<string, boolean> }
    if (!d?.user_id || d.user_id !== userPreferences.value.user_id) return
    userPreferences.value = { ...userPreferences.value, ...d.changed }
  })
}
