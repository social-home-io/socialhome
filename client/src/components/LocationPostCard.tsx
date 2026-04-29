/**
 * LocationPostCard — feed-card renderer for posts with type="location".
 *
 * Drops a single marker on the existing LocationMap (height 160 for
 * feed-card density) and shows the optional label + a 4dp coord
 * summary. The "Open in OSM" link mirrors LocationMessage so the user
 * can pop the pin into a full map in a new tab.
 *
 * No live updates — location posts are one-shot pins. The composer's
 * LocationPicker captured the coords at post time; this component is
 * read-only.
 */
import type { LocationData } from '@/types'
import { LocationMap, type LocationMarker } from './LocationMap'

interface LocationPostCardProps {
  location: LocationData
}

export function LocationPostCard({ location }: LocationPostCardProps) {
  const { lat, lon, label } = location
  const marker: LocationMarker = {
    id: 'pin',
    lat,
    lon,
    label: label ?? `${lat.toFixed(4)}, ${lon.toFixed(4)}`,
  }
  // High zoom level + zoom 15 in OSM puts the pin in the centre of a
  // street-level view, which matches how WhatsApp / Signal render
  // shared-location messages.
  const osmHref =
    `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}` +
    `#map=15/${lat}/${lon}`
  return (
    <div class="sh-location-post">
      <LocationMap markers={[marker]} height={160} />
      <div class="sh-location-post-meta">
        <strong class="sh-location-post-label">
          📍 {label || 'Shared location'}
        </strong>
        <span class="sh-muted sh-location-post-coords">
          {lat.toFixed(4)}, {lon.toFixed(4)}
        </span>
        <a
          class="sh-link sh-location-post-open"
          href={osmHref}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open in OSM ↗
        </a>
      </div>
    </div>
  )
}
