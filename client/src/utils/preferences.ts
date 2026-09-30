/**
 * User-preference helpers.
 *
 * The backend stores preferences as a JSON string on the ``User`` row;
 * the frontend reads it through this module so string parsing + sane
 * defaults live in one place.
 */
import { api } from '@/api'
import { currentUser } from '@/store/auth'

export type LandingPath = '/' | '/feed' | '/dashboard'

export interface MomentsPreferences {
  /** Per-user max-hops visibility (§Momentum-relay-policy). 1 = only
   *  moments authored by direct peers; 3 (default) = the wire cap, so
   *  every relayed moment is visible. */
  max_hops?: 1 | 2 | 3
}

interface Preferences {
  landing_path?: LandingPath
  followed_space_ids?: string[]
  moments?: MomentsPreferences
  /** ha / haos only — the HA notify service that pushes this user's
   *  Companion app (e.g. ``notify.mobile_app_pascals_iphone``). Empty /
   *  absent disables HA-app push for the user (§25.3). */
  ha_notify_service?: string
  /** First day of the week for calendar-style views. Absent = follow
   *  the browser locale (see ``utils/week.ts``). */
  week_start?: 'mon' | 'sun' | null
  [key: string]: unknown
}

export function getPreferences(): Preferences {
  const raw = (currentUser.value as unknown as {
    preferences_json?: string
  } | null)?.preferences_json
  if (!raw) return {}
  try {
    return JSON.parse(raw) as Preferences
  } catch {
    return {}
  }
}

export function getLandingPath(): LandingPath {
  return getPreferences().landing_path ?? '/'
}

export async function setPreference<K extends keyof Preferences>(
  key: K, value: Preferences[K],
): Promise<void> {
  await api.patch('/api/me', { preferences: { [key]: value } })
  // Optimistically update the cached currentUser so the new preference
  // reflects immediately without a separate ``/api/me`` round-trip.
  const prev = currentUser.value
  if (!prev) return
  const raw = (prev as unknown as {
    preferences_json?: string
  }).preferences_json ?? '{}'
  let parsed: Preferences = {}
  try { parsed = JSON.parse(raw) as Preferences } catch { /* empty */ }
  if (value === null || value === undefined) {
    delete parsed[key as string]
  } else {
    parsed[key as string] = value
  }
  currentUser.value = {
    ...prev,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    preferences_json: JSON.stringify(parsed),
  } as any
}
