/**
 * LocationMap — reusable Leaflet+OpenStreetMap map widget used by
 * PresencePage, DashboardPage, and per-space SpaceLocationCard
 * (§§23.40 / 23.80).
 *
 * Renders one pin per marker with the user's avatar circle, an
 * accuracy ring when the source GPS carries a ``gps_accuracy_m``, and
 * a tooltip with the display name + zone label. Auto-fits bounds so
 * the caller never has to compute zoom/centre.
 *
 * Leaflet CSS is imported lazily with the first mount so unrelated
 * pages don't pay the style cost. No API key — we point at the
 * OpenStreetMap public tile server and attribute it in-map.
 */
import { useEffect, useRef } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

export interface LocationMarker {
  /** Stable id for the marker (used as the React key). */
  id: string
  lat: number
  lon: number
  /** Optional accuracy radius in metres — drawn as a faint circle. */
  accuracy_m?: number | null
  /** Display-ready name shown in the popup. */
  label: string
  /** Optional avatar URL — falls back to initials on the pin. */
  avatar_url?: string | null
  /** Optional secondary line in the popup (zone / status). */
  sub_label?: string | null
  /** Presence colour dot class — e.g. "home" | "away" | "not_home". */
  state?: string
}

/** Per-space display zone (§23.8.7). Drawn as a labelled circle on
 *  top of the tile layer. Members' GPS pins fall inside zero or one
 *  of these circles; the zone label is shown as the marker's
 *  sub-label by the calling component. */
export interface LocationZoneOverlay {
  id: string
  name: string
  latitude: number
  longitude: number
  radius_m: number
  color: string | null
}

export interface LocationMapProps {
  markers: LocationMarker[]
  /** Per-space zones to draw as labelled circles. Optional — household
   *  surfaces don't pass any. */
  zones?: LocationZoneOverlay[]
  /** Height in CSS pixels. Falls back to 320. */
  height?: number
  /** When true + no markers, shows a muted fallback pane instead of
   *  an empty map — keeps the layout stable on dashboards. */
  emptyLabel?: string
}

/** Deterministic palette colour from a string id, used when a zone
 *  has no explicit ``color``. Chosen for legible contrast against the
 *  default OSM tile layer. */
const _ZONE_PALETTE = [
  '#3b82f6', '#f97316', '#10b981', '#a855f7', '#ec4899',
  '#facc15', '#14b8a6', '#ef4444', '#6366f1', '#84cc16',
]
function _zoneColor(zone: LocationZoneOverlay): string {
  if (zone.color) return zone.color
  let hash = 0
  for (const ch of zone.id) hash = (hash * 31 + ch.charCodeAt(0)) | 0
  return _ZONE_PALETTE[Math.abs(hash) % _ZONE_PALETTE.length]
}

function _initials(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '').join('')
}

function _stateColor(state: string | undefined): string {
  switch (state) {
    case 'home':     return '#22c55e'
    case 'away':     return '#f59e0b'
    case 'not_home': return '#94a3b8'
    default:         return '#6b7280'
  }
}

function _avatarHtml(m: LocationMarker): string {
  const colour = _stateColor(m.state)
  // Inline SVG-ish pin: a circle with border + avatar image fallback.
  // ``dangerouslySetInnerHTML`` isn't an option on Leaflet icons,
  // so we emit HTML here and Leaflet wraps it.
  if (m.avatar_url) {
    return (
      `<div class="sh-map-pin" style="border-color: ${colour}">`
      + `<img src="${m.avatar_url}" alt="" />`
      + `</div>`
    )
  }
  return (
    `<div class="sh-map-pin" style="background: ${colour}">`
    + `<span class="sh-map-pin__initials">${_initials(m.label)}</span>`
    + `</div>`
  )
}

export function LocationMap({
  markers, zones, height = 320, emptyLabel = 'No locations to show.',
}: LocationMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const zoneLayerRef = useRef<L.LayerGroup | null>(null)

  useEffect(() => {
    if (!containerRef.current) return
    if (mapRef.current) return
    const map = L.map(containerRef.current, {
      zoomControl: true,
      attributionControl: true,
      // Sensible defaults — fitBounds overrides these on first paint.
      center: [52.0, 5.0],
      zoom: 4,
      // Keep scroll-wheel off by default — users scroll pages past
      // embedded maps all the time and captured wheel events are
      // disorienting. Ctrl+scroll still zooms.
      scrollWheelZoom: 'center',
    })
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">'
        + 'OpenStreetMap</a> contributors',
    }).addTo(map)
    // Zones go below the marker layer so pins always sit on top of
    // their containing zone overlay.
    zoneLayerRef.current = L.layerGroup().addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map

    // Leaflet measures its container lazily — if the parent was
    // ``display:none`` on mount (e.g. a hidden tab) we need to
    // invalidateSize once the tab becomes visible.
    const ro = new ResizeObserver(() => { map.invalidateSize() })
    ro.observe(containerRef.current)

    return () => {
      ro.disconnect()
      map.remove()
      mapRef.current = null
      layerRef.current = null
      zoneLayerRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    const zoneLayer = zoneLayerRef.current
    if (!map || !zoneLayer) return
    zoneLayer.clearLayers()
    if (!zones) return
    for (const z of zones) {
      const colour = _zoneColor(z)
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.6,
        fillOpacity: 0.10,
        weight: 1.5,
      }).addTo(zoneLayer).bindTooltip(z.name, {
        permanent: true,
        direction: 'center',
        className: 'sh-zone-label',
      })
    }
  }, [zones])

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    layer.clearLayers()

    const usable = markers.filter(
      (m) => Number.isFinite(m.lat) && Number.isFinite(m.lon),
    )

    for (const m of usable) {
      const icon = L.divIcon({
        className: 'sh-map-icon',
        html: _avatarHtml(m),
        iconSize: [36, 36],
        iconAnchor: [18, 36],
      })
      const pin = L.marker([m.lat, m.lon], { icon }).addTo(layer)
      pin.bindPopup(
        `<strong>${m.label}</strong>`
        + (m.sub_label ? `<br /><span>${m.sub_label}</span>` : ''),
      )
      if (m.accuracy_m && m.accuracy_m > 0) {
        L.circle([m.lat, m.lon], {
          radius: m.accuracy_m,
          color: _stateColor(m.state),
          opacity: 0.4,
          fillOpacity: 0.08,
          weight: 1,
        }).addTo(layer)
      }
    }

    if (usable.length === 1) {
      map.setView([usable[0].lat, usable[0].lon], 14)
    } else if (usable.length > 1) {
      const bounds = L.latLngBounds(usable.map((m) => [m.lat, m.lon]))
      map.fitBounds(bounds.pad(0.3), { maxZoom: 15 })
    }
  }, [markers])

  const hasMarkers = markers.some(
    (m) => Number.isFinite(m.lat) && Number.isFinite(m.lon),
  )

  return (
    <div class="sh-location-map" style={`height: ${height}px`}>
      <div ref={containerRef} class="sh-location-map__canvas" />
      {!hasMarkers && (
        <div class="sh-location-map__empty sh-muted">
          {emptyLabel}
        </div>
      )}
    </div>
  )
}
