/**
 * LocationPostCard — renderer for a one-shot location pin: a feed post
 * with type="location", and a DM message with type="location".
 *
 * Drops a single marker on the existing LocationMap (height 160 for
 * card density — tiles come through the backend proxy) and shows the
 * optional label, the 4dp coordinate line, the coarse accuracy when one
 * was shared, and an "Open in maps" link that pops the pin into
 * OpenStreetMap in a new tab. The link is built from the two numbers
 * only (``mapsHref``) — nothing the sender typed reaches the URL.
 *
 * No live updates — pins are one-shot. The composer's LocationPicker
 * captured the coords at send time; this component is read-only.
 */
import type { LocationData } from '@/types'
import { t } from '@/i18n/i18n'
import { formatCoords, mapsHref } from '@/utils/dmLocation'
import { LocationMap, type LocationMarker } from './LocationMap'

interface LocationPostCardProps {
  location: LocationData & { accuracy_m?: number | null }
}

export function LocationPostCard({ location }: LocationPostCardProps) {
  const { lat, lon, label } = location
  const accuracy = location.accuracy_m ?? null
  const coords = formatCoords({ lat, lon })
  const marker: LocationMarker = {
    id: 'pin',
    lat,
    lon,
    accuracy_m: accuracy,
    label: label ?? coords,
    glyph: '📍',
  }
  const href = mapsHref({ lat, lon })
  return (
    <div class="sh-location-post">
      <LocationMap
        markers={[marker]}
        height={160}
        ariaLabel={label || t('location.shared')}
      />
      <div class="sh-location-post-meta">
        <strong class="sh-location-post-label">
          📍 {label || t('location.shared')}
        </strong>
        <span class="sh-muted sh-location-post-coords">
          {coords}
          {accuracy ? ` · ${t('location.accuracy', { m: String(accuracy) })}` : ''}
        </span>
        {href && (
          <a
            class="sh-link sh-location-post-open"
            href={href}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('location.open_in_maps')} ↗
          </a>
        )}
      </div>
    </div>
  )
}
