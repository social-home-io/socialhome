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
/** Every tooltip / popup content handed to a zone circle, in order. */
const zoneOverlays: unknown[] = []
/** Pixels per degree for the fake projection — lower it to "zoom out". */
const view = { scale: 100_000 }
interface FakeMarker {
  latlng: [number, number]
  opts: { icon?: { html: string }, title?: string, keyboard?: boolean }
  handlers: Record<string, (e?: unknown) => void>
}
/** Markers currently on a layer (cleared with ``clearLayers``). */
let liveMarkers: FakeMarker[] = []
const fakeMap = {
  remove: vi.fn(),
  invalidateSize: vi.fn(),
  setView: vi.fn(),
  fitBounds: vi.fn(),
  getZoom: vi.fn(() => 4),
  getSize: vi.fn(() => ({ x: 800, y: 400 })),
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
          liveMarkers = liveMarkers.filter((m) => !mine.includes(m))
          mine.length = 0
        }),
        _track: (m: FakeMarker) => { mine.push(m); liveMarkers.push(m) },
      }
      return g
    }),
    marker: vi.fn((latlng: [number, number], opts: FakeMarker['opts']) => {
      const fm: FakeMarker = { latlng, opts, handlers: {} }
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
        getElement: vi.fn(() => undefined),
      }
      return m
    }),
    circle: vi.fn(() => {
      const bounds = { getSouthWest: vi.fn(), getNorthEast: vi.fn() }
      const c = {
        addTo: vi.fn(() => c),
        bindTooltip: vi.fn((content: unknown) => { zoneOverlays.push(content); return c }),
        bindPopup: vi.fn((content: unknown) => { zoneOverlays.push(content); return c }),
        getBounds: vi.fn(() => bounds),
      }
      return c
    }),
    divIcon: vi.fn((opts: unknown) => opts),
    latLngBounds: vi.fn(() => {
      const b = { pad: vi.fn(() => ({})), extend: vi.fn(() => b) }
      return b
    }),
  },
}))

/** HTML of every bubble (person / group) marker currently drawn. */
function bubbles(): FakeMarker[] {
  return liveMarkers.filter((m) => m.opts.icon?.html.includes('sh-map-bubble'))
}
/** HTML of every zone-centre marker currently drawn. */
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
vi.mock('@/utils/mapTiles', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/mapTiles')>()),
  get addTileLayer() { return addTileLayer },
}))

beforeEach(() => {
  vi.clearAllMocks()
  liveMarkers = []
  view.scale = 100_000
  popups.length = 0
  zoneOverlays.length = 0
  for (const k of Object.keys(mapHandlers)) delete mapHandlers[k]
  addTileLayer.mockReset()
  addTileLayer.mockResolvedValue(undefined)
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

  it('shows a hostile zone name literally in the zone tooltip and popup', async () => {
    const { render } = await import('@testing-library/preact')
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
    expect(zoneOverlays).toHaveLength(2)
    for (const content of zoneOverlays) {
      // Mount the way Leaflet's ``DivOverlay._updateContent`` does:
      // a string goes through ``innerHTML``.
      const el = document.createElement('div')
      if (typeof content === 'string') el.innerHTML = content
      else el.appendChild(content as Node)
      expect(el.querySelector('img')).toBeNull()
      expect(el.querySelector('b')).toBeNull()
      expect(el.textContent).toBe(name)
    }
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

  it('zooms in to fit the members when a group bubble is clicked', async () => {
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
    expect(onSelect).not.toHaveBeenCalled()
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
    rerender(<LocationMap markers={[]} zones={[home]} selectedId="zone:z-home" onSelect={onSelect} />)
    expect(zoneMarkers()[0].opts.icon!.html).toContain('is-selected')
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

  it('caps the headroom on a short map so the anchors still fit', async () => {
    fakeMap.getSize.mockReturnValue({ x: 360, y: 160 })
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
    expect(opts.paddingTopLeft[1] + opts.paddingBottomRight[1]).toBeLessThan(160 * 0.7)
    fakeMap.getSize.mockReturnValue({ x: 800, y: 400 })
  })
})
