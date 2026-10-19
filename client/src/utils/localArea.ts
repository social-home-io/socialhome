/**
 * Local communities — a public space bound to an area (centre + radius).
 *
 * The host decides who may join, on the joining household's home rounded
 * to ~1 km ({@link JOIN_PRECISION_DP}) that the join carries — with the
 * person's consent, to the host only. These helpers only SHOW that.
 *
 * The centre never leaves the host (people's locations are not shared by
 * design): for a space THIS household hosts, the badge is computed here
 * against our own home ({@link areaVerdict}, the server's own rule); a
 * paired household's community shows only its radius. The server is the
 * rule either way (403 ``OUTSIDE_AREA`` / ``HOME_NOT_SET`` /
 * ``LOCATION_REQUIRED``).
 */
import { formatLocale, t } from '@/i18n/i18n'
import { haversineKm } from '@/features/connections/_mapMath'
import type { LocalArea } from '@/types'
import { coordLabel } from './coordLabel'

/** The radii offered as one-tap presets (km). The server takes any
 *  value within {@link MIN_RADIUS_KM}–{@link MAX_RADIUS_KM}. */
export const RADIUS_PRESETS_KM: readonly number[] = [1, 2, 5, 10, 25]
export const MIN_RADIUS_KM = 0.5
export const MAX_RADIUS_KM = 50
/** A new area starts here: a neighbourhood. */
export const DEFAULT_RADIUS_KM = 2

/** Where a household's home lies relative to an area — what the badge
 *  says, for a space THIS household hosts (a paired household's community
 *  shows no verdict: its host decides when a join arrives). ``unknown``
 *  while this household's own home is still loading; ``edge`` when the
 *  ~1 km cell around the home straddles the rim — an admin decides. */
export type AreaVerdict = 'inside' | 'outside' | 'edge' | 'no_home' | 'unknown'

export interface Home { lat: number; lon: number }

/** Decimals a home is rounded to before it rides a join to a local
 *  community's host — about 1.1 km, never a house or a street. The same
 *  constant as the server (``JOIN_LOCATION_PRECISION_DP``). */
export const JOIN_PRECISION_DP = 2
const HALF_CELL_DEG = 0.5 * 10 ** -JOIN_PRECISION_DP

/** Round to 4 decimal places (about 11 m) — the precision the server
 *  stores; it truncates again. */
export function round4(v: number): number {
  return Math.round(v * 1e4) / 1e4
}

/** ``home`` rounded to the join precision — what the host decides on. */
export function coarsePoint(home: Home): Home {
  const f = 10 ** JOIN_PRECISION_DP
  return { lat: Math.round(home.lat * f) / f, lon: Math.round(home.lon * f) / f }
}

/** Distance in km from the area's centre to ``home``. */
export function distanceKm(area: LocalArea, home: Home): number {
  return haversineKm(area.lat, area.lon, home.lat, home.lon)
}

/**
 * The host's decision for ``home`` (mirrors the server's
 * ``join_verdict``): the home is somewhere in the ~1 km cell around its
 * rounded point, so the whole cell inside → ``inside``, wholly outside →
 * ``outside``, straddling the rim → ``edge``.
 */
export function joinVerdict(area: LocalArea, home: Home): 'inside' | 'outside' | 'edge' {
  const p = coarsePoint(home)
  const h = HALF_CELL_DEG
  const latLo = Math.max(p.lat - h, -90)
  const latHi = Math.min(p.lat + h, 90)
  const corners = [
    [latLo, p.lon - h], [latLo, p.lon + h], [latHi, p.lon - h], [latHi, p.lon + h],
  ]
  const farthest = Math.max(...corners.map(([la, lo]) => haversineKm(area.lat, area.lon, la, lo)))
  if (farthest <= area.radius_km) return 'inside'
  const dLon = ((((area.lon - p.lon) + 180) % 360) + 360) % 360 - 180
  const nearLat = Math.min(Math.max(area.lat, latLo), latHi)
  const nearLon = p.lon + Math.min(Math.max(dLon, -h), h)
  return haversineKm(area.lat, area.lon, nearLat, nearLon) > area.radius_km ? 'outside' : 'edge'
}

/**
 * The badge for a local community THIS household hosts, seen from our
 * own ``home`` (``undefined`` = still loading, ``null`` = none set) —
 * the same answer the server's check gives.
 */
export function areaVerdict(area: LocalArea, home: Home | null | undefined): AreaVerdict {
  if (home === undefined) return 'unknown'
  if (home === null) return 'no_home'
  return joinVerdict(area, home)
}

/** "5 km" / "0.5 km" in the UI locale. */
export function radiusLabel(km: number): string {
  return new Intl.NumberFormat(formatLocale(), {
    style: 'unit', unit: 'kilometer', unitDisplay: 'short', maximumFractionDigits: 1,
  }).format(km)
}

/** "Within 5 km of 47.3769° N, 8.5417° E". */
export function areaLabel(area: LocalArea): string {
  return t('local_area.within', {
    radius: radiusLabel(area.radius_km),
    place: coordLabel(area.lat, area.lon),
  })
}

/** Short form for a list row: "Within 5 km". */
export function areaShortLabel(radiusKm: number): string {
  return t('local_area.within_short', { radius: radiusLabel(radiusKm) })
}

/** A paired household's community, which shows no centre:
 *  "Local community · within 5 km of the host's chosen centre". */
export function peerAreaLabel(radiusKm: number): string {
  return t('local_area.within_host_centre', { radius: radiusLabel(radiusKm) })
}

/** Is ``km`` a radius the server takes? */
export function radiusInBounds(km: number): boolean {
  return Number.isFinite(km) && km >= MIN_RADIUS_KM && km <= MAX_RADIUS_KM
}
