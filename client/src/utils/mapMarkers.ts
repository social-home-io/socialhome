/**
 * Pure marker logic for the person / zone map (Home Assistant
 * 2026.10-style markers).
 *
 * People in the same zone share one bubble anchored at the zone centre;
 * everyone else gets a solo bubble at their own position. On zoom-out,
 * bubbles whose screen positions would overlap merge into one. Nothing
 * here touches Leaflet — the projection is injected so the rules stay
 * unit-testable; ``LocationMap`` does the drawing.
 */
import type { TimetableColor } from '@/types'
import { hashColor } from './tokenColor'

/** Bubbles whose anchors are closer than this (in screen pixels) merge.
 *  Roughly one avatar (40 px) plus the bubble padding and a little air. */
export const MERGE_DISTANCE_PX = 56

/** Avatars a bubble shows before the rest fold into a "+N" chip. */
export const BUBBLE_MAX_AVATARS = 3

/** The minimum a person needs to be placed on the map: a position, or
 *  (zone-only mode) the id of the zone they are in. */
export interface MapPerson {
  id: string
  lat?: number | null
  lon?: number | null
  zone_id?: string | null
}

export interface ZoneLike {
  id: string
  latitude: number
  longitude: number
  radius_m: number
}

export interface MarkerGroup<P extends MapPerson = MapPerson> {
  /** ``zone:<id>`` / ``person:<id>`` / ``merge:<k1>|<k2>…`` — stable for
   *  the same set, so a redraw can keep track of what it drew. */
  key: string
  /** Where the bubble's tail points. */
  lat: number
  lon: number
  members: P[]
  /** Zones folded into this bubble (their members sit at the centre). */
  zoneIds: string[]
  /** Own positions of the members not placed in a zone. */
  points: Array<[number, number]>
  /** True when two or more bubbles were merged on zoom-out. */
  merged: boolean
}

/** A person's frame colour — a theme-following token (``--tt-edge`` of
 *  ``.sh-timetable-c--<token>``), the same for that person everywhere. */
export function personColorToken(userId: string): TimetableColor {
  return hashColor(userId)
}

function _haversineMeters(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const R = 6_371_000
  const toRad = (deg: number) => (deg * Math.PI) / 180
  const dLat = toRad(lat2 - lat1)
  const dLon = toRad(lon2 - lon1)
  const a =
    Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}

/** The nearest zone whose radius contains the point, or ``null`` when the
 *  point is outside every zone. The server never sends a matched zone in
 *  GPS mode (§23.8.7) — the client works it out. */
export function matchZone<Z extends ZoneLike>(
  zones: readonly Z[], lat: number, lon: number,
): Z | null {
  let best: { zone: Z, d: number } | null = null
  for (const z of zones) {
    const d = _haversineMeters(lat, lon, z.latitude, z.longitude)
    if (d <= z.radius_m && (best === null || d < best.d)) best = { zone: z, d }
  }
  return best?.zone ?? null
}

function _finite(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** The zone a person's bubble sits in: the zone they state
 *  (``zone_id``), else the one their point is in — ``null`` for neither.
 *  The overview counts people with this too, so its numbers match the
 *  map's bubbles. */
export function personZone<Z extends ZoneLike>(
  p: MapPerson, zones: readonly Z[],
): Z | null {
  const stated = p.zone_id ? zones.find((z) => z.id === p.zone_id) ?? null : null
  if (stated) return stated
  return _finite(p.lat) && _finite(p.lon) ? matchZone(zones, p.lat, p.lon) : null
}

/** One group per occupied zone (at its centre) plus one solo group per
 *  person outside every zone. A given ``zone_id`` wins over coordinates
 *  (zone-only mode carries no coordinates at all). People with neither a
 *  position nor a known zone are left off the map. */
export function groupByZone<P extends MapPerson>(
  people: readonly P[], zones: readonly ZoneLike[],
): MarkerGroup<P>[] {
  const out: MarkerGroup<P>[] = []
  const byZone = new Map<string, MarkerGroup<P>>()
  for (const p of people) {
    const zone = personZone(p, zones)
    if (zone) {
      let g = byZone.get(zone.id)
      if (!g) {
        g = {
          key: `zone:${zone.id}`, lat: zone.latitude, lon: zone.longitude,
          members: [], zoneIds: [zone.id], points: [], merged: false,
        }
        byZone.set(zone.id, g)
        out.push(g)
      }
      g.members.push(p)
    } else if (_finite(p.lat) && _finite(p.lon)) {
      out.push({
        key: `person:${p.id}`, lat: p.lat, lon: p.lon, members: [p],
        zoneIds: [], points: [[p.lat, p.lon]], merged: false,
      })
    }
  }
  return out
}

/** A bubble's on-screen box: ``w`` × ``h`` px, its bottom edge ``lift``
 *  px above the anchor (bubbles float above the point they mark). */
export interface BubbleExtent {
  w: number
  h: number
  lift: number
}

interface _Cluster<P extends MapPerson> {
  parts: MarkerGroup<P>[]
  /** Anchor in screen px (mean of the parts' anchors). */
  x: number
  y: number
}

function _combine<P extends MapPerson>(parts: MarkerGroup<P>[]): MarkerGroup<P> {
  if (parts.length === 1) return parts[0]
  return {
    key: 'merge:' + parts.map((g) => g.key).sort().join('|'),
    lat: parts.reduce((s, g) => s + g.lat, 0) / parts.length,
    lon: parts.reduce((s, g) => s + g.lon, 0) / parts.length,
    members: parts.flatMap((g) => g.members),
    zoneIds: parts.flatMap((g) => g.zoneIds),
    points: parts.flatMap((g) => g.points),
    merged: true,
  }
}

/** Greedily merge groups whose bubbles would overlap; repeats until
 *  nothing does. A merged group sits at the centroid of the anchors it
 *  absorbed.
 *
 *  Without ``extent`` two groups overlap when their anchors are closer
 *  than ``minDistancePx`` (in both axes). With ``extent`` each bubble is
 *  its real box — width, height and how far it floats above the anchor
 *  (a zone bubble floats higher than a solo one) — and ``minDistancePx``
 *  is the clearance kept between boxes. */
export function mergeOverlapping<P extends MapPerson>(
  groups: readonly MarkerGroup<P>[],
  project: (lat: number, lon: number) => { x: number, y: number },
  minDistancePx: number = MERGE_DISTANCE_PX,
  extent?: (group: MarkerGroup<P>) => BubbleExtent,
): MarkerGroup<P>[] {
  const box = (c: _Cluster<P>) => {
    if (!extent) return { cx: c.x, cy: c.y, w: 0, h: 0 }
    const { w, h, lift } = extent(_combine(c.parts))
    return { cx: c.x, cy: c.y - lift - h / 2, w, h }
  }
  const overlaps = (a: _Cluster<P>, b: _Cluster<P>) => {
    const p = box(a)
    const q = box(b)
    return Math.abs(p.cx - q.cx) < (p.w + q.w) / 2 + minDistancePx
      && Math.abs(p.cy - q.cy) < (p.h + q.h) / 2 + minDistancePx
  }
  let clusters: _Cluster<P>[] = groups.map((g) => {
    const { x, y } = project(g.lat, g.lon)
    return { parts: [g], x, y }
  })
  for (;;) {
    const next: _Cluster<P>[] = []
    for (const c of clusters) {
      const hit = next.find((n) => overlaps(n, c))
      if (hit) {
        const k = hit.parts.length
        const m = c.parts.length
        hit.x = (hit.x * k + c.x * m) / (k + m)
        hit.y = (hit.y * k + c.y * m) / (k + m)
        hit.parts.push(...c.parts)
      } else {
        next.push({ parts: [...c.parts], x: c.x, y: c.y })
      }
    }
    if (next.length === clusters.length) break
    clusters = next
  }
  return clusters.map((c) => _combine(c.parts))
}

/** The avatars a bubble shows and how many fold into the "+N" chip. */
export function bubbleSlots<P>(
  members: readonly P[], max: number = BUBBLE_MAX_AVATARS,
): { shown: P[], overflow: number } {
  return { shown: members.slice(0, max), overflow: Math.max(0, members.length - max) }
}

/** ``[[south, west], [north, east]]`` of a zone's whole circle. */
export function zoneBounds(z: ZoneLike): [[number, number], [number, number]] {
  const dLat = z.radius_m / 111_320
  const dLon = z.radius_m / (111_320 * Math.max(0.01, Math.cos((z.latitude * Math.PI) / 180)))
  return [[z.latitude - dLat, z.longitude - dLon], [z.latitude + dLat, z.longitude + dLon]]
}

/** ``[[south, west], [north, east]]`` around everything a bubble stands
 *  for: each folded zone's whole circle plus each solo member's point —
 *  what a click on a group bubble zooms the map to. */
export function groupBounds(
  group: MarkerGroup, zones: readonly ZoneLike[],
): [[number, number], [number, number]] {
  let s = Infinity, w = Infinity, n = -Infinity, e = -Infinity
  const add = (lat: number, lon: number) => {
    s = Math.min(s, lat); n = Math.max(n, lat)
    w = Math.min(w, lon); e = Math.max(e, lon)
  }
  for (const id of group.zoneIds) {
    const z = zones.find((zz) => zz.id === id)
    if (!z) continue
    const [sw, ne] = zoneBounds(z)
    add(...sw)
    add(...ne)
  }
  for (const [lat, lon] of group.points) add(lat, lon)
  if (!Number.isFinite(s)) add(group.lat, group.lon)
  return [[s, w], [n, e]]
}
