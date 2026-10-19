/**
 * LocalAreaMap — a local community's area drawn on the shared map: the
 * circle (centre + radius) as a zone overlay, the view fitted to the
 * whole circle. A picture by default (no panning, inert); with ``onPick``
 * every tap reports a new centre (the area picker).
 *
 * Tiles come through the backend proxy like every map (``LocationMap`` →
 * ``utils/mapTiles``) — no third-party fetch from the browser.
 */
import { t } from '@/i18n/i18n'
import type { LocalArea } from '@/types'
import type { Home } from '@/utils/localArea'
import { LocationMap, type LocationMarker, type LocationZoneOverlay } from './LocationMap'

const AREA_ZONE_ID = 'local-area'

export function LocalAreaMap({
  area, home, height = 180, onPick, ariaLabel,
}: {
  /** ``null`` in the picker before a centre is chosen. */
  area: LocalArea | null
  /** This household's home, drawn as a 🏠 pin for orientation. */
  home?: Home | null
  height?: number
  onPick?: (lat: number, lon: number) => void
  ariaLabel?: string
}) {
  const zones: LocationZoneOverlay[] = area
    ? [{
      id: AREA_ZONE_ID,
      name: t('local_area.map_zone'),
      latitude: area.lat,
      longitude: area.lon,
      radius_m: area.radius_km * 1000,
      color: null,
      icon: '🏘️',
    }]
    : []
  const markers: LocationMarker[] = home
    ? [{ id: 'home', lat: home.lat, lon: home.lon, label: t('local_area.your_home'), glyph: '🏠' }]
    : []
  return (
    // The wrapper dims the tiles in dark mode, like the Friends map.
    <div class="sh-local-area-map">
    <LocationMap
      markers={markers}
      zones={zones}
      height={height}
      readOnly={!onPick}
      onPick={onPick}
      // Fit the whole circle, not just its centre.
      selectedId={area ? `zone:${AREA_ZONE_ID}` : null}
      zoomToSelection
      areaFit
      emptyLabel=""
      ariaLabel={ariaLabel ?? t('local_area.map_label')}
    />
    </div>
  )
}
