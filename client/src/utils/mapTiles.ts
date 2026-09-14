/**
 * mapTiles — the single source of map tiles for every Leaflet map in
 * the SPA.
 *
 * Why this exists
 * ---------------
 *
 * The SPA used to point ``L.tileLayer`` straight at
 * ``https://tile.openstreetmap.org/{z}/{x}/{y}.png``. OpenStreetMap's
 * tile policy requires an identifying ``User-Agent`` / ``Referer``,
 * and a browser cannot set either (both are forbidden header names),
 * so the OSMF tile servers now answer a browser-issued tile request
 * with 403 — every map rendered as a grey box.
 *
 * The backend proxies tiles instead (``GET /api/map/config`` hands
 * out a signed, already-parameterised tile URL). This module fetches
 * that config exactly once per page load and hands the resulting URL
 * to Leaflet **verbatim** — the URL is deliberately relative (no
 * leading slash) so it resolves against ``<base href>`` and keeps
 * working behind the HA Supervisor ingress prefix. Rewriting it, or
 * prefixing it with ``/``, breaks haos.
 *
 * Nothing else in the SPA may hard-code a tile URL, an attribution
 * string, or a max zoom.
 */
import L from 'leaflet'
import { api } from '@/api'

/**
 * Copy for the tile-failure overlay. Lives here so the four map
 * surfaces cannot drift apart, and names the one recovery the user
 * actually has — a reload re-signs the tile URL and refetches the
 * config.
 */
export const TILE_ERROR_MESSAGE =
  "Map unavailable — couldn't load map tiles. Reload to try again."

/** Shape of ``GET /api/map/config``. */
export interface MapTileConfig {
  /** Relative tile URL template with ``{z}``/``{x}``/``{y}``
   *  placeholders, already carrying the backend's ``exp``/``sig``.
   *  Handed to Leaflet as-is. */
  tile_url: string
  /** Attribution HTML for the map's attribution control. */
  attribution: string
  /** Highest zoom level the proxied tile source serves. */
  max_zoom: number
}

/** In-flight (or resolved) config request. Module-level so N maps on
 *  one page share a single ``/api/map/config`` round trip. Cleared on
 *  failure so a transient blip doesn't poison the rest of the
 *  session. */
let _configPromise: Promise<MapTileConfig> | null = null

/** Fetch (once) the tile config. Subsequent callers get the same
 *  promise. */
export function loadTileConfig(): Promise<MapTileConfig> {
  if (!_configPromise) {
    _configPromise = api.get<MapTileConfig>('/api/map/config').catch(
      (err: unknown) => {
        _configPromise = null
        throw err
      },
    )
  }
  return _configPromise
}

/** Drop the cached config. Test-only seam — production code has no
 *  reason to refetch within a page load. */
export function resetTileConfigCache(): void {
  _configPromise = null
}

/**
 * Consecutive ``tileerror`` events before ``onError`` fires.
 *
 * A single failure is routine — Leaflet requests tiles past the edge
 * of the world and the proxy legitimately 404s them — so one error
 * must never flash "Map unavailable". A real outage (401 past the
 * signature TTL, 429, 502 from a dead upstream) fails every tile in
 * the viewport, so the streak builds immediately.
 *
 * The streak alone only catches viewports big enough to ask for that
 * many tiles: a small surface (LocationPostCard is 160 px tall,
 * LocationPicker 220, SpaceLocationCard 380 — 2-3 tiles in a narrow
 * mobile column) can have *every* tile fail without the count ever
 * reaching the threshold. The all-errored rule below covers those.
 */
export const TILE_ERROR_THRESHOLD = 4

/**
 * Add the shared tile layer to ``map``.
 *
 * Rejects when the config can't be fetched so the caller can render
 * an error state instead of a silent grey square.
 *
 * ``isCancelled`` guards the gap between mount and the config
 * arriving: maps are created synchronously in a ``useEffect`` and may
 * already have been ``remove()``d by the time this resolves. Pass a
 * closure over the effect's cleanup flag.
 *
 * ``onError`` covers the *other* half of the failure surface: the
 * config fetch succeeding but the tiles themselves failing. By then
 * this promise has long resolved, so the report arrives through the
 * callback instead — call sites route it into the same "Map
 * unavailable" state as a config rejection. Fires at most once, on
 * whichever comes first: ``TILE_ERROR_THRESHOLD`` consecutive
 * failures, or every requested tile having resolved as an error with
 * none loaded.
 */
export async function addTileLayer(
  map: L.Map,
  isCancelled?: () => boolean,
  onError?: () => void,
): Promise<void> {
  const cfg = await loadTileConfig()
  if (isCancelled?.()) return
  const layer = L.tileLayer(cfg.tile_url, {
    maxZoom: cfg.max_zoom,
    attribution: cfg.attribution,
  })
  if (onError) {
    let requested = 0
    let loaded = 0
    let errored = 0
    let streak = 0
    let reported = false
    // Leaflet fires 'tileloadstart' for every tile of the current view
    // synchronously as it builds them, so by the time any response can
    // come back ``requested`` already holds the whole viewport — an
    // error can never run ahead of its own request.
    layer.on('tileloadstart', () => { requested += 1 })
    // Any successful tile clears the streak — "consecutive" is what
    // separates an outage from a 404 at the edge of the world.
    layer.on('tileload', () => { loaded += 1; streak = 0 })
    layer.on('tileerror', () => {
      errored += 1
      streak += 1
      // Either the streak is long enough to rule out a stray 404, or
      // the viewport is too small to ever build one and the whole of
      // it has failed: nothing loaded, nothing outstanding.
      const outage = loaded === 0 && requested > 0 && errored >= requested
      if (reported || (streak < TILE_ERROR_THRESHOLD && !outage)) return
      if (isCancelled?.()) return
      reported = true
      onError()
    })
  }
  layer.addTo(map)
}
