/**
 * Federation-compatibility store — protocol-version skew across paired peers.
 *
 * Mirrors ``GET /api/admin/federation/compat`` (admin-only): our own
 * ``proto_version`` plus a row per confirmed peer with its version, last
 * reachability, and the list of features it's missing relative to us. Drives
 * the Federation (Connections) page compatibility badges (per-household
 * "up to date ✓" / "N behind" / "version unknown", the "N households behind"
 * header summary) and the per-peer "Re-check" affordance.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

export interface CompatPeer {
  instance_id:       string
  display_name:      string
  proto_version:     number
  status:            string
  last_reachable_at: string | null
  capabilities_known: boolean
  /** English labels of the features this peer's version lacks. */
  lacking_features:  string[]
  /** Stable slugs of the same features, same order (translated by
   *  ``utils/capabilityLabels``). */
  lacking_feature_keys?: string[]
}

export const compatPeers   = signal<CompatPeer[]>([])
export const compatOurs    = signal(0)
export const compatLoading  = signal(false)
export const compatError    = signal<string | null>(null)

export async function loadFederationCompat(): Promise<void> {
  compatLoading.value = true
  compatError.value = null
  try {
    const data = await api.get('/api/admin/federation/compat') as {
      ours: number
      peers: CompatPeer[]
    }
    compatOurs.value = data.ours ?? 0
    compatPeers.value = data.peers ?? []
  } catch (err: unknown) {
    compatError.value = (err as Error).message ?? 'Could not load federation compatibility.'
    compatPeers.value = []
  } finally {
    compatLoading.value = false
  }
}

/**
 * Count peers genuinely behind our protocol version. A peer whose
 * capabilities we haven't learned yet (``capabilities_known === false``) is
 * NOT counted — its reported ``proto_version`` is a placeholder until the
 * first ``INSTANCE_CAPABILITIES_UPDATED`` handshake, so counting it would
 * raise a phantom "peer behind" nag on every first contact.
 */
export function peersBehindCount(): number {
  return compatPeers.value.filter(
    (p) => p.capabilities_known && p.proto_version < compatOurs.value,
  ).length
}

/**
 * Feature slug (the backend ``CAPABILITY_FEATURE_KEYS`` entry for
 * ``MIN_FOR_INSTANCE_RESYNC``) — a peer that still *lacks* this can't honor a
 * resync request, so the "Re-check" affordance is hidden for it.
 *
 * Matched by slug only, with no fallback to the English label: the SPA is
 * served by the backend it talks to, so the compat endpoint always sends
 * ``lacking_feature_keys``. The slug, unlike the label, never changes.
 */
export const RESYNC_FEATURE = 'household_resync'

/**
 * True iff we can ask this peer to re-advertise (it understands the v_19
 * ``INSTANCE_RESYNC_REQUEST``). Derived from the compat data — no version
 * number duplicated in the client.
 */
export function peerSupportsResync(p: CompatPeer): boolean {
  return p.capabilities_known && !(p.lacking_feature_keys ?? []).includes(RESYNC_FEATURE)
}

/**
 * Ask a peer to re-advertise its capabilities (``scope: "capabilities"``).
 * Fires ``POST /api/admin/federation/resync``; the peer's fresh
 * ``proto_version`` arrives asynchronously via its reply, so callers refresh
 * the panel a moment later rather than expecting an immediate change.
 */
export async function resyncPeerCapabilities(instanceId: string): Promise<void> {
  await api.post('/api/admin/federation/resync', {
    instance_id: instanceId,
    scope: 'capabilities',
  })
}

/** Test helper — reset signals without hitting the API. */
export function _resetFederationCompatForTest(): void {
  compatPeers.value   = []
  compatOurs.value    = 0
  compatLoading.value = false
  compatError.value   = null
}
