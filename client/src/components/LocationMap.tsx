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
 * pages don't pay the style cost. Tiles come from the backend proxy
 * via the shared ``addTileLayer`` helper — never a hard-coded URL.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { addTileLayer, TILE_ERROR_MESSAGE } from '@/utils/mapTiles'

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
  const [tileError, setTileError] = useState(false)

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
    // Tiles arrive a tick later (the config is fetched once per page
    // load); ``cancelled`` keeps us off a map that unmounted first.
    let cancelled = false
    // Two failure surfaces: the config fetch (promise rejection) and
    // the tiles themselves (``onError``, after a streak — a 401 past
    // the signature TTL, a 429, or a dead upstream). Both land in the
    // same "Map unavailable" state; a silent grey square is the bug
    // this proxy exists to kill.
    void addTileLayer(
      map,
      () => cancelled,
      () => { if (!cancelled) setTileError(true) },
    ).catch(() => {
      if (!cancelled) setTileError(true)
    })
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
      cancelled = true
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
    const circles: L.Circle[] = []
    for (const z of zones) {
      const colour = _zoneColor(z)
      // Filled, slightly more opaque so the zone reads at a glance
      // without a permanent white label box on top of it. The name
      // appears in a hover tooltip and in the colour-coded legend
      // the consumer renders below the map.
      const c = L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.85,
        fillColor: colour,
        // Light fill so two overlapping zones don't compound into a
        // muddy grey blob. The crisp coloured border is what
        // distinguishes one zone from the next on the map; the
        // legend underneath spells out the names.
        fillOpacity: 0.12,
        weight: 2,
      })
        .addTo(zoneLayer)
        .bindTooltip(z.name, {
          // Desktop hover surface — invisible to touch devices,
          // so we also bind a popup below for tap discoverability.
          direction: 'top',
          offset: [0, -4],
          className: 'sh-zone-tooltip',
        })
        .bindPopup(
          // Tap-on-touch surface. Plain text keeps the popup small;
          // the legend below the map has the full detail row.
          `<strong>${z.name}</strong>`,
          { closeButton: false, autoPan: false },
        )
      circles.push(c)
    }
    // When there are no marker pins to drive the viewport, fit the
    // map to the zones so the user actually sees the configured
    // zones instead of a globe-zoom of "somewhere in Europe".
    if (circles.length > 0 && map.getZoom() < 6) {
      const bounds = circles
        .map((c) => c.getBounds())
        .reduce((acc, b) => acc.extend(b), L.latLngBounds(
          circles[0].getBounds().getSouthWest(),
          circles[0].getBounds().getNorthEast(),
        ))
      map.fitBounds(bounds.pad(0.4), { maxZoom: 15 })
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
      {tileError ? (
        <div class="sh-map-error">
          {TILE_ERROR_MESSAGE}
        </div>
      ) : !hasMarkers && (
        <div class="sh-location-map__empty sh-muted">
          {emptyLabel}
        </div>
      )}
    </div>
  )
}
