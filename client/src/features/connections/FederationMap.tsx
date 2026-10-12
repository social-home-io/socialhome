/**
 * FederationMap — Leaflet map of paired households.
 *
 * Each household with a shared home location is a pin built from the same
 * ConnAvatar it has in the list (hash tint, status ring + glyph badge) with
 * its name on a chip underneath; this household is a hearth "You" disc.
 * Auto-fits bounds so the map opens centred on the network.
 *
 * Peers that have no home_lat / home_lon can be listed in a "Not on map"
 * footer below the canvas (the Connections page names them itself).
 *
 * Lazy-loaded by ConnectionsPage so the Leaflet bundle is never
 * downloaded when the user is on the List tab.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import './FederationMap.css'
import {
  connections, selfLat, selfLon, type Connection, type TransportState,
} from '@/store/connections'
import { t } from '@/i18n/i18n'
import { addTileLayer, tileErrorMessage } from '@/utils/mapTiles'
import { haversineKm, bearing8, roundKm } from './_mapMath'
import type { AvatarStatus } from './ConnAvatar'
import { PIN_SIZE, SELF_SIZE, PeerPin, SelfPin, defaultPinStatus, pinHtml } from './MapPin'

const TRANSPORT_KEY: Record<Exclude<TransportState, null>, string> = {
  rtc: 'connections.transport.direct',
  https: 'connections.transport.internet',
  gfs_relay: 'connections.transport.gfs',
}

const STATUS_KEY: Record<AvatarStatus, string> = {
  ok: 'connections.map.lg_ok',
  warn: 'connections.map.lg_warn',
  down: 'connections.map.lg_down',
  pending: 'connections.sub.waiting',
}

/** One-line status for a pin's tooltip and popup. */
export function pinStatusText(peer: Connection, status: AvatarStatus | undefined): string {
  if (status) return t(STATUS_KEY[status])
  return peer.source === 'space_session' ? t('connections.type.space_only') : ''
}

function _line(text: string, cls?: string): HTMLElement {
  const el = document.createElement('div')
  if (cls) el.className = cls
  el.textContent = text
  return el
}

/** Build a peer's popup as DOM nodes — every peer-supplied value goes in
 *  as text, and "Manage" is a real ``<button>`` (an ``<a href="#">``
 *  would resolve against ``<base href>`` under ingress and navigate the
 *  iframe away). */
function _peerPopup(
  peer: Connection,
  status: AvatarStatus | undefined,
  transport: TransportState | null | undefined,
  distance: string | null,
  onManage: ((peer: Connection) => void) | null,
): HTMLElement {
  const root = document.createElement('div')
  root.className = 'sh-fed-popup'
  const name = document.createElement('strong')
  name.textContent = peer.display_name ?? peer.instance_id
  root.append(name)
  const statusText = pinStatusText(peer, status)
  if (statusText) {
    root.append(_line(statusText, `sh-fed-popup__status sh-fed-popup__status--${status ?? 'none'}`))
  }
  if (transport) root.append(_line(t(TRANSPORT_KEY[transport]), 'sh-fed-popup__muted'))
  if (distance) root.append(_line(distance, 'sh-fed-popup__muted'))
  if (onManage) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'sh-fed-popup__manage'
    btn.textContent = t('connections.manage')
    btn.addEventListener('click', () => onManage(peer))
    root.append(btn)
  }
  return root
}

function _selfPopup(): HTMLElement {
  const root = document.createElement('div')
  root.className = 'sh-fed-popup'
  const name = document.createElement('strong')
  name.textContent = t('connections.map.you')
  root.append(name, _line(t('connections.map.you_sub'), 'sh-fed-popup__muted'))
  return root
}

interface Props {
  /** Open the manage dialog for a peer. Omitted (non-admin) → no control. */
  onManage?: (peer: Connection) => void
  /** Which peers get the control; defaults to all. */
  canManage?: (peer: Connection) => boolean
  /** List peers without coords under the canvas. The Connections page
   *  turns this off: its own MapFoot names them beside the legend. */
  showOffMapFooter?: boolean
  /** A pin's ring + badge. Defaults to reachability; the page adds the
   *  admin-only "needs an update" state. */
  statusOf?: (peer: Connection) => AvatarStatus | undefined
  /** The rows to pin — the ones the page lists (so members never see a
   *  pending pin). Defaults to the store's full list. */
  rows?: Connection[]
}

const SELF_KEY = '__self__'

export default function FederationMap({
  onManage, canManage, showOffMapFooter = true, statusOf = defaultPinStatus, rows,
}: Props = {}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const [tileError, setTileError] = useState(false)
  /** Markers by key (``instance_id`` / ``SELF_KEY``) + the pin HTML they
   *  show, so an update touches only what changed. */
  const markersRef = useRef(new Map<string, { marker: L.Marker; html: string }>())
  /** The coordinate set the view was last fitted to — refit only when it
   *  changes, so a transport/status update never resets pan or zoom. */
  const fittedRef = useRef<string | null>(null)

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
    const markers = markersRef.current
    mapRef.current = map

    const ro = new ResizeObserver(() => { map.invalidateSize() })
    ro.observe(containerRef.current!)

    return () => {
      cancelled = true
      ro.disconnect()
      map.remove()
      mapRef.current = null
      layerRef.current = null
      markers.clear()
      fittedRef.current = null
    }
  }, [])

  // Signals read here re-render the component; the effect below then
  // syncs the markers. ``rows`` is what the page shows (members never get
  // pending rows); without it, the store's list.
  const lat = selfLat.value
  const lon = selfLon.value
  const peers = rows ?? connections.value


  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    const markers = markersRef.current
    const seen = new Set<string>()

    const upsert = (
      key: string, latlng: [number, number], html: string,
      opts: { size: number; title: string; zIndexOffset?: number }, popup: HTMLElement,
    ) => {
      seen.add(key)
      const icon = L.divIcon({
        className: 'sh-fed-icon',
        html,
        iconSize: [opts.size, opts.size],
        iconAnchor: [opts.size / 2, opts.size / 2],
        popupAnchor: [0, -opts.size / 2],
      })
      const known = markers.get(key)
      if (!known) {
        const marker = L.marker(latlng, { icon, title: opts.title, zIndexOffset: opts.zIndexOffset ?? 0 })
          .addTo(layer)
        marker.bindPopup(popup)
        markers.set(key, { marker, html })
        return
      }
      const at = known.marker.getLatLng()
      if (at.lat !== latlng[0] || at.lng !== latlng[1]) known.marker.setLatLng(latlng)
      if (known.html !== html) {
        known.marker.setIcon(icon)
        known.html = html
      }
      known.marker.getElement?.()?.setAttribute('title', opts.title)
      known.marker.setPopupContent(popup)
    }

    if (lat != null && lon != null) {
      upsert(SELF_KEY, [lat, lon], pinHtml(<SelfPin />),
        { size: SELF_SIZE, title: t('connections.map.you'), zIndexOffset: 1000 }, _selfPopup())
    }

    for (const peer of peers) {
      if (peer.home_lat == null || peer.home_lon == null) continue
      const transport = (peer as { transport?: TransportState }).transport ?? null
      const status = statusOf(peer)
      const label = [peer.display_name ?? peer.instance_id, pinStatusText(peer, status)]
        .filter(Boolean).join(' · ')
      const distance =
        lat != null && lon != null
          ? `~${roundKm(haversineKm(lat, lon, peer.home_lat, peer.home_lon))} km · `
            + bearing8(lat, lon, peer.home_lat, peer.home_lon)
          : null
      const manage = onManage && (!canManage || canManage(peer)) ? onManage : null
      upsert(peer.instance_id, [peer.home_lat, peer.home_lon],
        pinHtml(<PeerPin peer={peer} status={status} />),
        { size: PIN_SIZE, title: label }, _peerPopup(peer, status, transport, distance, manage))
    }

    for (const [key, { marker }] of [...markers]) {
      if (seen.has(key)) continue
      layer.removeLayer(marker)
      markers.delete(key)
    }

    // Fit only when the set of coordinates changed.
    const coordKey = [...markers.entries()]
      .map(([k, { marker }]) => { const ll = marker.getLatLng(); return `${k}@${ll.lat},${ll.lng}` })
      .sort().join('|')
    if (coordKey === fittedRef.current) return
    fittedRef.current = coordKey
    const all = [...markers.values()].map((m) => m.marker)
    if (all.length === 1) {
      map.setView(all[0].getLatLng(), 10)
    } else if (all.length > 1) {
      map.fitBounds(L.featureGroup(all).getBounds().pad(0.2))
    }
  }, [lat, lon, peers, onManage, canManage, statusOf])

  const offMap = peers.filter(
    (p) => p.home_lat == null || p.home_lon == null,
  )

  return (
    <div class="sh-federation-map" data-testid="sh-federation-map">
      <div class="sh-map-wrap">
        <div ref={containerRef} class="sh-federation-map__canvas" />
        {tileError && (
          <div class="sh-map-error">
            {tileErrorMessage()}
          </div>
        )}
      </div>
      {showOffMapFooter && offMap.length > 0 && (
        <div class="sh-federation-map__footer">
          <h4 class="sh-federation-map__footer-heading">{t('connections.map.not_on_map')}</h4>
          {offMap.map((p) => (
            <div key={p.instance_id} class="sh-federation-map__footer-row">
              <span class="sh-federation-map__footer-dot" aria-hidden="true" />
              <strong>{p.display_name ?? p.instance_id}</strong>
              <span class="sh-muted">{t('connections.map.no_coords')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
