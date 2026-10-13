/**
 * HouseholdZonesTab tests — the shared ZonesAdmin editor pointed at
 * ``/api/household/zones``, with the household extras: one Home zone
 * (first, badged, never deleted), read-only rows synced from Home
 * Assistant, a WS refetch, and the presence-off state.
 *
 * Leaflet is mocked (jsdom can't lay out a map); SpaceZonesAdmin.test
 * covers the map plumbing.
 */
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

vi.mock('leaflet', () => {
  const noop = () => undefined
  const fluentSelf = (target: any) => new Proxy(target, {
    get(t, prop) {
      if (prop in t) return (t as any)[prop]
      return () => target
    },
  })
  const layer = fluentSelf({
    addTo: () => layer, bindTooltip: () => layer, bindPopup: () => layer,
    remove: noop, clearLayers: noop,
  })
  const map = fluentSelf({
    on: noop, addTo: () => map, setView: () => map, fitBounds: () => map,
    invalidateSize: noop, remove: noop,
  })
  return {
    default: {
      map: () => map,
      tileLayer: () => layer,
      layerGroup: () => layer,
      circle: () => layer,
      latLngBounds: () => ({ pad: () => ({}) }),
    },
  }
})
vi.mock('leaflet/dist/leaflet.css', () => ({}))

const mockApi = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  get api() { return mockApi },
}))

vi.mock('@/utils/mapTiles', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/mapTiles')>()),
  addTileLayer: () => Promise.resolve(),
}))

const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: () => void) => {
      wsHandlers.set(type, h)
      return () => { wsHandlers.delete(type) }
    },
  },
}))

const showToast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast }))
const platform = vi.hoisted(() => ({ ha: false, zoneSync: false }))
vi.mock('@/platform', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/platform')>()),
  usesHaUserDirectory: () => platform.ha,
  supportsZoneSync: () => platform.zoneSync,
}))

import { ApiError } from '@/api'
import { HouseholdZonesTab } from './HouseholdZonesTab'

const _zone = (over: Partial<any> = {}) => ({
  id: 'hz_school',
  name: 'School',
  latitude: 47.3769,
  longitude: 8.5417,
  radius_m: 150,
  color: '#3b82f6',
  icon: null,
  is_home: false,
  external_id: null,
  read_only: false,
  created_at: '2026-10-01T12:00:00+00:00',
  updated_at: '2026-10-01T12:00:00+00:00',
  ...over,
})
const HOME = _zone({ id: 'hz_home', name: 'Zürich flat', is_home: true, icon: '🏠' })
const WORK = _zone({
  id: 'hz_work', name: 'Work', external_id: 'zone.work', read_only: true,
})

/** The list rows, top to bottom. */
const rows = (c: Element) => [...c.querySelectorAll('.sh-zones-admin__item')] as HTMLElement[]

describe('HouseholdZonesTab', () => {
  beforeEach(() => {
    mockApi.get.mockReset()
    mockApi.post.mockReset()
    mockApi.patch.mockReset()
    mockApi.delete.mockReset()
    showToast.mockReset()
    wsHandlers.clear()
    platform.ha = false
    platform.zoneSync = false
  })

  it('tells a Home Assistant household that HA decides who is in which zone', async () => {
    platform.ha = true
    mockApi.get.mockResolvedValue({ zones: [HOME] })
    const { findByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    expect(await findByText(/Home Assistant reports who is in which zone/)).toBeTruthy()
  })

  it('shows no Home Assistant note on a standalone household', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME] })
    const { findByText, queryByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('Zürich flat')
    expect(queryByText(/Home Assistant reports who is in which zone/)).toBeNull()
  })

  it('loads /api/household/zones and lists Home first with a Home badge', async () => {
    mockApi.get.mockResolvedValue({ zones: [_zone({ name: 'Aarau' }), HOME] })
    const { container, findByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('Zürich flat')
    expect(mockApi.get).toHaveBeenCalledWith('/api/household/zones')
    const [first, second] = rows(container)
    expect(first.textContent).toContain('Zürich flat')
    expect(first.querySelector('.sh-zones-admin__badge--home')?.textContent).toContain('Home')
    expect(second.querySelector('.sh-zones-admin__badge--home')).toBeNull()
  })

  it('lets the Home zone be edited but never deleted', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME] })
    const { container, findByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('Zürich flat')
    const [home] = rows(container)
    expect([...home.querySelectorAll('button')].map(b => b.textContent)).toEqual(['Edit'])
  })

  it('marks a synced zone "From Home Assistant" with no Edit / Delete', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME, WORK] })
    const { container, findByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('Work')
    const work = rows(container).find(r => r.textContent!.includes('Work'))!
    expect(work.textContent).toContain('From Home Assistant')
    expect(work.textContent).toContain('Edit this zone in Home Assistant')
    expect(work.querySelectorAll('button')).toHaveLength(0)
  })

  it('invites the Home zone first when there are none, and pre-fills it', async () => {
    mockApi.get.mockResolvedValue({ zones: [] })
    mockApi.post.mockResolvedValueOnce(HOME)
    const { findByText, getByText, getByLabelText } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByText(/Add your Home zone/)
    fireEvent.click(getByText('+ Add Home zone'))
    expect((getByLabelText('Name') as HTMLInputElement).value).toBe('Home')
    const home = getByLabelText('This is our Home zone') as HTMLInputElement
    expect(home.checked).toBe(true)
    fireEvent.input(getByLabelText('Latitude'), { target: { value: '47.3769' } })
    fireEvent.input(getByLabelText('Longitude'), { target: { value: '8.5417' } })
    fireEvent.click(getByText('Create zone'))
    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(1))
    expect(mockApi.post).toHaveBeenCalledWith(
      '/api/household/zones',
      expect.objectContaining({ name: 'Home', icon: '🏠', is_home: true }),
    )
  })

  it('sends is_home when the Home toggle marks another zone as Home', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME, _zone()] })
    mockApi.patch.mockResolvedValueOnce(_zone({ is_home: true }))
    const { container, findByText, getByText, getByLabelText } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByText('School')
    const school = rows(container).find(r => r.textContent!.includes('School'))!
    fireEvent.click(school.querySelector('button')!)
    const toggle = getByLabelText('This is our Home zone') as HTMLInputElement
    expect(toggle.checked).toBe(false)
    expect(toggle.disabled).toBe(false)
    getByText(/replaces “Zürich flat”/)
    fireEvent.click(toggle)
    fireEvent.click(getByText('Save changes'))
    await waitFor(() => expect(mockApi.patch).toHaveBeenCalledTimes(1))
    expect(mockApi.patch).toHaveBeenCalledWith(
      '/api/household/zones/hz_school',
      expect.objectContaining({ is_home: true }),
    )
  })

  it('locks the Home toggle on the Home zone itself', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME] })
    const { findByText, getByText, getByLabelText } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByText('Zürich flat')
    fireEvent.click(getByText('Edit'))
    const toggle = getByLabelText('This is our Home zone') as HTMLInputElement
    expect(toggle.checked).toBe(true)
    expect(toggle.disabled).toBe(true)
    getByText(/mark another zone as Home/)
  })

  it('locks the Home toggle when Home comes from Home Assistant', async () => {
    const syncedHome = _zone({
      id: 'hz_ha_home', name: 'Home', is_home: true,
      external_id: 'zone.home', read_only: true,
    })
    mockApi.get.mockResolvedValue({ zones: [syncedHome, _zone()] })
    const { container, findByText, getByText, getByLabelText } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByText('School')
    // Home exists, so the button is the plain "Add zone".
    getByText('+ Add zone')
    const school = rows(container).find(r => r.textContent!.includes('School'))!
    fireEvent.click(school.querySelector('button')!)
    const toggle = getByLabelText('This is our Home zone') as HTMLInputElement
    expect(toggle.disabled).toBe(true)
    getByText(/Home zone comes from Home Assistant/)
  })

  it('shows ZONE_IS_HOME as a translated toast', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME, _zone()] })
    mockApi.delete.mockRejectedValueOnce(new ApiError(409, '/api/household/zones/hz_school', {
      code: 'ZONE_IS_HOME', detail: 'English detail',
    }))
    const { findByText, getAllByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('School')
    fireEvent.click(getAllByText('Delete')[0])
    const buttons = getAllByText('Delete')
    fireEvent.click(buttons[buttons.length - 1])
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "The Home zone can't be removed. Mark another zone as Home first.", 'error',
    ))
  })

  it('shows ZONE_READ_ONLY as a translated toast', async () => {
    mockApi.get.mockResolvedValue({ zones: [HOME, _zone()] })
    mockApi.patch.mockRejectedValueOnce(new ApiError(409, '/api/household/zones/hz_school', {
      code: 'ZONE_READ_ONLY', detail: 'English detail',
    }))
    const { container, findByText, getByText } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByText('School')
    const school = rows(container).find(r => r.textContent!.includes('School'))!
    fireEvent.click(school.querySelector('button')!)
    fireEvent.click(getByText('Save changes'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'This zone comes from Home Assistant. Change it in Home Assistant.', 'error',
    ))
  })

  it('refetches quietly on household_zones_changed', async () => {
    mockApi.get.mockResolvedValueOnce({ zones: [HOME] })
    const { findByText, queryByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
    await findByText('Zürich flat')
    mockApi.get.mockResolvedValueOnce({ zones: [HOME, _zone({ name: 'Gym' })] })
    act(() => { wsHandlers.get('household_zones_changed')!() })
    // No "Loading…" flash — the list stays up while it refetches.
    expect(queryByText('Loading zones…')).toBeNull()
    await findByText('Gym')
    expect(mockApi.get).toHaveBeenCalledTimes(2)
  })

  it('a quiet refetch that succeeds clears an earlier load error', async () => {
    mockApi.get.mockRejectedValueOnce(new Error('offline'))
    const { findByRole, findByText, queryByRole } = render(
      <HouseholdZonesTab onOpenSettings={() => {}} />,
    )
    await findByRole('alert')
    mockApi.get.mockResolvedValueOnce({ zones: [HOME] })
    act(() => { wsHandlers.get('household_zones_changed')!() })
    await findByText('Zürich flat')
    expect(queryByRole('alert')).toBeNull()
  })

  it('points to the presence toggle when the feature is off', async () => {
    mockApi.get.mockRejectedValue(new ApiError(403, '/api/household/zones', {
      code: 'FEATURE_DISABLED', detail: 'presence', section: 'presence',
    }))
    const onOpenSettings = vi.fn()
    const { findByText, getByText } = render(
      <HouseholdZonesTab onOpenSettings={onOpenSettings} />,
    )
    await findByText(/Presence is turned off/)
    fireEvent.click(getByText('Open Settings'))
    expect(onOpenSettings).toHaveBeenCalled()
  })

  describe('Home Assistant zone sync', () => {
    const SYNC_ON = {
      available: true, enabled: true,
      last_synced_at: new Date().toISOString(), last_error: null,
    }
    const SYNCED_HOME = _zone({
      id: 'hz_ha_home', name: 'Home', is_home: true, icon: '🏠',
      external_id: 'zone.home', read_only: true,
    })
    const route = (zones: unknown[]) => (path: string) => Promise.resolve(
      path.endsWith('/sync') ? SYNC_ON : { zones },
    )

    it('shows the sync switch above the list when the platform has zones', async () => {
      platform.ha = true
      platform.zoneSync = true
      mockApi.get.mockImplementation(route([SYNCED_HOME, WORK]))
      const { findByRole, findByText } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
      await findByText('Work')
      const toggle = await findByRole('switch', { name: 'Sync zones from Home Assistant' })
      expect((toggle as HTMLInputElement).checked).toBe(true)
    })

    it('has no sync switch on a standalone household', async () => {
      mockApi.get.mockResolvedValue({ zones: [HOME] })
      const { findByText, queryByRole } = render(<HouseholdZonesTab onOpenSettings={() => {}} />)
      await findByText('Zürich flat')
      expect(queryByRole('switch')).toBeNull()
      expect(mockApi.get).not.toHaveBeenCalledWith('/api/household/zones/sync')
    })

    it('turning the sync off (after the question) refetches the list', async () => {
      platform.ha = true
      platform.zoneSync = true
      mockApi.get.mockImplementation(route([SYNCED_HOME, WORK, _zone()]))
      mockApi.patch.mockResolvedValueOnce({ ...SYNC_ON, enabled: false, last_synced_at: null })
      const { findByRole, findByText, getByText, queryByText } = render(
        <HouseholdZonesTab onOpenSettings={() => {}} />,
      )
      await findByText('Work')
      fireEvent.click(await findByRole('switch'))
      getByText(/Your own zones stay/)
      mockApi.get.mockImplementation(route([_zone()]))
      fireEvent.click(getByText('Turn off and remove'))
      await waitFor(() => expect(queryByText('Work')).toBeNull())
      getByText('School')
      expect(mockApi.patch).toHaveBeenCalledWith('/api/household/zones/sync', { enabled: false })
    })
  })
})
