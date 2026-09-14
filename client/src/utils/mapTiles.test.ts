/**
 * mapTiles tests.
 *
 * The load-bearing invariant here is the ingress one: the tile URL the
 * backend hands out is RELATIVE (no leading slash) so it resolves
 * against ``<base href>`` — under HA Supervisor ingress that's
 * ``/api/hassio_ingress/<token>/``. Anything that rewrites or
 * absolutises the URL breaks every map in haos, so the tests below
 * assert the helper passes it to Leaflet verbatim.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { Map as LeafletMap } from 'leaflet'

type TileHandler = () => void

/** Handlers Leaflet's layer registered, so a test can fire
 *  ``tileerror`` / ``tileload`` the way a failing tile request does. */
const layerHandlers = new Map<string, TileHandler[]>()

const tileLayer = vi.fn(
  (_url: string, _opts: Record<string, unknown>) => ({
    addTo: vi.fn(),
    on: (ev: string, fn: TileHandler) => {
      layerHandlers.set(ev, [...(layerHandlers.get(ev) ?? []), fn])
    },
  }),
)
vi.mock('leaflet', () => ({
  default: {
    get tileLayer() { return tileLayer },
  },
}))

/** Fire a Leaflet layer event ``times`` times. */
function emit(ev: string, times = 1): void {
  for (let i = 0; i < times; i += 1) {
    for (const fn of layerHandlers.get(ev) ?? []) fn()
  }
}

const mockApi = { get: vi.fn() }
vi.mock('@/api', () => ({
  get api() { return mockApi },
}))

const CONFIG = {
  tile_url: 'api/map/tiles?z={z}&x={x}&y={y}&exp=1234567890&sig=abc',
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">'
    + 'OpenStreetMap</a> contributors',
  max_zoom: 19,
}

const fakeMap = {} as unknown as LeafletMap

async function freshModule() {
  const mod = await import('./mapTiles')
  mod.resetTileConfigCache()
  return mod
}

beforeEach(() => {
  tileLayer.mockClear()
  layerHandlers.clear()
  mockApi.get.mockReset()
})

describe('mapTiles', () => {
  it('hands the backend tile URL to Leaflet verbatim', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer } = await freshModule()

    await addTileLayer(fakeMap)

    expect(mockApi.get).toHaveBeenCalledWith('/api/map/config')
    expect(tileLayer).toHaveBeenCalledWith(CONFIG.tile_url, {
      maxZoom: 19,
      attribution: CONFIG.attribution,
    })
  })

  it('keeps the tile URL relative so it resolves against the ingress prefix', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer } = await freshModule()

    await addTileLayer(fakeMap)

    const url = tileLayer.mock.calls[0][0]
    expect(url.startsWith('/')).toBe(false)
    expect(url.startsWith('http')).toBe(false)
    expect(url).toBe(CONFIG.tile_url)
  })

  it('fetches the config once no matter how many maps ask for tiles', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer } = await freshModule()

    await Promise.all([
      addTileLayer(fakeMap),
      addTileLayer(fakeMap),
      addTileLayer(fakeMap),
    ])

    expect(mockApi.get).toHaveBeenCalledTimes(1)
    expect(tileLayer).toHaveBeenCalledTimes(3)
  })

  it('rejects when the config fetch fails, and adds no layer', async () => {
    mockApi.get.mockRejectedValue(new Error('API 502: /api/map/config'))
    const { addTileLayer } = await freshModule()

    await expect(addTileLayer(fakeMap)).rejects.toThrow('API 502')
    expect(tileLayer).not.toHaveBeenCalled()
  })

  it('retries the fetch after a failure instead of caching the error', async () => {
    mockApi.get.mockRejectedValueOnce(new Error('offline'))
    mockApi.get.mockResolvedValueOnce(CONFIG)
    const { addTileLayer } = await freshModule()

    await expect(addTileLayer(fakeMap)).rejects.toThrow('offline')
    await addTileLayer(fakeMap)

    expect(mockApi.get).toHaveBeenCalledTimes(2)
    expect(tileLayer).toHaveBeenCalledTimes(1)
  })

  it('skips adding the layer when the caller cancelled (map unmounted)', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer } = await freshModule()

    await addTileLayer(fakeMap, () => true)

    expect(tileLayer).not.toHaveBeenCalled()
  })
  // ── Tile-level failures (config OK, tiles not) ──────────────────────
  //
  // A 401 past the signature TTL, a 429, or a 502 from a dead upstream
  // all land here — the config fetch already succeeded, so without
  // these the user is back to a silent grey square.

  it('reports a persistent tile failure through onError', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileerror', TILE_ERROR_THRESHOLD)

    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('does not report again once the failure has been surfaced', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileerror', TILE_ERROR_THRESHOLD * 3)

    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('ignores a lone transient tile error at the edge of the world', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileerror', TILE_ERROR_THRESHOLD - 1)

    expect(onError).not.toHaveBeenCalled()
  })

  it('reports a total outage on a small map with only N tiles, N below the streak threshold', async () => {
    // SpaceLocationCard (h=380), LocationPostCard (h=160) and
    // LocationPicker (h=220) in a narrow mobile column request only a
    // handful of tiles — fewer than TILE_ERROR_THRESHOLD. A dead proxy
    // there fails every one of them, and the streak never gets long
    // enough, so without the all-errored rule the user is left staring
    // at the silent grey square this whole module exists to kill.
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()
    const tiles = TILE_ERROR_THRESHOLD - 1

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileloadstart', tiles)
    emit('tileerror', tiles)

    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('stays quiet when one tile of a full viewport errors and the rest load', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileloadstart', TILE_ERROR_THRESHOLD * 2)
    emit('tileerror')
    emit('tileload', TILE_ERROR_THRESHOLD * 2 - 1)

    expect(onError).not.toHaveBeenCalled()
  })

  it('resets the failure streak when a tile loads in between', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()

    await addTileLayer(fakeMap, undefined, onError)
    emit('tileerror', TILE_ERROR_THRESHOLD - 1)
    emit('tileload')
    emit('tileerror', TILE_ERROR_THRESHOLD - 1)

    expect(onError).not.toHaveBeenCalled()
  })

  it('stays quiet about tile errors once the caller cancelled', async () => {
    mockApi.get.mockResolvedValue(CONFIG)
    const { addTileLayer, TILE_ERROR_THRESHOLD } = await freshModule()
    const onError = vi.fn()
    let cancelled = false

    await addTileLayer(fakeMap, () => cancelled, onError)
    cancelled = true
    emit('tileerror', TILE_ERROR_THRESHOLD)

    expect(onError).not.toHaveBeenCalled()
  })
})
