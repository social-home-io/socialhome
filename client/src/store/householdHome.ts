/**
 * This household's own home location, for the local-community badge
 * ("In your area"). Read once from ``GET /api/friends`` (``instance``) into
 * the ``selfLat`` / ``selfLon`` signals the Connections map already keeps
 * live from ``local.home_changed`` frames.
 *
 * ``householdHome`` is ``undefined`` until the first read finished (the
 * badge says nothing rather than "no home" while it loads), ``null`` when
 * the household has no home set, else ``{lat, lon}``.
 */
import { computed, signal } from '@preact/signals'
import { api } from '@/api'
import { selfLat, selfLon } from './connections'
import type { Home } from '@/utils/localArea'

const loaded = signal(false)
let inFlight: Promise<void> | null = null

export const householdHome = computed<Home | null | undefined>(() => {
  if (selfLat.value != null && selfLon.value != null) {
    return { lat: selfLat.value, lon: selfLon.value }
  }
  return loaded.value ? null : undefined
})

/** Load the home once per page (later callers share the first read). A
 *  failed read counts as "no home known" — the badge then asks the
 *  person to share one, and the host decides anyway. */
export function ensureHouseholdHome(): Promise<void> {
  if (loaded.value) return Promise.resolve()
  if (!inFlight) {
    inFlight = Promise.resolve()
      .then(() => api.get<{ instance?: { home_lat?: number | null; home_lon?: number | null } }>(
        '/api/friends',
      ))
      .then((j) => {
        if (j?.instance?.home_lat != null && j.instance.home_lon != null) {
          selfLat.value = j.instance.home_lat
          selfLon.value = j.instance.home_lon
        }
      })
      .catch(() => { /* unknown → no home */ })
      .finally(() => {
        loaded.value = true
        inFlight = null
      })
  }
  return inFlight
}

/** Test seam: forget the read. */
export function resetHouseholdHome(): void {
  loaded.value = false
  inFlight = null
  selfLat.value = null
  selfLon.value = null
}
