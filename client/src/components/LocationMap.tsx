/**
 * LocationMap — reusable Leaflet+OpenStreetMap map widget used by
 * PresencePage, DashboardPage, FriendsPage, the per-space
 * SpaceLocationCard (§§23.40 / 23.80) and the location post / picker.
 *
 * People are drawn Home Assistant 2026.10-style: rounded-square photo
 * (or initials) avatars framed in each person's own colour. Everyone in
 * the same zone shares ONE bubble pinned to the zone centre (up to three
 * avatars plus a "+N" chip); people outside every zone get a solo bubble
 * whose tail points at their position. Bubbles that would overlap on
 * zoom-out merge, and clicking a group bubble zooms in to fit it. Zones
 * are a coloured radius plus a centre marker carrying the zone's icon.
 * The grouping rules live in ``utils/mapMarkers`` (pure, unit-tested).
 *
 * A marker with a ``glyph`` is a *place* (a shared location, not a
 * person): it never groups or merges and keeps its name popup and
 * accuracy ring.
 *
 * Leaflet CSS is imported lazily with the first mount so unrelated
 * pages don't pay the style cost. Tiles come from the backend proxy
 * via the shared ``addTileLayer`` helper — never a hard-coded URL.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { addTileLayer, tileErrorMessage } from '@/utils/mapTiles'
import { escapeHtml } from '@/utils/html'
import { zoneColor } from '@/utils/zoneColor'
import { colorClass } from '@/utils/tokenColor'
import {
  MERGE_DISTANCE_PX,
  bubbleSlots,
  groupBounds,
  groupByZone,
  mergeOverlapping,
  personColorToken,
  type MarkerGroup,
} from '@/utils/mapMarkers'
import { t } from '@/i18n/i18n'

export interface LocationMarker {
  /** Stable id for the marker — the id ``onSelect`` reports. */
  id: string
  /** Position. Omitted / null in zone-only mode, where ``zone_id``
   *  places the person at their zone's centre instead. */
  lat?: number | null
  lon?: number | null
  /** Zone-only mode: the zone this person is in (no coordinates). */
  zone_id?: string | null
  /** Picks the person's frame colour; falls back to ``id``. */
  user_id?: string
  /** Optional accuracy radius in metres — drawn as a faint circle for
   *  a selected person (always for a place). */
  accuracy_m?: number | null
  /** Display-ready name (marker title, caption, place popup). */
  label: string
  /** Optional avatar URL — falls back to initials. */
  avatar_url?: string | null
  /** Optional secondary line (zone / status). */
  sub_label?: string | null
  /** Presence state — kept for callers; the colour is per person now. */
  state?: string
  /** A place pin (a shared location, not a person): draw this glyph
   *  instead of an avatar. */
  glyph?: string
}

/** Per-space display zone (§23.8.7). Drawn as a coloured radius with a
 *  centre marker showing the zone's icon. */
export interface LocationZoneOverlay {
  id: string
  name: string
  latitude: number
  longitude: number
  radius_m: number
  color: string | null
  /** One emoji shown at the zone centre (first letter of the name when
   *  null). */
  icon?: string | null
}

export interface LocationMapProps {
  markers: LocationMarker[]
  /** Per-space zones to draw. Optional — household surfaces don't pass
   *  any. */
  zones?: LocationZoneOverlay[]
  /** Height in CSS pixels. Falls back to 320. */
  height?: number
  /** When true + no markers, shows a muted fallback pane instead of
   *  an empty map — keeps the layout stable on dashboards. */
  emptyLabel?: string
  /** Pick mode: every click / tap on the map reports its coordinates,
   *  and the empty-state pane is suppressed (an empty map *is* the
   *  prompt). Used by :class:`LocationPicker`'s "pick on the map". */
  onPick?: (lat: number, lon: number) => void
  /** Accessible name for the map region. */
  ariaLabel?: string
  /** Selected person id, or ``zone:<id>``. Leave undefined to let the
   *  map keep its own selection. */
  selectedId?: string | null
  /** Called with a person id, ``zone:<id>``, or null (empty map click). */
  onSelect?: (id: string | null) => void
}

function _initials(name: string): string {
  return name.trim().split(/\s+/).slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '').join('')
}

function _finite(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function _isPlace(m: LocationMarker): boolean {
  return m.glyph != null
}

function _title(m: LocationMarker): string {
  return m.sub_label ? `${m.label} · ${m.sub_label}` : m.label
}

/** One rounded-square avatar framed in the person's colour token. The
 *  initials sit underneath the photo so a failed image falls back to
 *  them once the ``error`` listener drops the ``<img>``. */
function _avatarHtml(m: LocationMarker, selected: boolean): string {
  const tone = m.glyph != null ? 'sh-map-avatar--place' : colorClass(personColorToken(m.user_id ?? m.id))
  const face = m.glyph ?? _initials(m.label)
  const img = m.avatar_url
    ? `<img src="${escapeHtml(m.avatar_url)}" alt="" loading="lazy" />`
    : ''
  return (
    `<span class="sh-map-avatar ${tone}${selected ? ' is-selected' : ''}">`
    + `<span class="sh-map-avatar__initials">${escapeHtml(face)}</span>${img}`
    + `</span>`
  )
}

type _Kind = 'zone' | 'solo' | 'group'

function _kind(g: MarkerGroup<LocationMarker>): _Kind {
  if (g.merged) return 'group'
  return g.zoneIds.length > 0 ? 'zone' : 'solo'
}

function _bubbleHtml(
  g: MarkerGroup<LocationMarker>, kind: _Kind, selected: string | null,
): string {
  const { shown, overflow } = bubbleSlots(g.members)
  const avatars = shown.map((m) => _avatarHtml(m, m.id === selected)).join('')
  const more = overflow > 0
    ? `<span class="sh-map-bubble__more">+${overflow}</span>`
    : ''
  const solo = g.members.length === 1 ? g.members[0] : null
  const caption = solo && solo.id === selected
    ? `<span class="sh-map-bubble__caption">${escapeHtml(_title(solo))}</span>`
    : ''
  const anySelected = g.members.some((m) => m.id === selected)
  return (
    `<div class="sh-map-bubble sh-map-bubble--${kind}${anySelected ? ' is-selected' : ''}">`
    + caption
    + `<span class="sh-map-bubble__body" aria-hidden="true">${avatars}${more}</span>`
    + `<span class="sh-map-bubble__tail" aria-hidden="true"></span>`
    + (kind === 'zone' ? '' : '<span class="sh-map-bubble__dot" aria-hidden="true"></span>')
    + `</div>`
  )
}

function _zoneFace(z: LocationZoneOverlay): string {
  const icon = z.icon?.trim()
  if (icon) return icon
  return Array.from(z.name.trim())[0]?.toUpperCase() ?? '?'
}

function _zoneHtml(z: LocationZoneOverlay, selected: boolean): string {
  // ``zoneColor`` only ever returns a ``#RRGGBB`` value, so it is safe
  // inside a style attribute.
  return (
    `<div class="sh-map-zone${selected ? ' is-selected' : ''}" `
    + `style="--zone-colour: ${zoneColor(z)}" aria-hidden="true">`
    + `<span class="sh-map-zone__icon">${escapeHtml(_zoneFace(z))}</span>`
    + `</div>`
  )
}

/** Leaflet puts ``title`` on the icon element; screen readers get the
 *  same text as the accessible name, and a photo that fails to load
 *  (expired signature) drops out so the initials show. */
function _finishIcon(marker: L.Marker, title: string): void {
  const el = marker.getElement?.()
  if (!el) return
  el.setAttribute('aria-label', title)
  for (const img of Array.from(el.querySelectorAll('img'))) {
    img.addEventListener('error', () => { img.remove() }, { once: true })
  }
}

/** A zero-size divIcon: the bubble positions itself from the anchor
 *  point in CSS, so variable-width bubbles need no size maths here. */
function _icon(html: string, extra = ''): L.DivIcon {
  return L.divIcon({
    className: `sh-map-icon${extra}`,
    html,
    iconSize: [0, 0],
    iconAnchor: [0, 0],
  })
}

export function LocationMap({
  markers, zones, height = 320, emptyLabel,
  onPick, ariaLabel, selectedId, onSelect,
}: LocationMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const placeLayerRef = useRef<L.LayerGroup | null>(null)
  const zoneLayerRef = useRef<L.LayerGroup | null>(null)
  const [tileError, setTileError] = useState(false)
  // Uncontrolled fallback: callers that don't pass ``selectedId`` still
  // get click-to-show-name.
  const [ownSelected, setOwnSelected] = useState<string | null>(null)
  const selected = selectedId !== undefined ? selectedId : ownSelected
  // Latest props in refs so the one-time Leaflet bindings (map click,
  // zoomend) always see current values without re-creating the map.
  const onPickRef = useRef(onPick)
  onPickRef.current = onPick
  const markersRef = useRef(markers)
  markersRef.current = markers
  const zonesRef = useRef(zones)
  zonesRef.current = zones
  const selectedRef = useRef(selected)
  selectedRef.current = selected
  const selectRef = useRef((_id: string | null) => {})
  selectRef.current = (id: string | null) => {
    if (selectedId === undefined) setOwnSelected(id)
    onSelect?.(id)
  }
  const drawPeopleRef = useRef(() => {})

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
    // Zones go below the people layer so bubbles always sit on top of
    // their zone overlay.
    zoneLayerRef.current = L.layerGroup().addTo(map)
    placeLayerRef.current = L.layerGroup().addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    map.on('click', (e: L.LeafletMouseEvent) => {
      if (onPickRef.current) {
        onPickRef.current(e.latlng.lat, e.latlng.lng)
      } else if (selectedRef.current !== null) {
        // Clicking the empty map deselects. Marker clicks don't bubble
        // here (Leaflet markers stop propagation).
        selectRef.current(null)
      }
    })
    // Screen distances change with the zoom: re-run the merge.
    map.on('zoomend', () => { drawPeopleRef.current() })

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
      placeLayerRef.current = null
      zoneLayerRef.current = null
    }
  }, [])

  // Zones: coloured radius + centre marker with the zone's icon.
  const fittedZonesRef = useRef<LocationZoneOverlay[] | undefined>(undefined)
  useEffect(() => {
    const map = mapRef.current
    const zoneLayer = zoneLayerRef.current
    if (!map || !zoneLayer) return
    zoneLayer.clearLayers()
    if (!zones) return
    const circles: L.Circle[] = []
    for (const z of zones) {
      const colour = zoneColor(z)
      const c = L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.85,
        fillColor: colour,
        // Light fill so two overlapping zones don't compound into a
        // muddy grey blob. The crisp coloured border is what
        // distinguishes one zone from the next on the map.
        fillOpacity: 0.12,
        weight: 2,
      })
        .addTo(zoneLayer)
        // Leaflet mounts string tooltip / popup content via
        // ``innerHTML``, and a zone name can come from another
        // household's admin over federation — escape both.
        .bindTooltip(escapeHtml(z.name), {
          // Desktop hover surface — invisible to touch devices,
          // so we also bind a popup below for tap discoverability.
          direction: 'top',
          offset: [0, -4],
          className: 'sh-zone-tooltip',
        })
        .bindPopup(
          `<strong>${escapeHtml(z.name)}</strong>`,
          { closeButton: false, autoPan: false },
        )
      circles.push(c)
      const title = t('location.map.zone_marker', { name: z.name })
      const isSel = selected === `zone:${z.id}`
      const centre = L.marker([z.latitude, z.longitude], {
        icon: _icon(_zoneHtml(z, isSel), ' sh-map-icon--zone'),
        title,
        keyboard: true,
        zIndexOffset: isSel ? 500 : 0,
      })
        .on('click', () => { selectRef.current(`zone:${z.id}`) })
        .addTo(zoneLayer)
      _finishIcon(centre, title)
    }
    // When there are no person bubbles to drive the viewport, fit the
    // map to the zones (once per zone list, not on every selection) so
    // the user actually sees them instead of a globe-zoom of Europe.
    if (fittedZonesRef.current !== zones && circles.length > 0 && map.getZoom() < 6) {
      const bounds = circles
        .map((c) => c.getBounds())
        .reduce((acc, b) => acc.extend(b), L.latLngBounds(
          circles[0].getBounds().getSouthWest(),
          circles[0].getBounds().getNorthEast(),
        ))
      map.fitBounds(bounds.pad(0.4), { maxZoom: 15 })
    }
    fittedZonesRef.current = zones
  }, [zones, selected])

  // People: group by zone, merge what would overlap at this zoom, draw.
  drawPeopleRef.current = () => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    layer.clearLayers()
    const zoneList = zonesRef.current ?? []
    const sel = selectedRef.current
    const people = markersRef.current.filter((m) => !_isPlace(m))
    const groups = mergeOverlapping(
      groupByZone(people, zoneList),
      (lat, lon) => map.latLngToContainerPoint([lat, lon]),
      MERGE_DISTANCE_PX,
    )
    for (const g of groups) {
      const kind = _kind(g)
      const single = kind !== 'group' && g.members.length === 1 ? g.members[0] : null
      const zone = kind === 'zone' ? zoneList.find((z) => z.id === g.zoneIds[0]) : undefined
      const names = g.members.map((m) => m.label).join(', ')
      const title = single
        ? _title(single)
        : zone ? `${names} · ${zone.name}` : names
      const isSel = g.members.some((m) => m.id === sel)
      const marker = L.marker([g.lat, g.lon], {
        icon: _icon(_bubbleHtml(g, kind, sel), ` sh-map-icon--${kind}`),
        title,
        keyboard: true,
        // Bubbles float over zone markers; the selected one on top.
        zIndexOffset: isSel ? 2000 : 1000,
      })
        .on('click', () => {
          if (single) {
            selectRef.current(single.id)
            return
          }
          map.fitBounds(L.latLngBounds(groupBounds(g, zoneList)), {
            padding: [48, 48], maxZoom: 18,
          })
        })
        .addTo(layer)
      _finishIcon(marker, title)
      // Accuracy ring only for the selected person drawn at their own
      // point — a zone bubble isn't at the person's position.
      if (single && single.id === sel && kind === 'solo'
          && single.accuracy_m && single.accuracy_m > 0) {
        L.circle([g.lat, g.lon], {
          radius: single.accuracy_m,
          className: `sh-map-accuracy ${colorClass(personColorToken(single.user_id ?? single.id))}`,
          opacity: 0.5,
          fillOpacity: 0.1,
          weight: 1,
          interactive: false,
        }).addTo(layer)
      }
    }
  }
  useEffect(() => { drawPeopleRef.current() }, [markers, zones, selected])

  // Places + the viewport: only when the positions actually change, so a
  // parent re-render (a selection, a poll returning the same pins) never
  // yanks the user's zoom or closes an open place popup.
  const positionsKey = JSON.stringify(
    markers.map((m) => [m.id, m.lat ?? null, m.lon ?? null, m.zone_id ?? null, m.glyph ?? null]),
  )
  useEffect(() => {
    const map = mapRef.current
    const placeLayer = placeLayerRef.current
    if (!map || !placeLayer) return
    placeLayer.clearLayers()
    const current = markersRef.current
    for (const m of current) {
      if (!_isPlace(m) || !_finite(m.lat) || !_finite(m.lon)) continue
      const html = (
        '<div class="sh-map-bubble sh-map-bubble--solo sh-map-bubble--place">'
        + `<span class="sh-map-bubble__body" aria-hidden="true">${_avatarHtml(m, false)}</span>`
        + '<span class="sh-map-bubble__tail" aria-hidden="true"></span>'
        + '<span class="sh-map-bubble__dot" aria-hidden="true"></span></div>'
      )
      const pin = L.marker([m.lat, m.lon], {
        icon: _icon(html, ' sh-map-icon--solo'),
        title: m.label,
        keyboard: true,
      }).addTo(placeLayer)
      pin.bindPopup(
        `<strong>${escapeHtml(m.label)}</strong>`
        + (m.sub_label ? `<br /><span>${escapeHtml(m.sub_label)}</span>` : ''),
      )
      _finishIcon(pin, m.label)
      if (m.accuracy_m && m.accuracy_m > 0) {
        L.circle([m.lat, m.lon], {
          radius: m.accuracy_m,
          className: 'sh-map-accuracy',
          opacity: 0.4,
          fillOpacity: 0.08,
          weight: 1,
          interactive: false,
        }).addTo(placeLayer)
      }
    }

    // Fit to where the bubbles point: zone centres for zoned people,
    // own points for everyone else.
    const anchors: Array<[number, number]> = [
      ...groupByZone(current.filter((m) => !_isPlace(m)), zonesRef.current ?? [])
        .map((g): [number, number] => [g.lat, g.lon]),
      ...current.filter((m) => _isPlace(m) && _finite(m.lat) && _finite(m.lon))
        .map((m): [number, number] => [m.lat as number, m.lon as number]),
    ]
    if (anchors.length === 1) {
      // In pick mode keep the user's zoom when they move the pin — a
      // re-centre on every tap must not yank the map out from under them.
      const zoom = onPickRef.current ? Math.max(map.getZoom(), 14) : 14
      map.setView(anchors[0], zoom)
    } else if (anchors.length > 1) {
      map.fitBounds(L.latLngBounds(anchors).pad(0.3), { maxZoom: 15 })
    }
    // Draw after the view settles so the merge sees the final zoom.
    drawPeopleRef.current()
  }, [positionsKey])

  const hasMarkers = markers.some((m) => _isPlace(m) && _finite(m.lat) && _finite(m.lon))
    || groupByZone(markers.filter((m) => !_isPlace(m)), zones ?? []).length > 0

  return (
    <div
      class={'sh-location-map' + (onPick ? ' sh-location-map--pick' : '')}
      style={`height: ${height}px`}
      role={ariaLabel ? 'region' : undefined}
      aria-label={ariaLabel}
    >
      <div ref={containerRef} class="sh-location-map__canvas" />
      {tileError ? (
        <div class="sh-map-error">
          {tileErrorMessage()}
        </div>
      ) : !hasMarkers && !onPick && (
        <div class="sh-location-map__empty sh-muted">
          {emptyLabel ?? t('location.map_empty')}
        </div>
      )}
    </div>
  )
}
