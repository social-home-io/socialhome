/**
 * Pure view-model helpers for the Connections page — no rendering, no I/O.
 *
 * Everything the page decides about a row (which group it lands in, its
 * status, its avatar tint, which hero variant to show) lives here so it can
 * be unit-tested without mounting a component.
 */
import type { Connection } from '@/store/connections'
import type { CompatPeer } from '@/store/federationCompat'
import type { GfsConnection } from '@/types'
import { normaliseTimestamp } from '@/utils/relativeTime'

// ── Avatar tint ─────────────────────────────────────────────────────────

export type Tint = 'moss' | 'hearth' | 'honey' | 'ink'
export const TINTS: readonly Tint[] = ['moss', 'hearth', 'honey', 'ink']

/** FNV-1a (32-bit) + a murmur3 finaliser. Stable across sessions and
 *  browsers, so a household keeps its colour everywhere it appears (cards,
 *  constellation, map). The finaliser matters: FNV's low bits depend only
 *  on the low bits of each character, and the tint is ``hash % 4``. */
export function hashString(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  h ^= h >>> 16
  h = Math.imul(h, 0x85ebca6b)
  h ^= h >>> 13
  h = Math.imul(h, 0xc2b2ae35)
  h ^= h >>> 16
  return h >>> 0
}

/** ``TINTS[hash(instance_id) % 4]`` — never the household's federated
 *  theme, which a peer controls and could make unreadable. */
export function tintFor(instanceId: string): Tint {
  return TINTS[hashString(instanceId) % TINTS.length]
}

/** First letter of a display name, upper-cased; ``?`` when empty. */
export function initialOf(name: string | null | undefined): string {
  const ch = Array.from((name ?? '').trim())[0]
  return ch ? ch.toLocaleUpperCase() : '?'
}

// ── Row grouping + status ───────────────────────────────────────────────

/** A household seated purely by an invite-link redeem (§D2b). It is a
 *  confirmed peer for the shared space and nothing else. */
export function isSpaceOnly(c: Connection): boolean {
  return c.source === 'space_session'
}

/** A real, confirmed household relationship (the only rows Manage opens). */
export function isHousehold(c: Connection): boolean {
  return c.status === 'confirmed' && !isSpaceOnly(c)
}

export type PeerStatus = 'ok' | 'warn' | 'down'

/** Protocol versions this peer trails ours by. ``0`` when unknown: a peer
 *  whose capabilities we never learned reports a placeholder version. */
export function versionsBehind(peer: CompatPeer | undefined, ours: number): number {
  if (!peer || !peer.capabilities_known || ours <= 0) return 0
  return Math.max(0, ours - peer.proto_version)
}

/** The one status rule for a confirmed household — used by the list, the
 *  map pins and the Manage sheet so they can never disagree. The live
 *  ``reachable`` flag (patched by WS frames) wins; ``unreachable_since``
 *  only decides when a row carries no flag. ``behind`` is admin-only
 *  version data (0 for members). */
export function householdStatus(
  c: { reachable?: boolean; unreachable_since?: string | null },
  behind: number,
): PeerStatus {
  const down = c.reachable === false || (c.reachable === undefined && c.unreachable_since != null)
  if (down) return 'down'
  return behind > 0 ? 'warn' : 'ok'
}

export interface Household {
  conn: Connection
  status: PeerStatus
  /** Versions behind ours — always 0 for members (admin-only data). */
  behind: number
}

function recency(c: Connection): number {
  const iso = c.last_reachable_at ?? c.last_seen_at ?? c.paired_at
  if (!iso) return 0
  const ms = Date.parse(normaliseTimestamp(iso))
  return Number.isNaN(ms) ? 0 : ms
}

const RANK: Record<PeerStatus, number> = { down: 0, warn: 1, ok: 2 }

/** Confirmed households with status, problems first (down, then warn),
 *  then most recently connected. Version data only counts for admins — a
 *  member sees a behind-on-updates household as healthy. */
export function buildHouseholds(
  conns: Connection[],
  compat: Map<string, CompatPeer>,
  ours: number,
  isAdmin: boolean,
): Household[] {
  return conns
    .filter(isHousehold)
    .map((conn) => {
      const behind = isAdmin ? versionsBehind(compat.get(conn.instance_id), ours) : 0
      return { conn, status: householdStatus(conn, behind), behind }
    })
    .sort((a, b) => RANK[a.status] - RANK[b.status] || recency(b.conn) - recency(a.conn))
}

/** Rows that are neither confirmed households nor space-only: outbound or
 *  inbound QR pairings still in progress. */
export function pendingRows(conns: Connection[]): Connection[] {
  return conns.filter((c) => c.status !== 'confirmed' && !isSpaceOnly(c))
}

export type PendingKind = 'code' | 'request' | 'approval'

/** What a pending row waits on. Older servers send no ``pending_kind``;
 *  a pairing code is the neutral reading (it never claims who scanned). */
export function pendingKind(c: Connection): PendingKind {
  return c.pending_kind ?? 'code'
}

export function spaceOnlyRows(conns: Connection[]): Connection[] {
  return conns.filter(isSpaceOnly)
}

// ── Hero variant ────────────────────────────────────────────────────────

export type HeroVariant = 'loading' | 'error' | 'empty' | 'problem' | 'healthy'

export function heroVariant(opts: {
  loading: boolean
  error: boolean
  households: Household[]
}): HeroVariant {
  if (opts.error) return 'error'
  if (opts.loading) return 'loading'
  if (opts.households.length === 0) return 'empty'
  return opts.households.some((h) => h.status !== 'ok') ? 'problem' : 'healthy'
}

// ── Time ────────────────────────────────────────────────────────────────

const DAY = 24 * 60 * 60 * 1000

/** "since …" label: a time today, a weekday within the last week
 *  (``short`` → "Mon", ``long`` → "Monday"), else a short date. */
export function formatSince(
  iso: string | null | undefined,
  style: 'short' | 'long',
  locale: string,
  now: number = Date.now(),
): string | null {
  if (!iso) return null
  const at = Date.parse(normaliseTimestamp(iso))
  if (Number.isNaN(at)) return null
  const then = new Date(at)
  if (then.toDateString() === new Date(now).toDateString()) {
    return then.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' })
  }
  if (now - at < 6 * DAY) {
    return then.toLocaleDateString(locale, { weekday: style })
  }
  const sameYear = then.getFullYear() === new Date(now).getFullYear()
  return then.toLocaleDateString(locale, {
    month: 'short',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
  })
}

// ── GFS ─────────────────────────────────────────────────────────────────

/** Re-pair-class auth failures: the GFS no longer recognizes this home.
 *  Current GFS builds close with ``auth-failed``; older ones still send
 *  the two split reasons. */
const GFS_REPAIR_ERRORS = new Set(['auth-failed', 'unknown-instance', 'bad-signature'])

export interface GfsLiveState {
  /** Avatar ring + badge. */
  status: 'ok' | 'pending' | 'down'
  /** i18n key for the human label. ``last_error`` is GFS-controlled and is
   *  only ever surfaced through these mapped strings, never verbatim. */
  labelKey: string
}

/** Pairing status + live WS health → what the GFS card shows. A stored
 *  ``active`` row whose socket is down must NOT read as connected. */
export function gfsLiveState(gfs: GfsConnection): GfsLiveState {
  if (gfs.status === 'pending') return { status: 'pending', labelKey: 'gfs.status_pending' }
  if (gfs.status === 'suspended') return { status: 'down', labelKey: 'gfs.status_suspended' }
  if (gfs.connected) return { status: 'ok', labelKey: 'gfs.status_connected' }
  if (gfs.last_error && GFS_REPAIR_ERRORS.has(gfs.last_error)) {
    return { status: 'down', labelKey: 'gfs.status_repair_needed' }
  }
  if (gfs.last_error === 'ts-skew') return { status: 'down', labelKey: 'gfs.status_clock_skew' }
  return { status: 'pending', labelKey: 'gfs.status_reconnecting' }
}

/** ``host[:port]`` of a URL, or the input unchanged when it isn't one. */
export function hostOf(url: string | null | undefined): string {
  if (!url) return ''
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** ``scheme://host[:port]`` of a URL — what "Copy address" hands out. */
export function originOf(url: string): string {
  try {
    return new URL(url).origin
  } catch {
    return url
  }
}
