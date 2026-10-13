/**
 * SpaceLocationCard tests.
 *
 * Two modes are exercised:
 *   * ``location_mode === 'gps'`` — markers handed to LocationMap.
 *   * ``location_mode === 'zone_only'`` — members drawn as bubbles at
 *     their zone centres (no coordinates exist — the originating
 *     instance already matched GPS to zones).
 *
 * Both list the members and zones in the map overview beside the map.
 */
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/preact'

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

const EMPTY_ZONE_TEXT = 'No one in this space is in any zone right now.'
/** HTML of every divIcon the map built (bubbles + zone markers). */
const icons: string[] = []
/** Every marker ``title`` (the accessible name / hover text). */
const titles: string[] = []
vi.mock('leaflet', () => {
  const noop = () => undefined
  const layer = new Proxy({} as any, {
    get(_t, prop) {
      if (
        prop === 'addTo' || prop === 'bindTooltip' || prop === 'bindPopup'
        || prop === 'on'
      ) return () => layer
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
      if (prop === 'latLngToContainerPoint') {
        return (ll: [number, number]) => ({ x: ll[1] * 1e5, y: -ll[0] * 1e5 })
      }
      return noop
    },
  })
  return {
    default: {
      map: () => map,
      tileLayer: () => layer,
      layerGroup: () => layer,
      circle: () => layer,
      marker: (_ll: unknown, opts?: { title?: string }) => {
        if (opts?.title) titles.push(opts.title)
        return layer
      },
      latLngBounds: () => ({ pad: () => ({}) }),
      divIcon: (o: { html: string }) => { icons.push(o.html); return {} },
    },
  }
})
vi.mock('leaflet/dist/leaflet.css', () => ({}))

// LocationMap pulls its tile layer from the backend proxy through the
// shared helper — stub it out so no ``/api/map/config`` call happens.
vi.mock('@/utils/mapTiles', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/mapTiles')>()),
  addTileLayer: vi.fn(async () => {}),
}))

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
// Real i18n: the card's copy comes from the English catalog, so the
// assertions below read the English text users see.

import { SpaceLocationCard } from './SpaceLocationCard'
import { ws as wsModule } from '@/ws'
const mockWs = wsModule as unknown as { on: ReturnType<typeof vi.fn> }


/** Rows listed in the map overview (people or zones). */
const overviewRows = (c: Element) =>
  Array.from(c.querySelectorAll<HTMLElement>('.sh-map-overview__body .sh-map-overview__row'))
const rowName = (r: HTMLElement) => r.querySelector('.sh-map-overview__row-name')!.textContent
const rowSub = (r: HTMLElement) => r.querySelector('.sh-map-overview__row-sub')?.textContent ?? null
/** The overview's People / Zones switch. */
const tabs = (c: Element) =>
  Array.from(c.querySelectorAll<HTMLButtonElement>('.sh-map-overview__tabs button'))

describe('SpaceLocationCard', () => {
  beforeEach(() => {
    icons.length = 0
    titles.length = 0
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
    // The member is listed beside the map.
    expect(overviewRows(container).map(rowName)).toEqual(['Pascal'])
  })

  it('lists zone-only members in the overview with their zone', async () => {
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
    await findByText('Pascal')
    // A zone that isn't in the catalogue (deleted) still names it.
    expect(overviewRows(container).map(rowSub)).toEqual(['The Workshop', 'Office'])
    expect(container.querySelector('.sh-location-map')).toBeTruthy()
    // Footer counts members in zones.
    await findByText(/2 of 2 in a zone/i)
  })

  const office = {
    id: 'z_office', space_id: 'sp_test', name: 'Office',
    latitude: 47.3769, longitude: 8.5417, radius_m: 200,
    color: '#3b82f6', icon: '🏢',
    created_by: 'u_admin',
    created_at: '2026-04-28T00:00:00Z', updated_at: '2026-04-28T00:00:00Z',
  }

  it('draws zone-only members as one bubble at their zone centre', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'zone_only',
          entries: ['Pascal', 'Anna'].map((n) => ({
            user_id: `u_${n}`, username: n, display_name: n, state: 'zone',
            zone_id: 'z_office', zone_name: 'Office', picture_url: null,
          })),
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    render(<SpaceLocationCard spaceId="sp_test" />)
    await waitFor(() => {
      expect(icons.some((h) => h.includes('sh-map-bubble--zone'))).toBe(true)
    })
    const bubble = icons.filter((h) => h.includes('sh-map-bubble--zone')).at(-1)!
    expect(bubble.split('class="sh-map-avatar ').length - 1).toBe(2)
    expect(icons.some((h) => h.includes('sh-map-zone') && h.includes('🏢'))).toBe(true)
  })

  it('groups GPS members inside a zone into the zone bubble', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [
            {
              user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
              state: 'home', latitude: 47.3770, longitude: 8.5418,
              gps_accuracy_m: 10, picture_url: 'api/users/u_pascal/picture?v=1',
            },
            {
              user_id: 'u_anna', username: 'anna', display_name: 'Anna',
              state: 'away', latitude: 46.0, longitude: 7.0,
              gps_accuracy_m: 10, picture_url: null,
            },
          ],
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    render(<SpaceLocationCard spaceId="sp_test" />)
    await waitFor(() => {
      expect(icons.some((h) => h.includes('sh-map-bubble--zone'))).toBe(true)
    })
    const zoneBubble = icons.filter((h) => h.includes('sh-map-bubble--zone')).at(-1)!
    expect(zoneBubble).toContain('api/users/u_pascal/picture?v=1')
    expect(icons.some((h) => h.includes('sh-map-bubble--solo'))).toBe(true)
  })

  it('labels a member outside every zone by their state, in words', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [
            {
              user_id: 'u_local', username: 'anna', display_name: 'Anna',
              state: 'not_home', latitude: 46.0, longitude: 7.0, picture_url: null,
            },
            {
              // Another household's pin: the server can't know its state.
              user_id: 'u_remote', username: null, display_name: 'Rita',
              state: 'home', latitude: 45.0, longitude: 6.0, picture_url: null,
              instance_id: 'peer-1',
            },
          ],
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    render(<SpaceLocationCard spaceId="sp_test" />)
    await waitFor(() => { expect(titles).toContain('Anna · Not home') })
    expect(titles).toContain('Rita')
    expect(titles.some((t) => t.startsWith('Rita ·'))).toBe(false)
  })

  it('words each member in the overview: zone, state, or nothing for another household', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [
            {
              user_id: 'u_local', username: 'anna', display_name: 'Anna',
              state: 'not_home', latitude: 46.0, longitude: 7.0, picture_url: null,
            },
            {
              user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
              state: 'home', latitude: 47.3770, longitude: 8.5418, picture_url: null,
              updated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
            },
            {
              user_id: 'u_remote', username: null, display_name: 'Rita',
              state: 'home', latitude: 45.0, longitude: 6.0, picture_url: null,
              instance_id: 'peer-1',
            },
          ],
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText('Rita')
    expect(overviewRows(container).map(rowName)).toEqual(['Anna', 'Pascal', 'Rita'])
    expect(overviewRows(container).map(rowSub)).toEqual(['Not home', 'Office · 5 min ago', null])
    // Zones tab: Pascal is the one in the office.
    fireEvent.click(tabs(container)[1])
    expect(overviewRows(container).map((r) => r.querySelector('.sh-map-overview__count')!.textContent))
      .toEqual(['1 person'])
  })

  it('shares one selection between the overview and the map', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [{
            user_id: 'u_anna', username: 'anna', display_name: 'Anna',
            state: 'not_home', latitude: 46.0, longitude: 7.0, picture_url: null,
          }],
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText('Anna')
    icons.length = 0
    fireEvent.click(overviewRows(container)[0])
    expect(container.querySelector('h3.sh-map-overview__title')!.textContent).toBe('Anna')
    // The map redrew Anna's bubble as selected, named in its caption.
    expect(icons.some((h) => h.includes('sh-map-bubble--solo') && h.includes('is-selected'))).toBe(true)
  })

  it('shows no "nobody in a zone" text when members exist but their zones are unknown', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'zone_only',
          entries: [{
            user_id: 'u_local', username: 'anna', display_name: 'Anna', state: 'zone',
            zone_id: 'z_gone', zone_name: 'Old office', picture_url: null,
          }],
        })
      }
      return Promise.reject(new Error('zones down'))
    })
    const { findByText, queryByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText('Anna')
    expect(queryByText(EMPTY_ZONE_TEXT)).toBeNull()
  })

  it('shows the empty text once when nobody is in a zone', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true, location_mode: 'zone_only', entries: [],
        })
      }
      return Promise.resolve({ zones: [office] })
    })
    const { findAllByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    const hits = await findAllByText(EMPTY_ZONE_TEXT)
    expect(hits).toHaveLength(1)
  })

  it('never lets a hostile zone colour inject extra CSS into the zone badge', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'zone_only',
          entries: [{
            user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
            state: 'zone', zone_id: 'z_office', zone_name: 'Office', picture_url: null,
          }],
        })
      }
      return Promise.resolve({
        zones: [{
          id: 'z_office', space_id: 'sp_test', name: 'Office',
          latitude: 47.3769, longitude: 8.5417, radius_m: 200,
          color: 'red;position:fixed;inset:0;background-image:url(https://evil.example/b)',
          created_by: 'u_admin', created_at: '2026-04-28T00:00:00Z',
          updated_at: '2026-04-28T00:00:00Z',
        }],
      })
    })
    const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText('Pascal')
    fireEvent.click(tabs(container)[1])
    const chip = container.querySelector('.sh-map-overview__zone-badge') as HTMLElement
    expect(chip.style.position).toBe('')
    expect(chip.style.backgroundImage).not.toContain('url')
    expect(chip.getAttribute('style') ?? '').not.toContain('evil')
    expect(chip.style.getPropertyValue('--zone-colour')).toMatch(/^#[0-9a-f]{6}$/i)
  })

  it('shows the zone icon on its row in the overview', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'zone_only',
          entries: [{
            user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
            state: 'zone', zone_id: 'z_school', zone_name: 'School', picture_url: null,
          }],
        })
      }
      return Promise.resolve({
        zones: [{
          id: 'z_school', space_id: 'sp_test', name: 'School',
          latitude: 47.3769, longitude: 8.5417, radius_m: 200,
          color: '#3b82f6', icon: '🏫',
          created_by: 'u_admin', created_at: '2026-04-28T00:00:00Z',
          updated_at: '2026-04-28T00:00:00Z',
        }],
      })
    })
    const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
    await findByText('Pascal')
    fireEvent.click(tabs(container)[1])
    const row = overviewRows(container)[0]
    const icon = row.querySelector('.sh-map-overview__zone-badge') as HTMLElement
    expect(icon.textContent).toBe('🏫')
    expect(icon.getAttribute('aria-hidden')).toBe('true')
    expect(rowName(row)).toBe('School')
  })

  it('hydrates the sharing chip from /members on cold load', async () => {
    // Regression for "I shared my location, came back, and the map
    // page rendered as if it was reset" — the chip's ``sharingMe``
    // state derives from the ``location_share_enabled`` field on the
    // viewer's row in /members. Pre-fix the backend dropped the
    // field, so the chip always rendered OFF on re-entry even
    // though the row was actually enabled.
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [],
        })
      }
      if (url.includes('/members')) {
        return Promise.resolve([
          {
            user_id: 'u_pascal',
            role: 'owner',
            location_share_enabled: true,
          },
        ])
      }
      return Promise.resolve({ zones: [] })
    })
    const { findByText, queryByText } = render(
      <SpaceLocationCard spaceId="sp_test" currentUserId="u_pascal" />,
    )
    // The "you are sharing" copy shows up, NOT the "private here" copy.
    await findByText(/You are sharing your location/i)
    expect(queryByText(/Your location is private here/i)).toBeNull()
  })

  it('renders the OFF chip when the viewer has not opted in', async () => {
    mockApi.get.mockImplementation((url: string) => {
      if (url.includes('/presence')) {
        return Promise.resolve({
          feature_enabled: true,
          location_mode: 'gps',
          entries: [],
        })
      }
      if (url.includes('/members')) {
        return Promise.resolve([
          {
            user_id: 'u_pascal',
            role: 'owner',
            location_share_enabled: false,
          },
        ])
      }
      return Promise.resolve({ zones: [] })
    })
    const { findByText } = render(
      <SpaceLocationCard spaceId="sp_test" currentUserId="u_pascal" />,
    )
    await findByText(/Your location is private here/i)
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

  describe('live space_zone_changed upserts', () => {
    const zone = (id: string, name: string, icon: string | null = null) => ({
      id, space_id: 'sp_test', name,
      latitude: 47.3769, longitude: 8.5417, radius_m: 200,
      color: '#3b82f6', icon,
      created_by: 'u_admin', created_at: '2026-04-28T00:00:00Z',
      updated_at: '2026-04-28T00:00:00Z',
    })
    const zoneHandler = () => {
      const call = mockWs.on.mock.calls.find((c: unknown[]) => c[0] === 'space_zone_changed')
      return (call as unknown as [string, (e: unknown) => void])[1]
    }
    const upsert = (z: ReturnType<typeof zone>) => ({
      type: 'space_zone_changed',
      data: { space_id: 'sp_test', action: 'upsert', zone_id: z.id, zone: z },
    })

    it('lists a newly upserted zone in name order in the overview', async () => {
      mockApi.get.mockImplementation((url: string) => {
        if (url.includes('/presence')) {
          return Promise.resolve({ feature_enabled: true, location_mode: 'gps', entries: [] })
        }
        return Promise.resolve({ zones: [zone('z_office', 'Office')] })
      })
      const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
      await findByText('Office')
      act(() => { zoneHandler()(upsert(zone('z_aardvark', 'Aardvark', '🐜'))) })
      await findByText('Aardvark')
      expect(overviewRows(container).map(rowName)).toEqual(['Aardvark', 'Office'])
    })

    it('shows the new icon in the overview when a zone is re-iconed', async () => {
      mockApi.get.mockImplementation((url: string) => {
        if (url.includes('/presence')) {
          return Promise.resolve({
            feature_enabled: true,
            location_mode: 'zone_only',
            entries: [{
              user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
              state: 'zone', zone_id: 'z_school', zone_name: 'School', picture_url: null,
            }],
          })
        }
        return Promise.resolve({ zones: [zone('z_school', 'School', '🏫')] })
      })
      const { container, findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
      await findByText('Pascal')
      fireEvent.click(tabs(container)[1])
      const icon = () => container.querySelector('.sh-map-overview__zone-badge')?.textContent
      await waitFor(() => expect(icon()).toBe('🏫'))
      act(() => { zoneHandler()(upsert(zone('z_school', 'School', '🎒'))) })
      await waitFor(() => expect(icon()).toBe('🎒'))
    })
  })

  describe('live space_location_updated pins', () => {
    const pascal = (lat: number) => ({
      user_id: 'u_pascal', username: 'pascal', display_name: 'Pascal',
      state: 'not_home', latitude: lat, longitude: 8.5417,
      gps_accuracy_m: 10, picture_url: null,
    })
    let entries: unknown[]
    const presenceGets = () => mockApi.get.mock.calls
      .filter((c: unknown[]) => String(c[0]).endsWith('/presence')).length
    const pinHandler = () => {
      const call = mockWs.on.mock.calls.find((c: unknown[]) => c[0] === 'space_location_updated')
      return (call as unknown as [string, (e: unknown) => void])[1]
    }

    beforeEach(() => {
      entries = [pascal(47.3769)]
      mockApi.get.mockImplementation((url: string) => {
        if (url.includes('/presence')) {
          return Promise.resolve({
            feature_enabled: true, location_mode: 'gps', entries,
          })
        }
        return Promise.resolve({ zones: [] })
      })
    })

    it('refetches presence once for a burst of pin frames for this space', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        const { findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
        await findByText(/sharing GPS/i)
        expect(presenceGets()).toBe(1)
        entries = [pascal(47.4), { ...pascal(47.5), user_id: 'u_anna', username: 'anna', display_name: 'Anna' }]
        const frame = (lat: number) => ({
          type: 'space_location_updated',
          data: { type: 'space_location_updated', data: { space_id: 'sp_test', user_id: 'u_pascal', mode: 'gps', lat, lon: 8.5 } },
        })
        pinHandler()(frame(47.4))
        pinHandler()(frame(47.41))
        pinHandler()(frame(47.42))
        await vi.advanceTimersByTimeAsync(600)
        expect(presenceGets()).toBe(2)
        await findByText(/^2 of \d+ sharing GPS/)
      } finally {
        vi.useRealTimers()
      }
    })

    it('ignores a pin frame for another space', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        const { findByText } = render(<SpaceLocationCard spaceId="sp_test" />)
        await findByText(/sharing GPS/i)
        pinHandler()({
          type: 'space_location_updated',
          data: { type: 'space_location_updated', data: { space_id: 'sp_other', user_id: 'u_x' } },
        })
        await vi.advanceTimersByTimeAsync(600)
        expect(presenceGets()).toBe(1)
      } finally {
        vi.useRealTimers()
      }
    })
  })
})
