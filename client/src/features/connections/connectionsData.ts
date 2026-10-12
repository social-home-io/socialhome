/**
 * Connections page data — the page-local stores and their loaders.
 *
 * The paired-household list itself lives in ``@/store/connections`` (the
 * dashboard map reads it too). Everything here is only needed by this
 * page: GFS connections, the admin pair-request inbox, the admin roster
 * for the member footnote, and the admin external-address state.
 *
 * Loaders record failure in an ``*Error`` flag instead of swallowing it to
 * ``[]`` — an empty list and "couldn't load" must render differently.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { selfLat, selfLon } from '@/store/connections'
import type { GfsConnection, User } from '@/types'

export interface AutoPairRequest {
  request_id: string
  from_a_id: string
  from_a_display: string
  via_b_id: string
  via_b_display: string
  ts: string
  received_at: string
}

export const gfsConnections = signal<GfsConnection[]>([])
export const gfsLoading = signal(true)
export const gfsError = signal(false)

export async function loadGfsConnections(): Promise<void> {
  gfsLoading.value = true
  try {
    gfsConnections.value = await api.get('/api/gfs/connections') as GfsConnection[]
    gfsError.value = false
  } catch {
    gfsError.value = true
  } finally {
    gfsLoading.value = false
  }
}

export const autoPairRequests = signal<AutoPairRequest[]>([])

/** Admin-only inbox. A failure keeps the last list: the inbox is an
 *  addition to the page, not something a banner should be raised for. */
export async function loadAutoPairRequests(): Promise<void> {
  try {
    autoPairRequests.value = await api.get('/api/pairing/auto-pair-requests') as AutoPairRequest[]
  } catch {
    /* keep the last known inbox */
  }
}

export function dropAutoPairRequest(requestId: string): void {
  autoPairRequests.value = autoPairRequests.value.filter((r) => r.request_id !== requestId)
}

/** Display names of this household's admins, for "Ask Dave" in the
 *  member footnote. ``null`` = unknown (not loaded / failed) → the
 *  footnote falls back to "Ask an admin". */
export const householdAdmins = signal<string[] | null>(null)

export async function loadHouseholdAdmins(): Promise<void> {
  try {
    const users = await api.get('/api/users') as User[]
    householdAdmins.value = users
      .filter((u) => u.is_admin)
      .map((u) => u.display_name || u.username)
  } catch {
    householdAdmins.value = null
  }
}

/** Own household's home coordinates, for the map's "You" pin. Non-fatal:
 *  the pin also arrives via a ``local.home_changed`` WS frame. */
export async function loadSelfHome(): Promise<void> {
  try {
    const j = await api.get('/api/friends') as {
      instance?: { home_lat?: number | null; home_lon?: number | null }
    }
    if (j.instance?.home_lat != null && j.instance?.home_lon != null) {
      selfLat.value = j.instance.home_lat
      selfLon.value = j.instance.home_lon
    }
  } catch {
    /* the List view doesn't need it */
  }
}

// ── External address (admin) ────────────────────────────────────────────

export interface ExternalUrlState {
  loaded: boolean
  /** What the admin typed (``null`` = nothing stored). */
  stored: string | null
  /** What peers actually reach us at, whatever the source. */
  effective: string | null
  /** ``auto`` when the effective value comes from this setup's config. */
  source: string | null
  /** ``false`` under the Home Assistant App, where the integration owns
   *  the value and the server refuses a manual one. */
  manualSupported: boolean
}

export const externalUrl = signal<ExternalUrlState>({
  loaded: false, stored: null, effective: null, source: null, manualSupported: true,
})

export async function loadExternalUrl(): Promise<void> {
  try {
    const r = await api.get('/api/admin/federation/external-url') as {
      base: string | null; effective: string | null; source: string | null
      manual_supported?: boolean
    }
    externalUrl.value = {
      loaded: true,
      stored: r.base,
      effective: r.effective,
      source: r.source,
      // Older servers omit the flag; they honour a manual base everywhere.
      manualSupported: r.manual_supported !== false,
    }
  } catch {
    externalUrl.value = { ...externalUrl.value, loaded: true }
  }
}

export async function saveExternalUrl(base: string | null): Promise<{
  base: string | null; changed: boolean; peers_notified: number
}> {
  const r = await api.put('/api/admin/federation/external-url', { base }) as {
    base: string | null; changed: boolean; peers_notified: number
  }
  await loadExternalUrl()
  return r
}
