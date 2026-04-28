/**
 * SpaceLocationCard tests.
 *
 * Two modes are exercised:
 *   * ``location_mode === 'gps'`` — markers handed to LocationMap.
 *   * ``location_mode === 'zone_only'`` — flat list of zone labels;
 *     LocationMap is not rendered (the originating instance has
 *     already matched GPS to zones server-side).
 */
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  // localStorage stub so the first-visit onboarding modal logic
  // doesn't blow up in jsdom.
  const store = new Map<string, string>()
  const ls = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
    removeItem: (k: string) => { store.delete(k) },
    clear: () => { store.clear() },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    length: 0,
  }
  Object.defineProperty(globalThis, 'localStorage', {
    value: ls, writable: false,
  })
})

vi.mock('leaflet', () => {
  const noop = () => undefined
  const layer = new Proxy({} as any, {
    get(_t, prop) {
      if (prop === 'addTo' || prop === 'bindTooltip') return () => layer
      return noop
    },
  })
  const map = new Proxy({} as any, {
    get(_t, prop) {
      if (
        prop === 'on' || prop === 'addTo' || prop === 'setView'
        || prop === 'fitBounds' || prop === 'invalidateSize'
        || prop === 'remove' || prop === 'clearLayers'
      ) return () => map
      return noop
    },
  })
  return {
    default: {
      map: () => map,
      tileLayer: () => layer,
      layerGroup: () => layer,
      circle: () => layer,
      marker: () => layer,
      latLngBounds: () => ({ pad: () => ({}) }),
      divIcon: () => ({}),
    },
  }
})
vi.mock('leaflet/dist/leaflet.css', () => ({}))

const mockApi = {
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}
vi.mock('@/api', () => ({
  get api() { return mockApi },
}))

vi.mock('@/ws', () => {
  const m = { on: vi.fn(() => () => undefined) }
  return { ws: m, _mock: m }
})

vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/i18n/i18n', () => ({
  t: (k: string) => k,
  locale: { value: 'en' },
}))

import { SpaceLocationCard } from './SpaceLocationCard'
import { ws as wsModule } from '@/ws'
const mockWs = wsModule as unknown as { on: ReturnType<typeof vi.fn> }


describe('SpaceLocationCard', () => {
  beforeEach(() => {
    mockApi.get.mockReset()
    mockApi.patch.mockReset()
    mockWs.on.mockClear()
  })

  it('renders the off banner when feature is disabled', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({ feature_enabled: false, entries: [] })
      }
      return Promise.resolve({ zones: [] })
    })
    const { findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText(/Location sharing is off/i)
  })

  it('renders the GPS map when location_mode === "gps"', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [
            {
              user_id: 'u_pascal',
              username: 'pascal',
              display_name: 'Pascal',
              state: 'home',
              latitude: 47.3769,
              longitude: 8.5417,
              gps_accuracy_m: 10,
              picture_url: null,
            },
          ],
        })
      }
      return Promise.resolve({ zones: [] })
    })
    const { container, findByText } = render(
      <SpaceLocationCard spaceId="sp_test" />,
    )
    await findByText(/sharing GPS/i)
    // The Leaflet container is rendered (sh-location-map class is on the map widget).
    expect(container.querySelector('.sh-location-map')).toBeTruthy()
    // The zone-only list is NOT rendered.
    expect(container.querySelector('[data-testid="zone-only-list"]')).toBeNull()
  })

  it('renders a zone-label list when location_mode === "zone_only"', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'zone_only',
          entries: [
            {
              user_id: 'u_pascal',
              username: 'pascal',
              display_name: 'Pascal',
              state: 'zone',
              zone_id: 'z_office',
              zone_name: 'Office',
              picture_url: null,
            },
            {
              user_id: 'u_anna',
              username: 'anna',
              display_name: 'Anna',
              state: 'zone',
              zone_id: 'z_workshop',
              zone_name: 'The Workshop',
              picture_url: null,
            },
          ],
        })
      }
      return Promise.resolve({
        zones: [
          {
            id: 'z_office',
            space_id: 'sp_test',
            name: 'Office',
            latitude: 47.3769,
            longitude: 8.5417,
            radius_m: 200,
            color: '#3b82f6',
            created_by: 'u_admin',
            created_at: '2026-04-28T00:00:00Z',
            updated_at: '2026-04-28T00:00:00Z',
          },
        ],
      })
    })
    const { container, findByText } = render(
      <SpaceLocationCard spaceId="sp_test" />,
    )
    await findByText('Office')
    await findByText('The Workshop')
    // The flat list is rendered, the Leaflet map is NOT.
    expect(
      container.querySelector('[data-testid="zone-only-list"]'),
    ).toBeTruthy()
    expect(container.querySelector('.sh-location-map')).toBeNull()
    // Footer counts members in zones.
    await findByText(/2 of 2 in a zone/i)
  })

  it('subscribes to space_zone_changed WS events', async () => {
    mockApi.get.mockResolvedValue({
      feature_enabled: true, location_mode: 'gps', entries: [],
    })
    render(<SpaceLocationCard spaceId="sp_test" />)
    await waitFor(() => expect(mockWs.on).toHaveBeenCalled())
    expect(
      mockWs.on.mock.calls.some((c: any[]) => c[0] === 'space_zone_changed'),
    ).toBe(true)
  })
})
