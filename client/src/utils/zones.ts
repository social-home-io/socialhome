/**
 * Zone helpers shared by every surface that lists space zones (§23.8.7):
 * the alphabetical order (as Home Assistant lists its zones, with the
 * household's Home zone on top) and the icon presets the admin picks from.
 */
import type { IconPickerPreset } from '@/components/IconPicker'

/** ``zones`` sorted by name — case- and accent-insensitive, in the
 *  viewer's locale — with a household Home zone (``is_home``) first,
 *  matching the server's order. Space zones carry no ``is_home`` and
 *  sort purely by name. Returns a new array; the input is left untouched. */
export function sortZones<T extends { name: string; is_home?: boolean }>(
  zones: ReadonlyArray<T>,
): T[] {
  return [...zones].sort((a, b) =>
    Number(b.is_home === true) - Number(a.is_home === true)
    || a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
}

/** A zone radius for display: 25 m, 1.2 km, 50 km. */
export function formatRadius(m: number): string {
  if (m < 1000) return `${m} m`
  return `${(m / 1000).toFixed(m < 10_000 ? 1 : 0)} km`
}

const preset = (emoji: string, key: string): IconPickerPreset =>
  ({ emoji, key, label: `zones.icon.${key}` })

/* Labels are looked up dynamically; listed here so i18n:check sees them:
 * t('zones.icon.home') t('zones.icon.work') t('zones.icon.school') t('zones.icon.shop')
 * t('zones.icon.gym') t('zones.icon.park') t('zones.icon.doctor') t('zones.icon.sports')
 * t('zones.icon.music') t('zones.icon.restaurant') t('zones.icon.airport') t('zones.icon.station')
 */
/** Home Assistant–style places. Every emoji passes the server's
 *  single-emoji rule (socialhome/domain/emoji.py) — only U+FE0F marks,
 *  no tag characters. */
export const ZONE_ICON_PRESETS: readonly IconPickerPreset[] = [
  preset('🏠', 'home'),
  preset('🏢', 'work'),
  preset('🏫', 'school'),
  preset('🛒', 'shop'),
  preset('🏋️', 'gym'),
  preset('🌳', 'park'),
  preset('🏥', 'doctor'),
  preset('⚽', 'sports'),
  preset('🎵', 'music'),
  preset('🍽️', 'restaurant'),
  preset('✈️', 'airport'),
  preset('🚉', 'station'),
]
