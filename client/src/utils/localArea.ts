/**
 * Local communities — a public space bound to an area (centre + radius).
 *
 * The host decides who may join: a household whose home, as it shares it
 * with the host, lies inside the circle. These helpers only SHOW that —
 * the "Within 5 km of …" line and the "In your area" badge computed here
 * against this household's own home. The badge never leaves the browser;
 * the server is the rule (403 ``OUTSIDE_AREA`` / ``HOME_NOT_SHARED``).
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
 *  says. ``unknown`` while this household's own home is still loading. */
export type AreaVerdict = 'inside' | 'outside' | 'no_home' | 'not_shared' | 'unknown'

export interface Home { lat: number; lon: number }

/** Round to 4 decimal places (about 11 m) — the precision the server
 *  stores; it truncates again. */
export function round4(v: number): number {
  return Math.round(v * 1e4) / 1e4
}

/** Distance in km from the area's centre to ``home``. */
export function distanceKm(area: LocalArea, home: Home): number {
  return haversineKm(area.lat, area.lon, home.lat, home.lon)
}

/**
 * The badge for ``area`` seen from a household whose home is ``home``
 * (``undefined`` = still loading, ``null`` = none set). ``sharedWithHost``
 * is ``false`` when this household does not share its home with the
 * space's host — the host then can't check it, whatever the distance;
 * pass ``true`` (or omit) for a space this household hosts.
 *
 * Outside wins over "not shared": telling someone to share their home
 * with a host whose area they are outside of anyway would send them on a
 * pointless errand.
 */
export function areaVerdict(
  area: LocalArea,
  home: Home | null | undefined,
  sharedWithHost = true,
): AreaVerdict {
  if (home === undefined) return 'unknown'
  if (home === null) return 'no_home'
  if (distanceKm(area, home) > area.radius_km) return 'outside'
  return sharedWithHost ? 'inside' : 'not_shared'
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
export function areaShortLabel(area: LocalArea): string {
  return t('local_area.within_short', { radius: radiusLabel(area.radius_km) })
}

/** Is ``km`` a radius the server takes? */
export function radiusInBounds(km: number): boolean {
  return Number.isFinite(km) && km >= MIN_RADIUS_KM && km <= MAX_RADIUS_KM
}
