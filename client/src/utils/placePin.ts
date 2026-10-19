/**
 * A global space's place pin — an optional point its owner chose, listed
 * with the space on its GFSs, for sorting Discover › Global by distance.
 *
 * The distance is worked out HERE, against this household's own home: the
 * viewer's location is never sent anywhere (owner decision 2026-10-19).
 * Joining never checks it.
 *
 * The owner may add a radius HINT ("best for people within 10 km",
 * ``radius_km``): compared here with the same distance — "Within the area" /
 * "about 12 km away · outside the suggested 10 km", and the "Near me"
 * filter. A hint only: nobody checks it at join or follow.
 */
import { formatLocale, t } from '@/i18n/i18n'
import { haversineKm } from '@/features/connections/_mapMath'
import type { DirectoryEntry } from '@/types'
import { radiusLabel, type Home } from './localArea'

/** km from ``home`` to the entry's pin, or ``null`` when either is unknown. */
export function pinDistanceKm(entry: DirectoryEntry, home: Home | null | undefined): number | null {
  if (!home || entry.lat == null || entry.lon == null) return null
  return haversineKm(entry.lat, entry.lon, home.lat, home.lon)
}

/** Nearest pinned spaces first; unpinned ones (or no home) keep their
 *  order after them. */
export function sortByDistance(
  entries: DirectoryEntry[], home: Home | null | undefined,
): DirectoryEntry[] {
  return entries
    .map((e, i) => ({ e, i, d: pinDistanceKm(e, home) }))
    .sort((a, b) => {
      if (a.d !== null && b.d !== null) return a.d - b.d || a.i - b.i
      if (a.d !== null) return -1
      if (b.d !== null) return 1
      return a.i - b.i
    })
    .map((x) => x.e)
}

/** Our home against a pinned space's suggested area. */
export interface HintVerdict {
  kind: 'inside' | 'outside'
  /** km from our home to the pin. */
  km: number
  /** The owner's suggested radius. */
  radiusKm: number
}

/** Is our home within the suggested area of the entry's pin? ``null``
 *  without a pin, a hint or a home. */
export function hintVerdict(entry: DirectoryEntry, home: Home | null | undefined): HintVerdict | null {
  const km = pinDistanceKm(entry, home)
  const radiusKm = entry.radius_km
  if (km === null || radiusKm == null) return null
  return { kind: km <= radiusKm ? 'inside' : 'outside', km, radiusKm }
}

/** "Within the area" / "about 12 km away · outside the suggested 10 km". */
export function hintLabel(v: HintVerdict): string {
  return v.kind === 'inside'
    ? t('place_pin.hint.inside')
    : t('place_pin.hint.outside', { distance: distanceLabel(v.km), radius: radiusLabel(v.radiusKm) })
}

/** Does any entry carry a hint (is "Near me" worth offering)? */
export function hasHint(entries: DirectoryEntry[]): boolean {
  return entries.some((e) => e.radius_km != null && e.lat != null && e.lon != null)
}

/** "Near me": only the spaces whose suggested area our home is in. */
export function nearMe(entries: DirectoryEntry[], home: Home | null | undefined): DirectoryEntry[] {
  return entries.filter((e) => hintVerdict(e, home)?.kind === 'inside')
}

/** "about 3 km away" / "less than 1 km away" — rounded, never precise:
 *  the pin is the owner's, the home is ours, the line is a hint. */
export function distanceLabel(km: number): string {
  if (km < 1) return t('place_pin.distance_under_1')
  const rounded = km < 10 ? Math.round(km) : Math.round(km / 5) * 5
  const dist = new Intl.NumberFormat(formatLocale(), {
    style: 'unit', unit: 'kilometer', unitDisplay: 'short', maximumFractionDigits: 0,
  }).format(rounded)
  return t('place_pin.distance', { distance: dist })
}
