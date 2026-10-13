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
import { addTileLayer, loadTileConfig, tileErrorMessage } from '@/utils/mapTiles'
import { escapeHtml } from '@/utils/html'
import { zoneColor } from '@/utils/zoneColor'
import { colorClass } from '@/utils/tokenColor'
import {
  bubbleSlots,
  groupBounds,
  groupByZone,
  mergeOverlapping,
  personColorToken,
  zoneBounds,
  type BubbleExtent,
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

/** Pixels of the map covered by something drawn over it (the map
 *  overview's bottom sheet): a fit keeps markers out of that strip. */
export interface MapInsets {
  top?: number
  right?: number
  bottom?: number
  left?: number
}

export interface LocationMapProps {
  markers: LocationMarker[]
  /** Zones to draw (a space's, or the household's on the presence
   *  page). Optional. */
  zones?: LocationZoneOverlay[]
  /** Height in CSS pixels. Falls back to 320. */
  height?: number
  /** When true + no markers, shows a muted fallback pane instead of
   *  an empty map — keeps the layout stable on dashboards. An empty
   *  string shows no pane at all. */
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
  /** A picture of the map, not a control: no panning / zooming, inert
   *  markers, no zoom buttons — for a map inside a link (the corner
   *  card stays one link). Read once, on mount. */
  readOnly?: boolean
  /** Room taken by a panel over the map — added to every fit's padding
   *  so nothing lands underneath it. Defaults to none. */
  fitInsets?: MapInsets
  /** Move the view to the selection: a person at street level, a zone
   *  with its whole radius; clearing the selection fits everyone again.
   *  Off by default (the selection only highlights). */
  zoomToSelection?: boolean
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

/** Avatar edge (desktop; phones draw 36 px — 40 keeps merging safe). */
const AVATAR_PX = 40
/** A bubble body's padding + border around its avatars (CSS 5 + 1, ×2). */
const BUBBLE_CHROME_PX = 12
/** Full-size zone centre marker (CSS ``.sh-map-zone``). */
const ZONE_MARKER_PX = 36
/** Breathing room between a fitted marker and the map edge. */
const EDGE_PX = 12
/** Side padding when fitting the view. */
const FIT_SIDE_PX = 32
/** Clearance kept between two bubbles before they merge. */
const BUBBLE_GAP_PX = 6
/** How far above its anchor a bubble's box ends (CSS ``--sh-map-gap``
 *  minus the tail overhang): a zone bubble clears the zone marker. */
const LIFT_PX: Record<'zone' | 'solo' | 'group', number> = { zone: 19, solo: 5, group: 5 }
/** Below this on-screen radius the zone circle hides under the full
 *  36 px centre marker, so the marker goes compact (22 px). */
const COMPACT_ZONE_PX = 26
/** Zoom cap when a click zooms to a group (and the zoom at which a
 *  zone counts as already shown). */
const GROUP_MAX_ZOOM = 18
const GROUP_FIT_PADDING: [number, number] = [48, 48]
/** Pixel room when fitting the view: above the anchors a whole zone
 *  bubble (it floats highest), below them half a zone marker. */
const FIT_PADDING_TOP_LEFT: [number, number] = [
  FIT_SIDE_PX, LIFT_PX.zone + AVATAR_PX + BUBBLE_CHROME_PX + EDGE_PX,
]
/** Leaflet's zoom buttons in the top-left corner: 34 px wide plus a
 *  10 px margin. A fit keeps the bubbles clear of them when shown. */
const ZOOM_CONTROL_PX = 34 + 10
const FIT_PADDING_BOTTOM_RIGHT: [number, number] = [
  FIT_SIDE_PX, ZONE_MARKER_PX / 2 + EDGE_PX,
]
/** Closest the initial fit zooms in. */
const FIT_MAX_ZOOM = 16
/** Street level — where a selected person is shown. */
const STREET_ZOOM = 17

type _Kind = 'zone' | 'solo' | 'group'

function _kind(g: MarkerGroup<LocationMarker>): _Kind {
  if (g.merged) return 'group'
  return g.zoneIds.length > 0 ? 'zone' : 'solo'
}

/** Web-Mercator metres per screen pixel at ``lat`` and ``zoom``. */
function _metresPerPixel(lat: number, zoom: number): number {
  return (40_075_016.686 * Math.cos((lat * Math.PI) / 180)) / 2 ** (zoom + 8)
}

function _isCompactZone(z: LocationZoneOverlay, zoom: number): boolean {
  return z.radius_m / _metresPerPixel(z.latitude, zoom) < COMPACT_ZONE_PX
}

function _bubbleHtml(
  g: MarkerGroup<LocationMarker>, kind: _Kind, selected: string | null,
): string {
  const { shown, overflow } = bubbleSlots(g.members)
  const avatars = shown.map((m) => _avatarHtml(m, m.id === selected)).join('')
  const more = overflow > 0
    ? `<span class="sh-map-bubble__more">+${overflow}</span>`
    : ''
  // Name whoever is selected in this bubble — also when they're folded
  // into the "+N" chip, so the selection is never invisible.
  const picked = g.members.find((m) => m.id === selected)
  const caption = picked
    ? `<span class="sh-map-bubble__caption">${escapeHtml(_title(picked))}</span>`
    : ''
  return (
    `<div class="sh-map-bubble sh-map-bubble--${kind}${picked ? ' is-selected' : ''}">`
    + caption
    + `<span class="sh-map-bubble__body" aria-hidden="true">${avatars}${more}</span>`
    + `<span class="sh-map-bubble__tail" aria-hidden="true"></span>`
    // A solo bubble marks a real position; a zone bubble points at its
    // zone marker and a merged one at a centroid that is nowhere.
    + (kind === 'solo' ? '<span class="sh-map-bubble__dot" aria-hidden="true"></span>' : '')
    + `</div>`
  )
}

/** The bubble's on-screen box, for merging (see ``mergeOverlapping``). */
function _extent(g: MarkerGroup<LocationMarker>, selected: string | null): BubbleExtent {
  const { shown, overflow } = bubbleSlots(g.members)
  const n = shown.length
  const w = BUBBLE_CHROME_PX + n * AVATAR_PX + (n - 1) * 4 + (overflow > 0 ? 40 : 0)
  const h = AVATAR_PX + BUBBLE_CHROME_PX + (g.members.some((m) => m.id === selected) ? 28 : 0)
  return { w, h, lift: LIFT_PX[_kind(g)] }
}

function _zoneFace(z: LocationZoneOverlay): string {
  const icon = z.icon?.trim()
  if (icon) return icon
  return Array.from(z.name.trim())[0]?.toUpperCase() ?? '?'
}

function _zoneHtml(z: LocationZoneOverlay, selected: boolean, compact: boolean): string {
  // ``zoneColor`` only ever returns a ``#RRGGBB`` value, so it is safe
  // inside a style attribute.
  return (
    `<div class="sh-map-zone${selected ? ' is-selected' : ''}${compact ? ' sh-map-zone--compact' : ''}" `
    + `style="--zone-colour: ${zoneColor(z)}" aria-hidden="true">`
    + `<span class="sh-map-zone__icon">${escapeHtml(_zoneFace(z))}</span>`
    + `</div>`
  )
}

/** Finish a marker's icon element: ``data-key`` (to find it again after
 *  a redraw), the accessible name, Enter / Space activation (Leaflet
 *  makes markers tabbable buttons but only clicks activate them), and a
 *  photo that fails to load (expired signature) dropping out so the
 *  initials show. */
function _finishIcon(marker: L.Marker, title: string, key: string, interactive: boolean): void {
  const el = marker.getElement?.()
  if (!el) return
  el.setAttribute('data-key', key)
  el.setAttribute('aria-label', title)
  if (interactive) {
    el.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key !== 'Enter' && e.key !== ' ') return
      e.preventDefault()
      marker.fire('click')
    })
  } else {
    el.tabIndex = -1
  }
  for (const img of Array.from(el.querySelectorAll('img'))) {
    img.addEventListener('error', () => { img.remove() }, { once: true })
  }
}

/** Key of the marker holding keyboard focus, if any. */
function _focusedKey(markers: Map<string, L.Marker>): string | null {
  const active = document.activeElement
  if (!active) return null
  for (const [key, m] of markers) {
    const el = m.getElement?.()
    if (el && (el === active || el.contains(active))) return key
  }
  return null
}

function _refocus(markers: Map<string, L.Marker>, key: string | null): void {
  if (key) markers.get(key)?.getElement?.()?.focus({ preventScroll: true })
}

/** The members of a crowded zone as real buttons (names as text, never
 *  markup), for a Leaflet popup over the bubble. */
function _membersList(
  members: LocationMarker[], pick: (id: string) => void,
): HTMLElement {
  const list = document.createElement('ul')
  list.className = 'sh-map-members'
  for (const m of members) {
    const item = document.createElement('li')
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'sh-map-members__button'
    button.textContent = m.label
    button.addEventListener('click', () => { pick(m.id) })
    item.appendChild(button)
    list.appendChild(item)
  }
  return list
}

/** The tile source's attribution HTML as plain text (tags dropped,
 *  entities decoded). ``DOMParser`` builds an inert document — nothing
 *  loads or runs — and only its text is used. */
function _plainText(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  return (doc.body.textContent ?? '').replace(/\s+/g, ' ').trim()
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

function _inset(v: number | undefined): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0
}

/** Fit padding: room above for a whole bubble (they float above their
 *  anchor), below for a zone marker, the zoom buttons on the left, plus
 *  whatever a panel covers. */
function _fitPadding(
  still: boolean, insets: MapInsets | undefined,
): { paddingTopLeft: [number, number], paddingBottomRight: [number, number] } {
  return {
    paddingTopLeft: [
      FIT_PADDING_TOP_LEFT[0] + (still ? 0 : ZOOM_CONTROL_PX) + _inset(insets?.left),
      FIT_PADDING_TOP_LEFT[1] + _inset(insets?.top),
    ],
    paddingBottomRight: [
      FIT_PADDING_BOTTOM_RIGHT[0] + _inset(insets?.right),
      FIT_PADDING_BOTTOM_RIGHT[1] + _inset(insets?.bottom),
    ],
  }
}

export function LocationMap({
  markers, zones, height = 320, emptyLabel,
  onPick, ariaLabel, selectedId, onSelect, readOnly = false,
  fitInsets, zoomToSelection = false,
}: LocationMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const placeLayerRef = useRef<L.LayerGroup | null>(null)
  const zoneLayerRef = useRef<L.LayerGroup | null>(null)
  /** What each layer currently shows, by key — to restyle in place and
   *  to put keyboard focus back after a redraw. */
  const peopleMarkersRef = useRef(new Map<string, L.Marker>())
  const zoneMarkersRef = useRef(new Map<string, L.Marker>())
  const [tileError, setTileError] = useState(false)
  /** Read-only maps show the tile licence as a plain caption instead of
   *  Leaflet's attribution control (its links would nest inside the
   *  link the map sits in). */
  const [attribution, setAttribution] = useState<string | null>(null)
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
  const readOnlyRef = useRef(readOnly)
  const insetsRef = useRef(fitInsets)
  insetsRef.current = fitInsets
  const zoomToSelectionRef = useRef(zoomToSelection)
  zoomToSelectionRef.current = zoomToSelection
  /** True while the view shows a selection (not everyone): clearing the
   *  selection then fits everyone again. */
  const showsSelectionRef = useRef(false)
  const fitViewRef = useRef((_reason: 'data' | 'selection') => {})
  const selectRef = useRef((_id: string | null) => {})
  selectRef.current = (id: string | null) => {
    if (selectedId === undefined) setOwnSelected(id)
    onSelect?.(id)
  }
  const drawPeopleRef = useRef(() => {})
  const styleZonesRef = useRef(() => {})

  useEffect(() => {
    if (!containerRef.current) return
    if (mapRef.current) return
    const still = readOnlyRef.current
    const map = L.map(containerRef.current, {
      zoomControl: !still,
      attributionControl: !still,
      // Sensible defaults — fitBounds overrides these on first paint.
      center: [52.0, 5.0],
      zoom: 4,
      // Keep scroll-wheel off by default — users scroll pages past
      // embedded maps all the time and captured wheel events are
      // disorienting. Ctrl+scroll still zooms.
      scrollWheelZoom: still ? false : 'center',
      dragging: !still,
      touchZoom: !still,
      doubleClickZoom: !still,
      boxZoom: !still,
      keyboard: !still,
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
    if (still) {
      // Same cached config the tile layer used — no extra request.
      void loadTileConfig()
        .then((cfg) => { if (!cancelled) setAttribution(_plainText(cfg.attribution)) })
        .catch(() => { /* the tile error overlay covers this */ })
    }
    // Zones go below the people layer so bubbles always sit on top of
    // their zone overlay.
    zoneLayerRef.current = L.layerGroup().addTo(map)
    placeLayerRef.current = L.layerGroup().addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    map.on('click', (e: L.LeafletMouseEvent) => {
      if (readOnlyRef.current) return
      if (onPickRef.current) {
        onPickRef.current(e.latlng.lat, e.latlng.lng)
      } else if (selectedRef.current !== null) {
        // Clicking the empty map (zone circles included — they let
        // clicks through) deselects. Marker clicks don't reach here.
        selectRef.current(null)
      }
    })
    // Screen distances and zone sizes change with the zoom.
    map.on('zoomend', () => {
      styleZonesRef.current()
      drawPeopleRef.current()
    })

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

  // Zone look that follows the view and the selection — restyled in
  // place, never rebuilt (that would drop focus and churn the layer).
  styleZonesRef.current = () => {
    const map = mapRef.current
    if (!map) return
    const zoom = map.getZoom()
    for (const [id, marker] of zoneMarkersRef.current) {
      const z = zonesRef.current?.find((zz) => zz.id === id)
      const face = marker.getElement?.()?.querySelector('.sh-map-zone')
      if (!z || !face) continue
      const isSel = selectedRef.current === `zone:${id}`
      face.classList.toggle('is-selected', isSel)
      face.classList.toggle('sh-map-zone--compact', _isCompactZone(z, zoom))
      marker.setZIndexOffset(isSel ? 500 : 0)
    }
  }

  // Rebuild zones only when what they show changes — not when a parent
  // re-render hands over an equal array.
  const zonesDrawKey = JSON.stringify(
    (zones ?? []).map((z) => [z.id, z.name, z.latitude, z.longitude, z.radius_m, z.color, z.icon ?? null]),
  )
  // Zones: a coloured radius plus a centre marker with the zone's icon.
  // The circle is decoration (clicks pass through to the map); the
  // centre marker names and selects the zone.
  useEffect(() => {
    const map = mapRef.current
    const zoneLayer = zoneLayerRef.current
    if (!map || !zoneLayer) return
    const focused = _focusedKey(zoneMarkersRef.current)
    zoneLayer.clearLayers()
    zoneMarkersRef.current = new Map()
    const still = readOnlyRef.current
    const zoom = map.getZoom()
    for (const z of zonesRef.current ?? []) {
      const colour = zoneColor(z)
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.85,
        fillColor: colour,
        // Light enough that two overlapping zones don't compound into a
        // muddy blob; the crisp border tells one zone from the next.
        fillOpacity: 0.2,
        weight: 2,
        interactive: false,
      }).addTo(zoneLayer)
      const key = `zone:${z.id}`
      const title = t('location.map.zone_marker', { name: z.name })
      const isSel = selectedRef.current === key
      const centre = L.marker([z.latitude, z.longitude], {
        icon: _icon(_zoneHtml(z, isSel, _isCompactZone(z, zoom)), ' sh-map-icon--zone-centre'),
        title,
        keyboard: !still,
        interactive: !still,
        zIndexOffset: isSel ? 500 : 0,
      })
        .on('click', () => { selectRef.current(key) })
        .addTo(zoneLayer)
      _finishIcon(centre, title, key, !still)
      zoneMarkersRef.current.set(z.id, centre)
    }
    _refocus(zoneMarkersRef.current, focused)
  }, [zonesDrawKey])
  useEffect(() => { styleZonesRef.current() }, [selected])

  // People: group by zone, merge what would overlap at this zoom, draw.
  drawPeopleRef.current = () => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    const focused = _focusedKey(peopleMarkersRef.current)
    layer.clearLayers()
    peopleMarkersRef.current = new Map()
    const still = readOnlyRef.current
    const zoneList = zonesRef.current ?? []
    const sel = selectedRef.current
    const people = markersRef.current.filter((m) => !_isPlace(m))
    const groups = mergeOverlapping(
      groupByZone(people, zoneList),
      (lat, lon) => map.latLngToContainerPoint([lat, lon]),
      BUBBLE_GAP_PX,
      (g) => _extent(g, sel),
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
        keyboard: !still,
        interactive: !still,
        // Bubbles float over zone markers; the selected one on top.
        zIndexOffset: isSel ? 2000 : 1000,
      })
        .on('click', () => {
          if (single) {
            selectRef.current(single.id)
            return
          }
          const bounds = L.latLngBounds(groupBounds(g, zoneList))
          // A zone bubble selects its zone (who's there); a merged
          // bubble is no single place, so it selects nothing.
          if (kind === 'zone') {
            const key = `zone:${g.zoneIds[0]}`
            const fresh = selectedRef.current !== key
            selectRef.current(key)
            // The selection moves the view itself (to the zone's circle).
            if (fresh && zoomToSelectionRef.current) return
          }
          // The first click zooms in. Once the group already fills the
          // view, zooming can't split it (a crowded zone, or people
          // standing a few metres apart), so list who's there instead.
          const target = Math.min(
            map.getBoundsZoom(bounds, false, L.point(
              GROUP_FIT_PADDING[0] * 2, GROUP_FIT_PADDING[1] * 2,
            )),
            GROUP_MAX_ZOOM,
          )
          if (map.getZoom() >= target && map.getBounds().contains(bounds)) {
            const list = _membersList(g.members, (id) => {
              map.closePopup()
              selectRef.current(id)
            })
            L.popup({
              closeButton: false,
              className: 'sh-map-members-popup',
              // Float above this bubble's own height.
              offset: [0, -(LIFT_PX[kind] + AVATAR_PX + 24)],
            })
              .setLatLng([g.lat, g.lon])
              .setContent(list)
              .openOn(map)
            list.querySelector('button')?.focus({ preventScroll: true })
            return
          }
          map.fitBounds(bounds, { padding: GROUP_FIT_PADDING, maxZoom: GROUP_MAX_ZOOM })
        })
        .addTo(layer)
      _finishIcon(marker, title, g.key, !still)
      peopleMarkersRef.current.set(g.key, marker)
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
    _refocus(peopleMarkersRef.current, focused)
  }
  useEffect(() => { drawPeopleRef.current() }, [markers, zones, selected])

  // Places + the viewport: only when positions or zones actually change,
  // so a parent re-render (a selection, a poll returning the same pins)
  // never yanks the user's zoom or closes an open place popup.
  const positionsKey = JSON.stringify(
    markers.map((m) => [m.id, m.lat ?? null, m.lon ?? null, m.zone_id ?? null, m.glyph ?? null]),
  )
  const zonesKey = JSON.stringify(
    (zones ?? []).map((z) => [z.id, z.latitude, z.longitude, z.radius_m]),
  )
  useEffect(() => {
    const map = mapRef.current
    const placeLayer = placeLayerRef.current
    if (!map || !placeLayer) return
    placeLayer.clearLayers()
    const still = readOnlyRef.current
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
        title: _title(m),
        keyboard: !still,
        interactive: !still,
      }).addTo(placeLayer)
      if (!still) {
        pin.bindPopup(
          `<strong>${escapeHtml(m.label)}</strong>`
          + (m.sub_label ? `<br /><span>${escapeHtml(m.sub_label)}</span>` : ''),
        )
      }
      _finishIcon(pin, _title(m), `place:${m.id}`, !still)
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

    fitViewRef.current('data')
    // Draw after the view settles so the merge sees the final zoom.
    styleZonesRef.current()
    drawPeopleRef.current()
  }, [positionsKey, zonesKey])

  // The viewport. ``data``: positions or zones changed — keep showing the
  // selection when the view follows it, else fit everyone. ``selection``:
  // the selection (or the panel over the map) changed — follow it, or
  // fit everyone once it is cleared.
  fitViewRef.current = (reason) => {
    const map = mapRef.current
    if (!map) return
    const still = readOnlyRef.current
    const current = markersRef.current
    const zoneList = zonesRef.current ?? []
    const people = current.filter((m) => !_isPlace(m))
    const padding = _fitPadding(still, insetsRef.current)
    const sel = zoomToSelectionRef.current ? selectedRef.current : null
    if (sel) {
      let target: [[number, number], [number, number]] | null = null
      let maxZoom = STREET_ZOOM
      if (sel.startsWith('zone:')) {
        const z = zoneList.find((zz) => `zone:${zz.id}` === sel)
        if (z) { target = zoneBounds(z); maxZoom = GROUP_MAX_ZOOM }
      } else {
        // Where the person's bubble is drawn: their zone's centre when
        // they share a zone bubble, else their own point.
        const g = groupByZone(people, zoneList).find((gg) => gg.members.some((m) => m.id === sel))
        if (g) target = [[g.lat, g.lon], [g.lat, g.lon]]
      }
      if (target) {
        map.fitBounds(L.latLngBounds(target), { ...padding, maxZoom })
        showsSelectionRef.current = true
        return
      }
    }
    if (reason === 'selection' && !showsSelectionRef.current) return
    showsSelectionRef.current = false

    // Fit to where the bubbles point (zone centres for zoned people, own
    // points for everyone else) plus every zone's centre. A zone's radius
    // only matters once zoomed to that zone (the zone-click path fits the
    // whole circle); fitting every circle here zoomed a level too far out
    // and merged everyone.
    const anchors: Array<[number, number]> = [
      ...groupByZone(people, zoneList).map((g): [number, number] => [g.lat, g.lon]),
      ...current.filter((m) => _isPlace(m) && _finite(m.lat) && _finite(m.lon))
        .map((m): [number, number] => [m.lat as number, m.lon as number]),
      ...zoneList.map((z): [number, number] => [z.latitude, z.longitude]),
    ]
    if (anchors.length === 1) {
      // In pick mode keep the user's zoom when they move the pin — a
      // re-centre on every tap must not yank the map out from under them.
      const zoom = onPickRef.current ? Math.max(map.getZoom(), 14) : 14
      map.setView(anchors[0], zoom)
      // Centre it in the part a panel leaves visible.
      const insets = insetsRef.current
      const dx = (_inset(insets?.right) - _inset(insets?.left)) / 2
      const dy = (_inset(insets?.bottom) - _inset(insets?.top)) / 2
      if (dx !== 0 || dy !== 0) map.panBy([dx, dy], { animate: false })
    } else if (anchors.length > 1) {
      // Pixel padding, no ``pad()`` inflation.
      map.fitBounds(L.latLngBounds(anchors), { ...padding, maxZoom: FIT_MAX_ZOOM })
    }
  }

  // Follow the selection, and the panel over the map growing or
  // shrinking. Skipped on mount: the positions effect has just fitted.
  const insetsKey = JSON.stringify([
    _inset(fitInsets?.top), _inset(fitInsets?.right),
    _inset(fitInsets?.bottom), _inset(fitInsets?.left),
  ])
  const viewMountedRef = useRef(false)
  useEffect(() => {
    if (!viewMountedRef.current) { viewMountedRef.current = true; return }
    if (!zoomToSelectionRef.current) return
    fitViewRef.current('selection')
  }, [selected, insetsKey])

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
      {readOnly && attribution && (
        <div class="sh-location-map__attribution">{attribution}</div>
      )}
      {tileError ? (
        <div class="sh-map-error">
          {tileErrorMessage()}
        </div>
      ) : !hasMarkers && !onPick && emptyLabel !== '' && (
        <div class="sh-location-map__empty sh-muted">
          {emptyLabel ?? t('location.map_empty')}
        </div>
      )}
    </div>
  )
}
