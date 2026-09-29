/**
 * dmLocation — the SPA side of a ``type='location'`` DM message.
 *
 * The message's ``content`` is a JSON object (see
 * ``socialhome/domain/dm_location.py``):
 *
 *   {"lat": 52.3702, "lon": 4.8952, "label": "Marina" | null,
 *    "accuracy_m": 50 | null}
 *
 * The server is the authority — it rounds the coordinates to 4 dp and
 * buckets the accuracy before the row is stored or federated. This
 * module rounds on the way out too (so the preview matches what will be
 * stored) and parses defensively on the way in: content from another
 * household is untrusted, so anything that isn't a finite in-range
 * coordinate renders as "couldn't show this location" instead of a
 * broken map.
 */

export interface DmLocation {
  lat: number
  lon: number
  label: string | null
  accuracy_m: number | null
}

/** Mirrors ``DM_LOCATION_LABEL_MAX`` on the server. */
export const DM_LOCATION_LABEL_MAX = 80

const round4 = (v: number): number => Math.round(v * 1e4) / 1e4

function finiteInRange(v: unknown, limit: number): v is number {
  return typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= limit
}

/** Parse a location message's ``content``; ``null`` when malformed. */
export function parseDmLocation(content: string | null | undefined): DmLocation | null {
  if (!content) return null
  let data: unknown
  try {
    data = JSON.parse(content)
  } catch {
    return null
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const o = data as Record<string, unknown>
  if (!finiteInRange(o.lat, 90) || !finiteInRange(o.lon, 180)) return null
  const label =
    typeof o.label === 'string' && o.label.trim()
      ? o.label.trim().slice(0, DM_LOCATION_LABEL_MAX)
      : null
  const accuracy =
    typeof o.accuracy_m === 'number' && Number.isFinite(o.accuracy_m) && o.accuracy_m > 0
      ? o.accuracy_m
      : null
  return { lat: round4(o.lat), lon: round4(o.lon), label, accuracy_m: accuracy }
}

/** Serialise a picked location into message ``content`` (4 dp). */
export function toDmLocationContent(loc: {
  lat: number
  lon: number
  label?: string | null
  accuracy_m?: number | null
}): string {
  const label = loc.label?.trim() ? loc.label.trim().slice(0, DM_LOCATION_LABEL_MAX) : null
  const accuracy =
    typeof loc.accuracy_m === 'number' && Number.isFinite(loc.accuracy_m) && loc.accuracy_m >= 0
      ? Math.round(loc.accuracy_m)
      : null
  return JSON.stringify({
    lat: round4(loc.lat),
    lon: round4(loc.lon),
    label,
    accuracy_m: accuracy,
  })
}

/** ``"52.3702, 4.8952"`` — the human-readable coordinate line. */
export function formatCoords(loc: { lat: number; lon: number }): string {
  return `${loc.lat.toFixed(4)}, ${loc.lon.toFixed(4)}`
}

/**
 * "Open in maps" link for a pin. Built only from the two numbers
 * (formatted with ``toFixed``, so nothing from the sender's label or
 * any other string reaches the URL) and pointed at OpenStreetMap at
 * street-level zoom. Returns ``null`` for a non-finite / out-of-range
 * coordinate so the caller renders no link rather than a bad one.
 */
export function mapsHref(loc: { lat: number; lon: number }): string | null {
  if (!finiteInRange(loc.lat, 90) || !finiteInRange(loc.lon, 180)) return null
  const lat = loc.lat.toFixed(4)
  const lon = loc.lon.toFixed(4)
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=16/${lat}/${lon}`
}
