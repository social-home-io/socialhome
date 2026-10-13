import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'

// jsdom doesn't ship ResizeObserver; LocationMap uses it to
// invalidate Leaflet's size when a hidden tab becomes visible.
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

// Leaflet manipulates the DOM directly which is heavy under jsdom.
// The smoke test only validates the module contract: exports a
// component, renders a container, and handles the empty state.
/** Handlers the component registered via ``map.on(...)``. */
const mapHandlers: Record<string, (e: unknown) => void> = {}
/** Every popup HTML string handed to a marker. */
const popups: string[] = []
/** Map popups opened with ``L.popup().openOn(map)`` (their content). */
const mapPopups: HTMLElement[] = []
/** Fake view state: pixels per degree for the projection (lower it to
 *  "zoom out"), the zoom, and whether a group's bounds already fit. */
const view = { scale: 100_000, zoom: 4, boundsZoom: 15, contains: false, height: 400 }
interface FakeMarker {
  latlng: [number, number]
  opts: {
    icon?: { html: string, className?: string }
    title?: string
    keyboard?: boolean
    interactive?: boolean
  }
  handlers: Record<string, (e?: unknown) => void>
  /** The icon element, mounted while the marker is on a layer — built
   *  the way Leaflet builds a divIcon (className + innerHTML), with
   *  tabindex/role/title when ``keyboard`` is on. */
  el: HTMLElement
}
/** Markers currently on a layer (cleared with ``clearLayers``). */
let liveMarkers: FakeMarker[] = []
/** Stands in for the map's marker pane so icon elements are in the
 *  document (focusable). */
let pane: HTMLElement
const fakeMap = {
  remove: vi.fn(),
  invalidateSize: vi.fn(),
  setView: vi.fn(),
  panBy: vi.fn(),
  fitBounds: vi.fn(),
  closePopup: vi.fn(),
  getZoom: vi.fn(() => view.zoom),
  getSize: vi.fn(() => ({ x: 800, y: view.height })),
  getBoundsZoom: vi.fn(() => view.boundsZoom),
  getBounds: vi.fn(() => ({ contains: vi.fn(() => view.contains) })),
  latLngToContainerPoint: vi.fn((ll: [number, number]) => ({
    x: ll[1] * view.scale, y: -ll[0] * view.scale,
  })),
  on: vi.fn((name: string, fn: (e: unknown) => void) => {
    mapHandlers[name] = fn
  }),
}
vi.mock('leaflet', () => ({
  default: {
    map: vi.fn(() => fakeMap),
    tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
    layerGroup: vi.fn(() => {
      const mine: FakeMarker[] = []
      const g = {
        addTo: vi.fn(() => g),
        clearLayers: vi.fn(() => {
          for (const m of mine) m.el.remove()
          liveMarkers = liveMarkers.filter((m) => !mine.includes(m))
          mine.length = 0
        }),
        _track: (m: FakeMarker) => {
          mine.push(m)
          liveMarkers.push(m)
          pane.appendChild(m.el)
        },
      }
      return g
    }),
    marker: vi.fn((latlng: [number, number], opts: FakeMarker['opts']) => {
      const el = document.createElement('div')
      el.className = opts.icon?.className ?? ''
      el.innerHTML = opts.icon?.html ?? ''
      if (opts.keyboard !== false && opts.interactive !== false) {
        el.tabIndex = 0
        el.setAttribute('role', 'button')
      }
      if (opts.title) el.title = opts.title
      const fm: FakeMarker = { latlng, opts, handlers: {}, el }
      const m = {
        addTo: vi.fn((layer: { _track?: (x: FakeMarker) => void }) => {
          layer._track?.(fm)
          return m
        }),
        bindPopup: vi.fn((html: string) => { popups.push(html); return m }),
        on: vi.fn((name: string, fn: (e?: unknown) => void) => {
          fm.handlers[name] = fn
          return m
        }),
        fire: vi.fn((name: string) => { fm.handlers[name]?.(); return m }),
        setZIndexOffset: vi.fn(() => m),
        getElement: vi.fn(() => (el.isConnected ? el : undefined)),
      }
      return m
    }),
    circle: vi.fn(() => {
      const bounds = { getSouthWest: vi.fn(), getNorthEast: vi.fn() }
      const c = {
        addTo: vi.fn(() => c),
        bindTooltip: vi.fn(() => c),
        bindPopup: vi.fn(() => c),
        getBounds: vi.fn(() => bounds),
      }
      return c
    }),
    popup: vi.fn(() => {
      const p = {
        setLatLng: vi.fn(() => p),
        setContent: vi.fn((el: HTMLElement) => { (p as { _el?: HTMLElement })._el = el; return p }),
        openOn: vi.fn(() => {
          const el = (p as { _el?: HTMLElement })._el!
          pane.appendChild(el)
          mapPopups.push(el)
          return p
        }),
      }
      return p
    }),
    point: vi.fn((x: number, y: number) => ({ x, y })),
    divIcon: vi.fn((opts: unknown) => opts),
    latLngBounds: vi.fn(() => {
      const b = { pad: vi.fn(() => ({})), extend: vi.fn(() => b) }
      return b
    }),
  },
}))

/** Every bubble (person / group) marker currently drawn. */
function bubbles(): FakeMarker[] {
  return liveMarkers.filter((m) => m.opts.icon?.html.includes('sh-map-bubble'))
}
/** Every zone-centre marker currently drawn. */
function zoneMarkers(): FakeMarker[] {
  return liveMarkers.filter((m) => m.opts.icon?.html.includes('sh-map-zone'))
}
function count(html: string, needle: string): number {
  return html.split(needle).length - 1
}

// Tiles come from the backend proxy (``/api/map/config``) via the
// shared helper — stub it so the component test doesn't need a fetch.
const addTileLayer = vi.fn<
  (
    map: unknown,
    isCancelled?: () => boolean,
    onError?: () => void,
  ) => Promise<void>
>()
const loadTileConfig = vi.fn<() => Promise<{ tile_url: string, attribution: string, max_zoom: number }>>()
vi.mock('@/utils/mapTiles', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/mapTiles')>()),
  get addTileLayer() { return addTileLayer },
  get loadTileConfig() { return loadTileConfig },
}))

beforeEach(() => {
  vi.clearAllMocks()
  liveMarkers = []
  Object.assign(view, { scale: 100_000, zoom: 4, boundsZoom: 15, contains: false, height: 400 })
  popups.length = 0
  mapPopups.length = 0
  document.body.querySelector('[data-pane]')?.remove()
  pane = document.createElement('div')
  pane.setAttribute('data-pane', '')
  document.body.appendChild(pane)
  for (const k of Object.keys(mapHandlers)) delete mapHandlers[k]
  addTileLayer.mockReset()
  addTileLayer.mockResolvedValue(undefined)
  loadTileConfig.mockReset()
  loadTileConfig.mockResolvedValue({
    tile_url: 'api/map/tiles?z={z}&x={x}&y={y}',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    max_zoom: 19,
  })
})

describe('LocationMap', () => {
  it('module exports a LocationMap component', async () => {
    const mod = await import('./LocationMap')
    expect(mod.LocationMap).toBeTruthy()
    expect(typeof mod.LocationMap).toBe('function')
  })

  it('shows the empty-label pane when no markers are provided', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { getByText } = render(
      <LocationMap markers={[]} emptyLabel="Nothing here yet." />,
    )
    expect(getByText('Nothing here yet.')).toBeTruthy()
  })

  it('shows no pane at all for an empty-string label', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { container } = render(<LocationMap markers={[]} emptyLabel="" />)
    expect(container.querySelector('.sh-location-map__empty')).toBeNull()
  })

  it('asks the shared helper for the proxied tile layer', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[]} />)
    expect(addTileLayer).toHaveBeenCalled()
  })

  it('shows a tiles-unavailable message when the tile config fails', async () => {
    addTileLayer.mockRejectedValue(new Error('API 502: /api/map/config'))
    const { render, waitFor } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { getByText } = render(<LocationMap markers={[]} />)
    await waitFor(() => {
      expect(getByText(/Map unavailable/)).toBeTruthy()
    })
  })
  it('surfaces a tile-load failure the same way as a config failure', async () => {
    let report: (() => void) | undefined
    addTileLayer.mockImplementation((_map, _cancelled, onError) => {
      report = onError
      return Promise.resolve()
    })
    const { render, waitFor, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { getByText } = render(<LocationMap markers={[]} />)

    await waitFor(() => { expect(report).toBeTypeOf('function') })
    act(() => { report!() })

    await waitFor(() => {
      expect(getByText(/Map unavailable/)).toBeTruthy()
    })
  })

  // Regression: the mount effect's cleanup sets ``cancelled = true`` so
  // a tile config that lands after unmount doesn't touch a removed map
  // (or set state on a gone component). Deleting that line used to leave
  // the whole suite green, because the stubbed helper resolved before
  // unmount could ever race it — so hold the promise open across the
  // unmount and read the flag the component handed us.
  it('tells the tile helper it was cancelled once the map unmounts', async () => {
    let captured: (() => boolean) | undefined
    let resolveTiles: () => void = () => {}
    addTileLayer.mockImplementation((_map, isCancelled) => {
      captured = isCancelled
      return new Promise<void>((resolve) => { resolveTiles = resolve })
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { unmount, container } = render(<LocationMap markers={[]} />)

    await waitFor(() => { expect(captured).toBeTypeOf('function') })
    expect(captured!()).toBe(false)

    unmount()
    expect(captured!()).toBe(true)

    // Resolving after unmount must be a no-op — no render, no state.
    resolveTiles()
    await Promise.resolve()
    expect(container.textContent).toBe('')
  })

  it('escapes a place label before it reaches the popup HTML', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[{
          id: 'x', lat: 1, lon: 2, glyph: '📍',
          label: '<img src=x onerror=alert(1)>',
          sub_label: '"quoted" & <b>',
        }]}
      />,
    )
    expect(popups).toHaveLength(1)
    expect(popups[0]).not.toContain('<img')
    expect(popups[0]).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(popups[0]).toContain('&quot;quoted&quot; &amp; &lt;b&gt;')
  })

  it('names a hostile zone literally on its centre marker, never as markup', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const name = '<img src=x onerror="window.__xss=1"> & <b>Bold</b>'
    render(
      <LocationMap
        markers={[]}
        zones={[{
          id: 'z1', name, latitude: 1, longitude: 2, radius_m: 100, color: null,
        }]}
      />,
    )
    const [centre] = zoneMarkers()
    // Leaflet sets ``title`` as a DOM property — plain text.
    expect(centre.opts.title).toBe(`Zone ${name}`)
    expect(centre.el.getAttribute('aria-label')).toBe(`Zone ${name}`)
    expect(centre.el.querySelector('img')).toBeNull()
    expect(centre.el.querySelector('b')).toBeNull()
    // The circle itself is decoration: no tooltip / popup of its own.
    const circle = vi.mocked(L.circle).mock.results[0].value
    expect(circle.bindTooltip).not.toHaveBeenCalled()
    expect(circle.bindPopup).not.toHaveBeenCalled()
  })

  it('reports map clicks in pick mode and hides the empty pane', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onPick = vi.fn()
    const { queryByText } = render(
      <LocationMap markers={[]} emptyLabel="Nothing here yet." onPick={onPick} />,
    )
    expect(queryByText('Nothing here yet.')).toBeNull()
    mapHandlers.click({ latlng: { lat: 52.1, lng: 4.2 } })
    expect(onPick).toHaveBeenCalledWith(52.1, 4.2)
  })
})

describe('LocationMap person and zone markers', () => {
  const home = {
    id: 'z-home', name: 'Home', latitude: 47, longitude: 8, radius_m: 200,
    color: null, icon: '🏠',
  }
  const inHome = (id: string, label = id.toUpperCase()) => ({
    id, label, lat: 47.0001, lon: 8.0001, avatar_url: null,
  })

  it('folds two people in one zone into one bubble with both avatars', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[inHome('a', 'Anna'), { ...inHome('b', 'Ben'), avatar_url: 'api/users/b/picture?v=1' }]}
        zones={[home]}
      />,
    )
    expect(bubbles()).toHaveLength(1)
    const [bubble] = bubbles()
    // Pinned to the zone centre, not a member's own point.
    expect(bubble.latlng).toEqual([47, 8])
    expect(count(bubble.opts.icon!.html, 'class="sh-map-avatar ')).toBe(2)
    expect(bubble.opts.icon!.html).toContain('src="api/users/b/picture?v=1"')
    expect(bubble.opts.icon!.html).toContain('>A<')
    expect(bubble.opts.title).toContain('Anna')
    expect(bubble.opts.title).toContain('Ben')
    expect(bubble.opts.keyboard).toBe(true)
  })

  it('caps a crowded zone at three avatars and a +N chip', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={['a', 'b', 'c', 'd', 'e'].map((id) => inHome(id))}
        zones={[home]}
      />,
    )
    const [bubble] = bubbles()
    expect(count(bubble.opts.icon!.html, 'class="sh-map-avatar ')).toBe(3)
    expect(bubble.opts.icon!.html).toContain('+2')
    for (const n of ['A', 'B', 'C', 'D', 'E']) expect(bubble.opts.title).toContain(n)
  })

  it('gives a person outside every zone a solo marker at their position', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[inHome('a'), { id: 'c', label: 'Cara', lat: 46, lon: 7 }]}
        zones={[home]}
      />,
    )
    expect(bubbles()).toHaveLength(2)
    const solo = bubbles().find((b) => b.latlng[0] === 46)!
    expect(solo.latlng).toEqual([46, 7])
    expect(solo.opts.icon!.html).toContain('sh-map-bubble--solo')
    expect(count(solo.opts.icon!.html, 'class="sh-map-avatar ')).toBe(1)
  })

  it('places zone-only members at their zone centre', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { queryByText } = render(
      <LocationMap
        markers={[{ id: 'a', label: 'Anna', zone_id: 'z-home' }]}
        zones={[home]}
        emptyLabel="Nobody"
      />,
    )
    expect(bubbles()).toHaveLength(1)
    expect(bubbles()[0].latlng).toEqual([47, 8])
    expect(queryByText('Nobody')).toBeNull()
  })

  it('frames each person in their own colour token', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { personColorToken } = await import('@/utils/mapMarkers')
    render(
      <LocationMap markers={[{ id: 'u1', user_id: 'uid-anna', label: 'Anna', lat: 1, lon: 2 }]} />,
    )
    expect(bubbles()[0].opts.icon!.html)
      .toContain(`sh-timetable-c--${personColorToken('uid-anna')}`)
  })

  it('marks each zone centre with its icon, or its first letter', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[]}
        zones={[home, { ...home, id: 'z-work', name: 'work', icon: null, latitude: 48 }]}
      />,
    )
    const html = zoneMarkers().map((m) => m.opts.icon!.html)
    expect(html).toHaveLength(2)
    expect(html[0]).toContain('🏠')
    expect(html[1]).toContain('>W<')
    expect(zoneMarkers()[0].latlng).toEqual([47, 8])
    expect(zoneMarkers()[0].opts.title).toContain('Home')
  })

  it('merges bubbles that would overlap once the map zooms out', async () => {
    const { render, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[
          { id: 'a', label: 'Anna', lat: 47, lon: 8 },
          { id: 'b', label: 'Ben', lat: 47, lon: 8.01 },
        ]}
      />,
    )
    expect(bubbles()).toHaveLength(2)
    view.scale = 1_000 // 0.01° → 10 px
    act(() => { mapHandlers.zoomend?.({}) })
    expect(bubbles()).toHaveLength(1)
    const [merged] = bubbles()
    expect(merged.opts.icon!.html).toContain('sh-map-bubble--group')
    expect(count(merged.opts.icon!.html, 'class="sh-map-avatar ')).toBe(2)
  })

  it('zooms to the zone on a first click of a crowded zone bubble', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(
      <LocationMap markers={[inHome('a'), inHome('b')]} zones={[home]} onSelect={onSelect} />,
    )
    fakeMap.fitBounds.mockClear()
    vi.mocked(L.latLngBounds).mockClear()
    bubbles()[0].handlers.click()
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    // The zone's whole circle: ±200 m around the centre.
    const [[s], [n]] = vi.mocked(L.latLngBounds).mock.calls[0][0] as [[number, number], [number, number]]
    expect(s).toBeLessThan(47)
    expect(n).toBeGreaterThan(47)
    // The zone is what's selected — nobody in particular yet.
    expect(onSelect).toHaveBeenCalledWith('zone:z-home')
    expect(mapPopups).toHaveLength(0)
  })

  it('selects a solo person on click and rings the selected avatar', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    const markers = [{ id: 'c', label: 'Cara', lat: 46, lon: 7, accuracy_m: 30 }]
    const { rerender } = render(
      <LocationMap markers={markers} selectedId={null} onSelect={onSelect} />,
    )
    expect(bubbles()[0].opts.icon!.html).not.toContain('is-selected')
    bubbles()[0].handlers.click()
    expect(onSelect).toHaveBeenCalledWith('c')
    rerender(<LocationMap markers={markers} selectedId="c" onSelect={onSelect} />)
    expect(bubbles()[0].opts.icon!.html).toContain('is-selected')
  })

  it('draws the accuracy ring only for the selected person', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const markers = [{ id: 'c', label: 'Cara', lat: 46, lon: 7, accuracy_m: 30 }]
    const radii = () => vi.mocked(L.circle).mock.calls
      .map((c) => (c[1] as unknown as { radius: number }).radius)
    const { rerender } = render(<LocationMap markers={markers} selectedId={null} />)
    expect(radii()).not.toContain(30)
    rerender(<LocationMap markers={markers} selectedId="c" />)
    expect(radii()).toContain(30)
  })

  it('selects internally when the caller does not control selection', async () => {
    const { render, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[{ id: 'c', label: 'Cara', lat: 46, lon: 7 }]} />)
    act(() => { bubbles()[0].handlers.click() })
    expect(bubbles()[0].opts.icon!.html).toContain('is-selected')
    act(() => { mapHandlers.click({ latlng: { lat: 0, lng: 0 } }) })
    expect(bubbles()[0].opts.icon!.html).not.toContain('is-selected')
  })

  it('reports a zone marker click as zone:<id>', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    const { rerender } = render(
      <LocationMap markers={[]} zones={[home]} selectedId={null} onSelect={onSelect} />,
    )
    zoneMarkers()[0].handlers.click()
    expect(onSelect).toHaveBeenCalledWith('zone:z-home')
    const L = (await import('leaflet')).default
    const circles = vi.mocked(L.circle).mock.calls.length
    const centre = zoneMarkers()[0].el
    rerender(<LocationMap markers={[]} zones={[home]} selectedId="zone:z-home" onSelect={onSelect} />)
    // Restyled in place — not rebuilt on every selection.
    expect(vi.mocked(L.circle).mock.calls.length).toBe(circles)
    expect(zoneMarkers()[0].el).toBe(centre)
    expect(centre.querySelector('.sh-map-zone')!.classList.contains('is-selected')).toBe(true)
  })

  it('deselects when the empty map is clicked', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(<LocationMap markers={[inHome('a')]} selectedId="a" onSelect={onSelect} />)
    mapHandlers.click({ latlng: { lat: 1, lng: 2 } })
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('does not deselect in pick mode', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(<LocationMap markers={[]} onPick={vi.fn()} onSelect={onSelect} />)
    mapHandlers.click({ latlng: { lat: 1, lng: 2 } })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('escapes names, zone icons and avatar URLs in marker HTML', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const evil = '"><img src=x onerror=alert(1)>'
    render(
      <LocationMap
        markers={[
          { id: 'a', label: `<b>${evil}`, lat: 46, lon: 7, avatar_url: `api/x${evil}` },
          { id: 'b', label: evil, lat: 46, lon: 9, sub_label: evil },
        ]}
        zones={[{ ...home, name: `<i>${evil}`, icon: evil }]}
        selectedId="b"
      />,
    )
    const all = [...bubbles(), ...zoneMarkers()]
    expect(all.length).toBeGreaterThanOrEqual(3)
    for (const m of all) {
      const el = document.createElement('div')
      el.innerHTML = m.opts.icon!.html
      // Only our own avatar <img> elements may exist, never an injected one.
      for (const img of el.querySelectorAll('img')) {
        expect(img.getAttribute('src')).toBe(`api/x${evil}`)
        expect(img.getAttribute('onerror')).toBeNull()
      }
      expect(el.querySelector('b')).toBeNull()
      expect(el.querySelector('i')).toBeNull()
    }
  })
})

describe('LocationMap viewport', () => {
  it('fits all bubble anchors with headroom for the bubbles above them', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[
          { id: 'a', label: 'Anna', lat: 47, lon: 8 },
          { id: 'b', label: 'Ben', lat: 47.1, lon: 8.1 },
        ]}
      />,
    )
    const opts = fakeMap.fitBounds.mock.calls.at(-1)![1] as {
      paddingTopLeft: [number, number], paddingBottomRight: [number, number],
    }
    // More room on top (bubbles rise above their anchor) than below.
    expect(opts.paddingTopLeft[1]).toBeGreaterThan(opts.paddingBottomRight[1])
  })

  it('fits the seeded family layout with fixed pixel padding at zone centres', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    // Four zones ~1.5 km apart, five people at Home, two at School, one
    // out and about — the layout of the visual check.
    const zone = (id: string, lat: number, lon: number, r: number) => ({
      id, name: id, latitude: lat, longitude: lon, radius_m: r, color: null,
    })
    const zones = [
      zone('home', 47.3769, 8.5417, 250), zone('school', 47.39, 8.515, 250),
      zone('work', 47.36, 8.53, 300), zone('gym', 47.366, 8.555, 200),
    ]
    const people = [
      ...['a', 'b', 'c', 'd', 'e'].map((id) => ({ id, label: id, lat: 47.377, lon: 8.5418 })),
      ...['f', 'g'].map((id) => ({ id, label: id, lat: 47.39, lon: 8.515 })),
      { id: 'h', label: 'h', lat: 47.37, lon: 8.508 },
    ]
    const bounds = { pad: vi.fn(), extend: vi.fn() }
    vi.mocked(L.latLngBounds).mockReturnValueOnce(bounds as never)
    render(<LocationMap markers={people} zones={zones} />)
    const anchors = vi.mocked(L.latLngBounds).mock.calls[0][0] as unknown as Array<[number, number]>
    // Zone centres, never the radius corners (a radius only matters once
    // zoomed to that zone).
    for (const z of zones) expect(anchors).toContainEqual([z.latitude, z.longitude])
    const lats = anchors.map((a) => a[0])
    expect(Math.max(...lats)).toBe(47.39)
    expect(Math.min(...lats)).toBe(47.36)
    const [b, opts] = fakeMap.fitBounds.mock.calls.at(-1)! as [unknown, {
      paddingTopLeft: [number, number], paddingBottomRight: [number, number],
      maxZoom: number, padding?: unknown,
    }]
    // The bounds go in as-is — no ``pad()`` inflation on top of the
    // pixel padding.
    expect(b).toBe(bounds)
    expect(bounds.pad).not.toHaveBeenCalled()
    // Left: the side gap plus room for the zoom buttons (34 + 10 px).
    expect(opts.paddingTopLeft).toEqual([32 + 44, 83])
    expect(opts.paddingBottomRight).toEqual([32, 30])
    expect(opts.maxZoom).toBe(16)
  })

  it('a read-only map has no zoom buttons, so no room is kept for them', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        readOnly
        markers={[
          { id: 'a', label: 'Anna', lat: 47, lon: 8 },
          { id: 'b', label: 'Ben', lat: 47.1, lon: 8.1 },
        ]}
      />,
    )
    const opts = fakeMap.fitBounds.mock.calls.at(-1)![1] as {
      paddingTopLeft: [number, number],
    }
    expect(opts.paddingTopLeft).toEqual([32, 83])
  })
})

describe('LocationMap crowded zones', () => {
  const home = {
    id: 'z-home', name: 'Home', latitude: 47, longitude: 8, radius_m: 200,
    color: null, icon: '🏠',
  }
  const at = (id: string, label: string) => ({ id, label, lat: 47.0001, lon: 8.0001 })

  it('lists the members as buttons once the zone already fills the view', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    const evil = '<img src=x onerror=alert(1)>'
    render(
      <LocationMap
        markers={[at('a', 'Anna'), at('b', evil), at('c', 'Cara'), at('d', 'Dan')]}
        zones={[home]}
        selectedId={null}
        onSelect={onSelect}
      />,
    )
    view.zoom = 16
    view.boundsZoom = 16
    view.contains = true
    fakeMap.fitBounds.mockClear()
    bubbles()[0].handlers.click()
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
    expect(onSelect).toHaveBeenCalledWith('zone:z-home')
    expect(mapPopups).toHaveLength(1)
    const buttons = [...mapPopups[0].querySelectorAll('button')]
    expect(buttons.map((b) => b.textContent)).toEqual(['Anna', evil, 'Cara', 'Dan'])
    expect(mapPopups[0].querySelector('img')).toBeNull()
    buttons[3].click()
    expect(onSelect).toHaveBeenLastCalledWith('d')
    expect(fakeMap.closePopup).toHaveBeenCalled()
  })

  it('zooms a merged zone + solo bubble first, then lists its members without selecting a zone', async () => {
    const { render, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(
      <LocationMap
        markers={[at('a', 'Anna'), { id: 'b', label: 'Ben', lat: 47, lon: 8.01 }]}
        zones={[home]}
        selectedId={null}
        onSelect={onSelect}
      />,
    )
    view.scale = 1_000
    act(() => { mapHandlers.zoomend?.({}) })
    const [merged] = bubbles()
    expect(merged.opts.icon!.html).toContain('sh-map-bubble--group')
    // Not yet fitted: zoom in.
    fakeMap.fitBounds.mockClear()
    merged.handlers.click()
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    expect(mapPopups).toHaveLength(0)
    // Fitted already and still merged: list who's in it.
    view.contains = true
    view.zoom = 16
    view.boundsZoom = 16
    fakeMap.fitBounds.mockClear()
    merged.handlers.click()
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
    expect([...mapPopups[0].querySelectorAll('button')].map((b) => b.textContent))
      .toEqual(['Anna', 'Ben'])
    // A merged bubble is no zone: nothing selected until a name is picked.
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('lists two people standing together once zooming in cannot split them', async () => {
    const { render, act } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    // ~5 m apart, no zones (the household presence page).
    render(
      <LocationMap
        markers={[
          { id: 'a', label: 'Anna', lat: 47.37690, lon: 8.54170 },
          { id: 'b', label: 'Ben', lat: 47.37694, lon: 8.54172 },
        ]}
        selectedId={null}
        onSelect={onSelect}
      />,
    )
    act(() => { mapHandlers.zoomend?.({}) })
    const [merged] = bubbles()
    expect(merged.opts.icon!.html).toContain('sh-map-bubble--group')
    view.zoom = 18
    view.boundsZoom = 22 // Leaflet would go further; capped at 18
    view.contains = true
    fakeMap.fitBounds.mockClear()
    vi.mocked(L.popup).mockClear()
    merged.handlers.click()
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
    // The popup floats above a solo-height bubble, not a zone bubble.
    const popupOpts = vi.mocked(L.popup).mock.calls[0][0] as { offset: [number, number] }
    expect(popupOpts.offset[1]).toBeGreaterThan(-80)
    const buttons = [...mapPopups[0].querySelectorAll('button')]
    expect(buttons.map((b) => b.textContent)).toEqual(['Anna', 'Ben'])
    buttons[1].click()
    expect(onSelect).toHaveBeenCalledWith('b')
  })

  it('names the selected member on the zone bubble even when hidden in +N', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[at('a', 'Anna'), at('b', 'Ben'), at('c', 'Cara'), at('d', 'Dan')]}
        zones={[home]}
        selectedId="d"
      />,
    )
    const el = document.createElement('div')
    el.innerHTML = bubbles()[0].opts.icon!.html
    expect(el.querySelector('.sh-map-bubble__caption')!.textContent).toBe('Dan')
  })

  it('draws no position dot for a merged bubble (its centroid is nowhere)', async () => {
    const { render, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[
          { id: 'a', label: 'Anna', lat: 47, lon: 8 },
          { id: 'b', label: 'Ben', lat: 47, lon: 8.01 },
        ]}
      />,
    )
    expect(bubbles()[0].opts.icon!.html).toContain('sh-map-bubble__dot')
    view.scale = 1_000
    act(() => { mapHandlers.zoomend?.({}) })
    expect(bubbles()[0].opts.icon!.html).not.toContain('sh-map-bubble__dot')
  })
})

describe('LocationMap keyboard', () => {
  it('activates a marker with Enter and with Space', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(
      <LocationMap
        markers={[{ id: 'c', label: 'Cara', lat: 46, lon: 7 }]}
        selectedId={null}
        onSelect={onSelect}
      />,
    )
    const el = bubbles()[0].el
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    el.dispatchEvent(enter)
    expect(onSelect).toHaveBeenCalledWith('c')
    expect(enter.defaultPrevented).toBe(true)
    onSelect.mockClear()
    const space = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true })
    el.dispatchEvent(space)
    expect(onSelect).toHaveBeenCalledWith('c')
    expect(space.defaultPrevented).toBe(true)
  })

  it('keeps keyboard focus on the same marker across a redraw', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const markers = [{ id: 'c', label: 'Cara', lat: 46, lon: 7 }]
    const { rerender } = render(<LocationMap markers={markers} selectedId={null} />)
    const before = bubbles()[0].el
    before.focus()
    expect(document.activeElement).toBe(before)
    rerender(<LocationMap markers={markers} selectedId="c" />)
    const after = bubbles()[0].el
    expect(after).not.toBe(before)
    expect(document.activeElement).toBe(after)
    expect(after.getAttribute('data-key')).toBe('person:c')
  })

  it('names the marker for screen readers and drops a photo that fails to load', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[{ id: 'c', label: 'Cara', sub_label: 'Away', lat: 46, lon: 7, avatar_url: 'api/x.png' }]}
      />,
    )
    const el = bubbles()[0].el
    expect(el.getAttribute('aria-label')).toBe('Cara · Away')
    const img = el.querySelector('img')!
    img.dispatchEvent(new Event('error'))
    expect(el.querySelector('img')).toBeNull()
    expect(el.querySelector('.sh-map-avatar__initials')!.textContent).toBe('C')
  })
})

describe('LocationMap zones', () => {
  const work = {
    id: 'z-work', name: 'Work', latitude: 47.3, longitude: 8.3, radius_m: 300,
    color: null, icon: '💼',
  }

  it('fits the view to the people and every zone centre', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[{ id: 'a', label: 'Anna', lat: 47, lon: 8 }]} zones={[work]} />)
    expect(fakeMap.fitBounds).toHaveBeenCalled()
    const anchors = vi.mocked(L.latLngBounds).mock.calls.at(-1)![0] as Array<[number, number]>
    const lats = anchors.map((a) => a[0])
    // Anna plus the Work zone's centre.
    expect(lats).toContain(47)
    expect(anchors).toContainEqual([47.3, 8.3])
    expect(fakeMap.setView).not.toHaveBeenCalled()
  })

  it('refits when the zones change', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const markers = [{ id: 'a', label: 'Anna', lat: 47, lon: 8 }]
    const { rerender } = render(<LocationMap markers={markers} zones={[work]} />)
    fakeMap.fitBounds.mockClear()
    rerender(<LocationMap markers={markers} zones={[work]} />)
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
    rerender(<LocationMap markers={markers} zones={[{ ...work, latitude: 47.5 }]} />)
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
  })

  it('still centres a single place without zones', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[{ id: 'p', label: 'Pin', lat: 1, lon: 2, glyph: '📍' }]} />)
    expect(fakeMap.setView).toHaveBeenCalledWith([1, 2], 14)
  })

  it('draws zone circles that let clicks through to the map', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(<LocationMap markers={[]} zones={[work]} selectedId="zone:z-work" onSelect={onSelect} />)
    const opts = vi.mocked(L.circle).mock.calls[0][1] as unknown as { interactive: boolean, fillOpacity: number }
    expect(opts.interactive).toBe(false)
    expect(opts.fillOpacity).toBeCloseTo(0.2)
    mapHandlers.click({ latlng: { lat: 47.3, lng: 8.3 } })
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('shrinks the zone marker while the zone circle is smaller than it', async () => {
    const { render, act } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[]} zones={[work]} />)
    const zone = () => zoneMarkers()[0].el.querySelector('.sh-map-zone')!
    // Zoom 4: a 300 m zone is a fraction of a pixel.
    expect(zone().classList.contains('sh-map-zone--compact')).toBe(true)
    view.zoom = 17 // ~0.8 m/px → a 300 m radius is hundreds of px
    act(() => { mapHandlers.zoomend?.({}) })
    expect(zone().classList.contains('sh-map-zone--compact')).toBe(false)
  })
})

describe('LocationMap read-only', () => {
  it('turns off every map interaction and makes markers inert', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const onSelect = vi.fn()
    render(
      <LocationMap
        readOnly
        markers={[{ id: 'c', label: 'Cara', lat: 46, lon: 7 }, { id: 'p', label: 'Pin', lat: 45, lon: 6, glyph: '📍' }]}
        zones={[{ id: 'z', name: 'Z', latitude: 46, longitude: 7, radius_m: 100, color: null }]}
        onSelect={onSelect}
      />,
    )
    const opts = vi.mocked(L.map).mock.calls[0][1] as Record<string, unknown>
    for (const k of ['dragging', 'touchZoom', 'doubleClickZoom', 'scrollWheelZoom', 'boxZoom', 'keyboard', 'zoomControl']) {
      expect(opts[k]).toBe(false)
    }
    for (const m of liveMarkers) {
      expect(m.opts.interactive).toBe(false)
      expect(m.opts.keyboard).toBe(false)
      expect(m.el.tabIndex).toBe(-1)
    }
    expect(popups).toHaveLength(0)
    mapHandlers.click?.({ latlng: { lat: 1, lng: 2 } })
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('LocationMap places', () => {
  it('reads a place pin out with its sub label', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[{ id: 'h', label: 'The Meiers', sub_label: '4 people · paired 2 d ago', lat: 47, lon: 8, glyph: '🏠' }]}
      />,
    )
    const [pin] = liveMarkers
    expect(pin.el.getAttribute('aria-label')).toBe('The Meiers · 4 people · paired 2 d ago')
  })
})

describe('LocationMap read-only attribution', () => {
  it('drops the attribution control and shows the licence as plain text', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    loadTileConfig.mockResolvedValue({
      tile_url: 'x', max_zoom: 19,
      attribution: '&copy; <a href="https://osm.org/copyright">OpenStreetMap</a> contributors <img src=x onerror=alert(1)>',
    })
    const { container } = render(
      <LocationMap readOnly markers={[{ id: 'c', label: 'Cara', lat: 46, lon: 7 }]} />,
    )
    const opts = vi.mocked(L.map).mock.calls[0][1] as Record<string, unknown>
    expect(opts.attributionControl).toBe(false)
    await waitFor(() => {
      expect(container.querySelector('.sh-location-map__attribution')).not.toBeNull()
    })
    const caption = container.querySelector('.sh-location-map__attribution')!
    expect(caption.textContent).toBe('© OpenStreetMap contributors')
    // No link inside the card's link, nothing injected.
    expect(caption.querySelector('a')).toBeNull()
    expect(caption.querySelector('img')).toBeNull()
  })

  it('keeps the attribution control on an interactive map', async () => {
    const { render } = await import('@testing-library/preact')
    const L = (await import('leaflet')).default
    const { LocationMap } = await import('./LocationMap')
    const { container } = render(<LocationMap markers={[]} />)
    const opts = vi.mocked(L.map).mock.calls[0][1] as Record<string, unknown>
    expect(opts.attributionControl).toBe(true)
    expect(container.querySelector('.sh-location-map__attribution')).toBeNull()
  })
})

describe('LocationMap fit insets', () => {
  const two = [
    { id: 'a', label: 'Anna', lat: 47, lon: 8 },
    { id: 'b', label: 'Ben', lat: 47.1, lon: 8.1 },
  ]
  type FitOpts = {
    paddingTopLeft: [number, number], paddingBottomRight: [number, number], maxZoom: number,
  }

  it('adds the insets of a panel over the map to the fit padding', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={two} fitInsets={{ bottom: 120, right: 10 }} />)
    const opts = fakeMap.fitBounds.mock.calls.at(-1)![1] as FitOpts
    // Same as without insets ([76, 83] / [32, 30]) plus the panel.
    expect(opts.paddingTopLeft).toEqual([32 + 44, 83])
    expect(opts.paddingBottomRight).toEqual([32 + 10, 30 + 120])
  })

  it('keeps a single anchor centred in the part of the map left visible', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[two[0]]} fitInsets={{ bottom: 120 }} />)
    expect(fakeMap.setView).toHaveBeenCalledWith([47, 8], 14)
    // Content moves up by half the covered strip.
    expect(fakeMap.panBy).toHaveBeenCalledWith([0, 60], { animate: false })
  })

  it('does not pan a single anchor without insets', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(<LocationMap markers={[two[0]]} />)
    expect(fakeMap.panBy).not.toHaveBeenCalled()
  })
})

describe('LocationMap zoom to selection', () => {
  const home = {
    id: 'z-home', name: 'Home', latitude: 47, longitude: 8, radius_m: 200,
    color: null, icon: '🏠',
  }
  const people = [
    { id: 'a', label: 'Anna', lat: 47.0001, lon: 8.0001 },
    { id: 'c', label: 'Cara', lat: 46.5, lon: 7.5 },
  ]
  type FitOpts = {
    paddingTopLeft: [number, number], paddingBottomRight: [number, number], maxZoom: number,
  }
  const lastBounds = async () => {
    const L = (await import('leaflet')).default
    return vi.mocked(L.latLngBounds).mock.calls.at(-1)![0] as unknown as Array<[number, number]>
  }

  it('zooms to street level on a selected person, clear of the panel', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId={null}
                   fitInsets={{ bottom: 200 }} />,
    )
    fakeMap.fitBounds.mockClear()
    rerender(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId="c"
                   fitInsets={{ bottom: 200 }} />,
    )
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    expect(await lastBounds()).toEqual([[46.5, 7.5], [46.5, 7.5]])
    const opts = fakeMap.fitBounds.mock.calls[0][1] as FitOpts
    expect(opts.maxZoom).toBe(17)
    expect(opts.paddingBottomRight[1]).toBe(30 + 200)
  })

  it('zooms a person inside a zone to where their bubble is drawn', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId={null} />,
    )
    rerender(<LocationMap markers={people} zones={[home]} zoomToSelection selectedId="a" />)
    // The zone centre — the shared bubble's anchor.
    expect(await lastBounds()).toEqual([[47, 8], [47, 8]])
  })

  it('fits a selected zone with its whole radius', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId={null} />,
    )
    fakeMap.fitBounds.mockClear()
    rerender(<LocationMap markers={people} zones={[home]} zoomToSelection selectedId="zone:z-home" />)
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    const [[s, w], [n, e]] = await lastBounds()
    expect(s).toBeLessThan(47)
    expect(n).toBeGreaterThan(47)
    expect(w).toBeLessThan(8)
    expect(e).toBeGreaterThan(8)
  })

  it('fits everyone again once the selection is cleared', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId="c" />,
    )
    fakeMap.fitBounds.mockClear()
    rerender(<LocationMap markers={people} zones={[home]} zoomToSelection selectedId={null} />)
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    expect((fakeMap.fitBounds.mock.calls[0][1] as FitOpts).maxZoom).toBe(16)
  })

  it('stays on the selected person when positions update', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId="c" />,
    )
    fakeMap.fitBounds.mockClear()
    const moved = [people[0], { ...people[1], lat: 46.6 }]
    rerender(<LocationMap markers={moved} zones={[home]} zoomToSelection selectedId="c" />)
    expect(await lastBounds()).toEqual([[46.6, 7.5], [46.6, 7.5]])
  })

  it('waits for a running zoom to end before moving the view again', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId="c" />,
    )
    fakeMap.fitBounds.mockClear()
    // The selection started zooming; then the sheet over the map grew.
    mapHandlers.zoomstart({})
    rerender(
      <LocationMap markers={people} zones={[home]} zoomToSelection selectedId="c"
                   fitInsets={{ bottom: 240 }} />,
    )
    // Leaflet would drop a view change mid-animation — so none is made.
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
    mapHandlers.zoomend({})
    expect(fakeMap.fitBounds).toHaveBeenCalledTimes(1)
    const opts = fakeMap.fitBounds.mock.calls[0][1] as FitOpts
    expect(opts.paddingBottomRight[1]).toBe(30 + 240)
    expect(await lastBounds()).toEqual([[46.5, 7.5], [46.5, 7.5]])
  })

  it('leaves the view alone on selection without zoomToSelection', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const { rerender } = render(<LocationMap markers={people} zones={[home]} selectedId={null} />)
    fakeMap.fitBounds.mockClear()
    rerender(<LocationMap markers={people} zones={[home]} selectedId="c" />)
    rerender(<LocationMap markers={people} zones={[home]} selectedId={null} />)
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
  })

  it('a zone bubble click leaves the zoom to the selection', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    const two = [people[0], { id: 'b', label: 'Ben', lat: 47.0002, lon: 8.0002 }]
    const onSelect = vi.fn()
    render(
      <LocationMap markers={two} zones={[home]} zoomToSelection selectedId={null}
                   onSelect={onSelect} />,
    )
    fakeMap.fitBounds.mockClear()
    bubbles()[0].handlers.click()
    expect(onSelect).toHaveBeenCalledWith('zone:z-home')
    expect(fakeMap.fitBounds).not.toHaveBeenCalled()
  })
})
