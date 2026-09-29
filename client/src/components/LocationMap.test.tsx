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
vi.mock('leaflet', () => ({
  default: {
    map: vi.fn(() => ({
      remove: vi.fn(),
      invalidateSize: vi.fn(),
      setView: vi.fn(),
      fitBounds: vi.fn(),
      getZoom: vi.fn(() => 4),
      on: vi.fn((name: string, fn: (e: unknown) => void) => {
        mapHandlers[name] = fn
      }),
    })),
    tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
    layerGroup: vi.fn(() => {
      const g = { addTo: vi.fn(() => g), clearLayers: vi.fn() }
      return g
    }),
    marker: vi.fn(() => {
      const m = {
        addTo: vi.fn(() => m),
        bindPopup: vi.fn((html: string) => { popups.push(html) }),
      }
      return m
    }),
    circle: vi.fn(() => ({ addTo: vi.fn() })),
    divIcon: vi.fn(),
    latLngBounds: vi.fn(() => ({ pad: vi.fn(() => ({})) })),
  },
}))

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
  popups.length = 0
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

  it('escapes a marker label before it reaches the popup HTML', async () => {
    const { render } = await import('@testing-library/preact')
    const { LocationMap } = await import('./LocationMap')
    render(
      <LocationMap
        markers={[{
          id: 'x', lat: 1, lon: 2,
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

describe('escapeHtml', () => {
  it('escapes the five HTML-significant characters', async () => {
    const { escapeHtml } = await import('./LocationMap')
    expect(escapeHtml(`<a href="x">'&'</a>`)).toBe(
      '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;',
    )
  })
})
