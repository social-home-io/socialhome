/**
 * FederationMap component tests.
 *
 * Leaflet is mocked so jsdom doesn't have to implement a real canvas +
 * tile fetch pipeline.  Assertions focus on the JSX output (container
 * element, "Not on map" footer, peer listings) rather than Leaflet API
 * calls.
 */
import { describe, test, expect, beforeEach, vi, beforeAll } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/preact'

// ResizeObserver is used by FederationMap to handle hidden-tab reflow.
beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

// Stub the CSS import so Vite/jsdom doesn't choke on it.
vi.mock('leaflet/dist/leaflet.css', () => ({}))

// Vanilla-Leaflet mock — returns chainable stubs so the useEffect
// initialisation doesn't throw.
vi.mock('leaflet', () => {
  const layerGroupStub = {
    addTo: vi.fn().mockReturnThis(),
    clearLayers: vi.fn().mockReturnThis(),
    getBounds: vi.fn(() => ({ pad: vi.fn(() => ({})) })),
  }
  const markerStub = {
    addTo: vi.fn().mockReturnThis(),
    bindPopup: vi.fn().mockReturnThis(),
    getLatLng: vi.fn(() => ({ lat: 0, lng: 0 })),
  }
  const mapStub = {
    remove: vi.fn(),
    invalidateSize: vi.fn(),
    setView: vi.fn().mockReturnThis(),
    fitBounds: vi.fn().mockReturnThis(),
    addTo: vi.fn().mockReturnThis(),
  }
  return {
    default: {
      map: vi.fn(() => mapStub),
      tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
      layerGroup: vi.fn(() => layerGroupStub),
      marker: vi.fn(() => markerStub),
      divIcon: vi.fn(() => ({})),
      featureGroup: vi.fn(() => layerGroupStub),
      latLngBounds: vi.fn(() => ({ pad: vi.fn(() => ({})) })),
    },
  }
})

// Tiles come from the backend proxy (``/api/map/config``) through the
// shared helper; stub it so the test needs no fetch.
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

import { connections, selfLat, selfLon } from '@/store/connections'
import FederationMap from './FederationMap'

describe('FederationMap', () => {
  beforeEach(() => {
    selfLat.value = null
    selfLon.value = null
    connections.value = []
    addTileLayer.mockReset()
    addTileLayer.mockResolvedValue(undefined)
  })

  test('renders the map container element', () => {
    render(<FederationMap />)
    expect(screen.getByTestId('sh-federation-map')).toBeDefined()
  })

  test('renders without crashing when no coords are present', () => {
    render(<FederationMap />)
    // No footer when no peers
    expect(screen.queryByText('Not on map')).toBeNull()
  })

  test('shows "Not on map" footer for peers without home coords', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Bob',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
    ]
    render(<FederationMap />)
    expect(screen.getByText('Not on map')).toBeDefined()
    expect(screen.getByText('Bob')).toBeDefined()
    expect(screen.getByText('Paired but no home coordinates yet.')).toBeDefined()
  })

  test('omits the footer when every peer has coords', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Bob',
        reachable: true,
        home_lat: 52.52,
        home_lon: 13.40,
      },
    ]
    render(<FederationMap />)
    expect(screen.queryByText('Not on map')).toBeNull()
  })

  test('lists multiple peers without coords in the footer', () => {
    connections.value = [
      {
        instance_id: 'p1',
        display_name: 'Alice',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
      {
        instance_id: 'p2',
        display_name: 'Carol',
        reachable: true,
        home_lat: null,
        home_lon: null,
      },
      {
        instance_id: 'p3',
        display_name: 'Dave',
        reachable: true,
        home_lat: 48.85,
        home_lon: 2.35,
      },
    ]
    render(<FederationMap />)
    expect(screen.getByText('Alice')).toBeDefined()
    expect(screen.getByText('Carol')).toBeDefined()
    // Dave has coords — should NOT appear in the footer
    // (he will be on the map, not in the footer list)
    const footer = screen.getByTestId('sh-federation-map').querySelector(
      '.sh-federation-map__footer',
    )
    expect(footer).not.toBeNull()
    // Footer rows: Alice + Carol only
    const rows = footer!.querySelectorAll('.sh-federation-map__footer-row')
    expect(rows.length).toBe(2)
  })

  test('uses instance_id as fallback when display_name is absent', () => {
    connections.value = [
      {
        instance_id: 'some-uuid',
        display_name: undefined,
        reachable: true,
        home_lat: null,
        home_lon: null,
      } as never,
    ]
    render(<FederationMap />)
    expect(screen.getByText('some-uuid')).toBeDefined()
  })

  test('shows a tiles-unavailable message when the tile config fails', async () => {
    addTileLayer.mockRejectedValue(new Error('API 502: /api/map/config'))
    render(<FederationMap />)
    await waitFor(() => {
      expect(screen.getByText(/Map unavailable/)).toBeDefined()
    })
  })
  test('surfaces a tile-load failure the same way as a config failure', async () => {
    let report: (() => void) | undefined
    addTileLayer.mockImplementation((_map, _cancelled, onError) => {
      report = onError
      return Promise.resolve()
    })
    render(<FederationMap />)

    await waitFor(() => { expect(report).toBeTypeOf('function') })
    act(() => { report!() })

    await waitFor(() => {
      expect(screen.getByText(/Map unavailable/)).toBeDefined()
    })
  })
})
