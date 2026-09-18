/**
 * Connections store — paired federation instances and their
 * reachability, driven by `connection.reachable` and
 * `connection.unreachable` WS frames (§23.71).
 *
 * Also holds the local household's home coords (updated via
 * `local.home_changed` WS frames) and patches peer coords on
 * `peer.home_changed` WS frames so the federation map stays live.
 *
 * NetworkMap + ConnectionsPage both read :data:`connections`.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

/** Active federation transport for a peer.
 *
 *  ``gfs_relay`` marks a household that is only reachable through the
 *  connection server relay (e.g. a peer seated from an invite link) — it
 *  has no direct HTTPS inbox of ours to post to, so labelling it
 *  ``https`` would be a lie. */
export type TransportState = 'rtc' | 'https' | 'gfs_relay' | null

export interface Connection {
  instance_id:   string
  /** The displayed name — local alias when set, else the peer's
   *  advertised display_name. The backend already resolves this. */
  display_name: string
  /** What the peer actually advertises via the federation handshake.
   *  Used by ``ConnectionDetail`` to render "They advertise themselves
   *  as <X>" alongside the editable alias input. */
  federated_display_name?: string
  local_alias?: string | null
  status?: string
  paired_at?: string | null
  source?: string
  reachable:     boolean
  inbox_url?: string
  intro_relay_enabled?: boolean
  unreachable_since?: string | null
  /** Last moment an outbound envelope to this peer was accepted
   *  (``remote_instances.last_reachable_at``); ``null`` when it has never
   *  been reached. Distinct from ``last_seen_at`` (inbound activity) —
   *  ``ConnectionDetail`` renders it as "Last connected". */
  last_reachable_at?: string | null
  /** Undelivered federation envelopes still queued for this peer. A peer
   *  that has been offline for weeks piles these up; the count is what
   *  separates a blip from a household that has been gone for months.
   *  Absent on older API responses. */
  queued_envelopes?: number
  /** Federation envelopes permanently given up on for this peer (terminal
   *  ``failed``): a PERMANENT rejection or an exhausted retry budget.
   *  These are NOT retried — reported apart from ``queued_envelopes`` so
   *  the UI never renders dropped messages as still-in-flight. Absent on
   *  older API responses. */
  dropped_envelopes?: number
  transport?: TransportState
  /** Monotonic federation protocol version the peer last advertised via
   *  INSTANCE_CAPABILITIES_UPDATED. Defaults to 1 server-side when the peer
   *  has never announced capabilities. */
  proto_version?: number
  last_seen_at?: string | null
  home_lat?: number | null
  home_lon?: number | null
  /** Whether this household shares its home location with the peer.
   *  Defaults to true in the UI when the backend omits the field
   *  (older peer that hasn't sent the field yet). */
  share_home?: boolean
}

export const connections = signal<Connection[]>([])

/** Own household's home coordinates (updated live via local.home_changed). */
export const selfLat = signal<number | null>(null)
/** Own household's home coordinates (updated live via local.home_changed). */
export const selfLon = signal<number | null>(null)

function upsert(patch: Partial<Connection> & { instance_id: string }): void {
  const existing = connections.value.find((c) => c.instance_id === patch.instance_id)
  if (existing) {
    connections.value = connections.value.map((c) =>
      c.instance_id === patch.instance_id ? { ...c, ...patch } : c,
    )
  } else {
    connections.value = [
      ...connections.value,
      { reachable: true, ...patch } as Connection,
    ]
  }
}

export function wireConnectionsWs(): void {
  ws.on('connection.reachable', (e) => {
    const d = e.data as unknown as { instance_id: string, last_seen_at?: string }
    if (!d?.instance_id) return
    upsert({
      instance_id:  d.instance_id,
      reachable:    true,
      last_seen_at: d.last_seen_at ?? null,
    })
  })
  ws.on('connection.unreachable', (e) => {
    const d = e.data as unknown as { instance_id: string }
    if (!d?.instance_id) return
    upsert({ instance_id: d.instance_id, reachable: false })
  })
  ws.on('connection.added', (e) => {
    const d = e.data as unknown as Connection
    if (!d?.instance_id) return
    upsert(d)
  })
  ws.on('connection.removed', (e) => {
    const d = e.data as unknown as { instance_id: string }
    if (!d?.instance_id) return
    connections.value = connections.value.filter((c) => c.instance_id !== d.instance_id)
  })
  ws.on('local.home_changed', (e) => {
    const d = e.data as unknown as { latitude: number; longitude: number }
    if (d?.latitude == null || d?.longitude == null) return
    selfLat.value = d.latitude
    selfLon.value = d.longitude
  })
  ws.on('peer.home_changed', (e) => {
    const d = e.data as unknown as {
      instance_id: string
      latitude: number
      longitude: number
    }
    if (!d?.instance_id || d?.latitude == null || d?.longitude == null) return
    connections.value = connections.value.map((c) =>
      c.instance_id === d.instance_id
        ? { ...c, home_lat: d.latitude, home_lon: d.longitude }
        : c,
    )
  })
}
