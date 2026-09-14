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
vi.mock('leaflet', () => ({
  default: {
    map: vi.fn(() => ({
      remove: vi.fn(),
      invalidateSize: vi.fn(),
      setView: vi.fn(),
      fitBounds: vi.fn(),
    })),
    tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
    layerGroup: vi.fn(() => ({ addTo: vi.fn(), clearLayers: vi.fn() })),
    marker: vi.fn(() => ({ addTo: vi.fn(), bindPopup: vi.fn() })),
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
})
