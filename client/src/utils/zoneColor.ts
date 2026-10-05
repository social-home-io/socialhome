/**
 * Zone colours (§23.8.7) — one palette and one guard for every surface
 * that paints a zone (maps, legend, admin list, zone-only chips).
 *
 * ``zone.color`` can arrive from another household over federation.
 * The server validates ``#RRGGBB`` on every path, but the client still
 * refuses anything else before it reaches a style or SVG attribute: a
 * value like ``red;background:url(…)`` would otherwise inject extra
 * declarations through a string ``style``.
 */

/** Deterministic palette for zones without a usable colour. Chosen for
 *  legible contrast against the default OSM tile layer. */
export const ZONE_PALETTE: readonly string[] = [
  '#3b82f6', '#f97316', '#10b981', '#a855f7', '#ec4899',
  '#facc15', '#14b8a6', '#ef4444', '#6366f1', '#84cc16',
]

const _HEX = /^#[0-9a-fA-F]{6}$/

/** ``value`` when it is exactly ``#RRGGBB``, else ``null``. */
export function safeZoneHex(value: unknown): string | null {
  return typeof value === 'string' && _HEX.test(value) ? value : null
}

/** The colour to paint ``zone`` with: its own when valid, otherwise a
 *  stable palette pick hashed from its id (``ZONE_PALETTE[0]`` when
 *  there is no zone at all). */
export function zoneColor(
  zone: { id: string, color: string | null } | undefined,
): string {
  if (!zone) return ZONE_PALETTE[0]
  const own = safeZoneHex(zone.color)
  if (own) return own
  let hash = 0
  for (const ch of zone.id) hash = (hash * 31 + ch.charCodeAt(0)) | 0
  return ZONE_PALETTE[Math.abs(hash) % ZONE_PALETTE.length]
}
