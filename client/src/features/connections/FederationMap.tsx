/**
 * FederationMap — Leaflet map of paired households.
 *
 * Shows the local household as a filled green "You" pin, and each
 * confirmed peer with a white/green circle bearing their first initial
 * plus a small transport badge (⚡ WebRTC / ☁ HTTPS).  Peers on HTTPS
 * fallback get an amber border.  Auto-fits bounds so the map opens
 * centred on the network.
 *
 * Peers that have no home_lat / home_lon are listed in a "Not on map"
 * footer below the canvas so operators know they could ask those peers
 * to set home coordinates.
 *
 * Lazy-loaded by ConnectionsPage so the Leaflet bundle is never
 * downloaded when the user is on the List tab.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import './FederationMap.css'
import { connections, selfLat, selfLon } from '@/store/connections'
import { addTileLayer, TILE_ERROR_MESSAGE } from '@/utils/mapTiles'
import { haversineKm, bearing8, roundKm } from './_mapMath'

function _initial(name: string | undefined): string {
  if (!name) return '?'
  return name.trim()[0]?.toUpperCase() ?? '?'
}

function _selfPinHtml(): string {
  return '<div class="sh-fed-pin sh-fed-pin--self">You</div>'
}

function _peerPinHtml(name: string | undefined, transport: 'rtc' | 'https' | null | undefined): string {
  const modifier = transport === 'https' ? ' sh-fed-pin--https' : ''
  const badge = transport === 'rtc'
    ? '<span class="sh-fed-pin-tx" aria-hidden="true">⚡</span>'
    : transport === 'https'
      ? '<span class="sh-fed-pin-tx" aria-hidden="true">☁</span>'
      : ''
  return (
    `<div class="sh-fed-pin${modifier}">`
    + _initial(name)
    + badge
    + `</div>`
  )
}

function _transportLabel(transport: 'rtc' | 'https' | null | undefined): string {
  if (transport === 'rtc') return '⚡ Direct (WebRTC)'
  if (transport === 'https') return '☁ HTTPS (fallback)'
  return 'Transport unknown'
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => (
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as Record<string, string>)[c]!
  ))
}

export default function FederationMap() {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const [tileError, setTileError] = useState(false)

  // Mount: create the Leaflet map once.
  useEffect(() => {
    if (!containerRef.current) return
    if (mapRef.current) return

    const map = L.map(containerRef.current, {
      zoomControl: true,
      attributionControl: true,
      center: [20, 0],
      zoom: 2,
      scrollWheelZoom: 'center',
    })
    // Tiles come from the backend proxy; the config lands a tick
    // later, so guard against the map being removed meanwhile.
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

    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map

    const ro = new ResizeObserver(() => { map.invalidateSize() })
    ro.observe(containerRef.current!)

    return () => {
      cancelled = true
      ro.disconnect()
      map.remove()
      mapRef.current = null
      layerRef.current = null
    }
  }, [])

  // Rebuild markers whenever signals change.
  // Preact re-renders on signal reads, so reading .value here means this
  // function runs whenever selfLat, selfLon, or connections change.
  const lat = selfLat.value
  const lon = selfLon.value
  const peers = connections.value

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return

    layer.clearLayers()
    const allMarkers: L.Marker[] = []

    // Self pin
    if (lat != null && lon != null) {
      const icon = L.divIcon({
        className: 'sh-fed-icon',
        html: _selfPinHtml(),
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      })
      const selfMarker = L.marker([lat, lon], { icon }).addTo(layer)
      selfMarker.bindPopup(
        '<strong>Your household</strong><br/>'
        + '<span style="color:#6b7280">Local instance</span>',
      )
      allMarkers.push(selfMarker)
    }

    // Peer pins
    for (const peer of peers) {
      if (peer.home_lat == null || peer.home_lon == null) continue
      const transport = (peer as { transport?: 'rtc' | 'https' | null }).transport ?? null
      const icon = L.divIcon({
        className: 'sh-fed-icon',
        html: _peerPinHtml(peer.display_name, transport),
        iconSize: [28, 28],
        iconAnchor: [14, 14],
      })
      const marker = L.marker([peer.home_lat, peer.home_lon], { icon }).addTo(layer)
      const manageId = `sh-map-manage-${peer.instance_id}`
      const distanceRow =
        lat != null && lon != null
          ? `<div style="color:#6b7280">~${roundKm(
              haversineKm(lat, lon, peer.home_lat, peer.home_lon),
            )} km · ${bearing8(lat, lon, peer.home_lat, peer.home_lon)}</div>`
          : ''
      marker.bindPopup(
        `<strong>${escapeHtml(peer.display_name ?? peer.instance_id)}</strong><br/>`
        + `<span>${_transportLabel(transport)}</span><br/>`
        + distanceRow
        + `<a id="${manageId}" href="#" style="font-size:13px">Manage</a>`,
      )
      allMarkers.push(marker)
    }

    // Auto-fit bounds
    if (allMarkers.length === 1) {
      const latlng = allMarkers[0].getLatLng()
      map.setView(latlng, 10)
    } else if (allMarkers.length > 1) {
      const group = L.featureGroup(allMarkers)
      map.fitBounds(group.getBounds().pad(0.2))
    }
  }, [lat, lon, peers])

  const offMap = peers.filter(
    (p) => p.home_lat == null || p.home_lon == null,
  )

  return (
    <div class="sh-federation-map" data-testid="sh-federation-map">
      <div class="sh-map-wrap">
        <div ref={containerRef} class="sh-federation-map__canvas" />
        {tileError && (
          <div class="sh-map-error">
            {TILE_ERROR_MESSAGE}
          </div>
        )}
      </div>
      {offMap.length > 0 && (
        <div class="sh-federation-map__footer">
          <h4 class="sh-federation-map__footer-heading">Not on map</h4>
          {offMap.map((p) => (
            <div key={p.instance_id} class="sh-federation-map__footer-row">
              <span class="sh-federation-map__footer-dot" aria-hidden="true" />
              <strong>{p.display_name ?? p.instance_id}</strong>
              <span class="sh-muted">Paired but no home coordinates yet.</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
