/**
 * HeroConstellation geometry (HANDOFF §5) — pure, so it can be tested.
 *
 * Canvas 248×184, "You" at (124, 92). Households sit on r = 72; with a GFS
 * tile in the lower-right wedge the households share the 290° arc from
 * 64°, otherwise a full circle from −150°. At most 6 slots: past that, 5
 * households plus a "+N" node.
 */
export const CX = 124
export const CY = 92
export const RADIUS = 72
export const MAX_SLOTS = 6
export const GFS_TILE = { x: 210, y: 150 }

export interface Placed<T> {
  item: T
  x: number
  y: number
}

export interface ConstellationLayout<T> {
  nodes: Placed<T>[]
  /** "+N" overflow node, N = total − shown. */
  more: { count: number; x: number; y: number } | null
}

/** SVG degrees (0 = east, clockwise) of slot ``i`` of ``n``. */
export function slotAngle(i: number, n: number, gfs: boolean): number {
  if (gfs) return 64 + ((i + 0.5) * 290) / n
  return -150 + (i * 360) / Math.max(n, 1)
}

function at(deg: number): { x: number; y: number } {
  const a = (deg * Math.PI) / 180
  return {
    x: Math.round((CX + RADIUS * Math.cos(a)) * 10) / 10,
    y: Math.round((CY + RADIUS * Math.sin(a)) * 10) / 10,
  }
}

/** Place ``items`` (already ordered problems-first by the caller). */
export function layoutConstellation<T>(items: T[], gfs: boolean): ConstellationLayout<T> {
  const overflow = items.length > MAX_SLOTS
  const shown = overflow ? items.slice(0, MAX_SLOTS - 1) : items
  const slots = shown.length + (overflow ? 1 : 0)
  const nodes = shown.map((item, i) => ({ item, ...at(slotAngle(i, slots, gfs)) }))
  const more = overflow
    ? { count: items.length - shown.length, ...at(slotAngle(slots - 1, slots, gfs)) }
    : null
  return { nodes, more }
}

/** Three dashed "+" slots for the first-run constellation. */
export function emptySlots(): { x: number; y: number }[] {
  return [-150, -30, 90].map(at)
}

/** Quadratic "string sag" link from the centre: control point = midpoint
 *  + (0, ``sag``). */
export function linkPath(x: number, y: number, sag = 8): string {
  const mx = Math.round(((CX + x) / 2) * 10) / 10
  const my = Math.round(((CY + y) / 2 + sag) * 10) / 10
  return `M${CX},${CY} Q${mx},${my} ${x},${y}`
}
