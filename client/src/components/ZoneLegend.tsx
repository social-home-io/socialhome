/**
 * ZoneLegend — colour-coded list of zones rendered under a map, used
 * by both the member-facing :component:`SpaceLocationCard` and the
 * admin :component:`SpaceZonesAdmin` preview pane.
 *
 * The maps themselves draw zones as transparent colored circles
 * with no permanent label (the previous "white square" label boxes
 * stacked up over the map and obscured what was underneath); this
 * legend gives the user the colour ↔ name mapping plus the radius
 * details that used to live in those boxes.
 */
import type { SpaceZone } from '@/types'
import { t } from '@/i18n/i18n'
import { zoneColor } from '@/utils/zoneColor'

function _fmtRadius(m: number): string {
  if (m < 1000) return `${m} m`
  return `${(m / 1000).toFixed(m < 10_000 ? 1 : 0)} km`
}

interface ZoneLegendProps {
  zones: ReadonlyArray<SpaceZone>
  /** Empty-state message shown when ``zones`` is empty. */
  emptyLabel?: string
}

export function ZoneLegend({ zones, emptyLabel }: ZoneLegendProps) {
  if (zones.length === 0) {
    if (!emptyLabel) return null
    return (
      <div class="sh-zone-legend" role="list">
        <span class="sh-zone-legend__empty">{emptyLabel}</span>
      </div>
    )
  }
  return (
    <div class="sh-zone-legend" role="list" aria-label={t('location.zones_legend')}>
      {zones.map((z) => (
        <div key={z.id} class="sh-zone-legend__row" role="listitem">
          <span
            class="sh-zone-legend__swatch"
            style={{ background: zoneColor(z) }}
            aria-hidden="true"
          />
          <div class="sh-zone-legend__text">
            <span class="sh-zone-legend__name">{z.name}</span>
            <span class="sh-zone-legend__coords">
              {z.latitude.toFixed(4)}, {z.longitude.toFixed(4)}
            </span>
          </div>
          <span class="sh-zone-legend__meta">{_fmtRadius(z.radius_m)}</span>
        </div>
      ))}
    </div>
  )
}
