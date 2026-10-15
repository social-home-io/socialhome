import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { signal } from '@preact/signals'
import type { User } from '@/types'

/** Props of every LocationMap render — the map itself is Leaflet-heavy. */
interface MapProps {
  markers: Array<Record<string, unknown>>
  zones?: Array<Record<string, unknown>>
  selectedId?: string | null
  zoomToSelection?: boolean
  fitInsets?: Record<string, number>
}
const mapProps = vi.hoisted(() => [] as MapProps[])
vi.mock('@/components/LocationMap', () => ({
  LocationMap: (p: MapProps) => { mapProps.push(p); return null },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
const emit = (type: string, data: Record<string, unknown>) =>
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))

const me: User = {
  user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  status: { emoji: null, text: null, expires_at: null },
}

const row = (status: Record<string, unknown> | null) => ({
  username: 'bob', user_id: 'u2', display_name: 'Bob', picture_url: null,
  state: 'home', zone_name: null, is_online: true, is_idle: false,
  last_seen_at: null, status,
})

describe('PresencePage', () => {
  const currentUser = signal<User | null>(null)

  beforeEach(() => {
    vi.resetModules()
    handlers.clear()
    currentUser.value = { ...me }
    vi.doMock('@/store/auth', () => ({ currentUser }))
    vi.doMock('@/ws', () => ({
      ws: {
        on: (type: string, h: Handler) => {
          if (!handlers.has(type)) handlers.set(type, new Set())
          handlers.get(type)!.add(h)
          return () => { handlers.get(type)?.delete(h) }
        },
      },
    }))
  })

  it('hands the map each member with their picture and user id', async () => {
    mapProps.length = 0
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [{
          ...row(null), latitude: 47.1, longitude: 8.2, gps_accuracy_m: 12,
          picture_url: 'api/users/u2/picture?v=1&exp=1&sig=x',
        }]),
      },
    }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    await view.findByText('Bob', { selector: 'strong' })
    const markers = mapProps.at(-1)!.markers
    expect(markers).toHaveLength(1)
    expect(markers[0]).toMatchObject({
      id: 'u2', user_id: 'u2', lat: 47.1, lon: 8.2, label: 'Bob',
      avatar_url: 'api/users/u2/picture?v=1&exp=1&sig=x',
    })
  })

  it('words the home zone like every other surface, never the raw HA "home"', async () => {
    mapProps.length = 0
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [
          { ...row(null), zone_name: 'home', latitude: 47.1, longitude: 8.2 },
          {
            ...row(null), user_id: 'u3', username: 'cy', display_name: 'Cy',
            state: 'zone', zone_name: 'School', latitude: 47.2, longitude: 8.3,
          },
        ]),
      },
    }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    await view.findByText('Bob', { selector: 'strong' })
    expect(view.queryByText('home')).toBeNull()
    expect(view.getAllByText('Home').length).toBeGreaterThan(0)
    expect(view.getAllByText('School').length).toBeGreaterThan(0)
    const subs = mapProps.at(-1)!.markers.map(m => m.sub_label)
    expect(subs).toEqual(['Home', 'School'])
  })

  it("shows each member's status", async () => {
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(async () => [row({ emoji: '🌴', text: 'On leave', expires_at: null })]) },
    }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    expect(await view.findByText('🌴 On leave')).toBeTruthy()
    expect(view.getByText('No status set')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Set status' })).toBeTruthy()
  })

  it('user.status_changed refetches the roster and updates our own status', async () => {
    let rows = [row(null)]
    const get = vi.fn(async () => rows)
    vi.doMock('@/api', () => ({ api: { get } }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    await view.findByText('Bob', { selector: 'strong' })

    rows = [row({ emoji: '🎧', text: 'Focus', expires_at: null })]
    emit('user.status_changed', { user_id: 'u2', status: rows[0].status })
    expect(await view.findByText('🎧 Focus')).toBeTruthy()

    emit('user.status_changed', {
      user_id: 'u1', status: { emoji: '🍽️', text: 'Lunch', expires_at: null },
    })
    await waitFor(() => expect(currentUser.value?.status?.text).toBe('Lunch'))
    expect(await view.findByText('🍽️ Lunch')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Edit status' })).toBeTruthy()

    emit('user.status_changed', { user_id: 'u1', status: null })
    await waitFor(() => expect(view.getByText('No status set')).toBeTruthy())
  })

  describe('household zones and the map overview', () => {
    const zone = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
      id, name, latitude: 47, longitude: 8, radius_m: 200, color: null, icon: null,
      is_home: false, external_id: null, read_only: false,
      created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', ...extra,
    })
    let zones: ReturnType<typeof zone>[]
    let people: Array<Record<string, unknown>>
    const get = vi.fn(async (path: string) => (
      path === '/api/household/zones' ? { zones } : people
    ))

    beforeEach(() => {
      mapProps.length = 0
      get.mockClear()
      zones = [zone('hz_home', 'Home', { icon: '🏠', is_home: true }), zone('hz_school', 'School')]
      people = [
        // At home with a position.
        { ...row(null), latitude: 47.1, longitude: 8.2, zone_name: 'home' },
        // Home Assistant reports a zone but no position.
        {
          ...row(null), user_id: 'u3', username: 'cy', display_name: 'Cy',
          state: 'zone', zone_name: 'school', is_online: false, last_seen_at: null,
        },
        // Out and about.
        {
          ...row(null), user_id: 'u4', username: 'di', display_name: 'Di',
          state: 'not_home', latitude: 46.5, longitude: 7.5,
        },
        // Nothing to place.
        { ...row(null), user_id: 'u5', username: 'ed', display_name: 'Ed', state: 'unknown' },
      ]
      vi.doMock('@/api', () => ({ api: { get } }))
    })

    it('draws the household zones and puts each member in their stored zone', async () => {
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Bob', { selector: 'strong' })
      expect(get).toHaveBeenCalledWith('/api/household/zones')
      await waitFor(() => expect(mapProps.at(-1)!.zones).toHaveLength(2))
      const last = mapProps.at(-1)!
      expect(last.zoomToSelection).toBe(true)
      expect(last.fitInsets).toEqual({})
      const byId = Object.fromEntries(last.markers.map((m) => [m.id, m]))
      expect(Object.keys(byId).sort()).toEqual(['u2', 'u3', 'u4'])
      expect(byId.u2).toMatchObject({ zone_id: 'hz_home', lat: 47.1, lon: 8.2 })
      // No position: placed by the zone alone (case-insensitive name).
      expect(byId.u3).toMatchObject({ zone_id: 'hz_school', lat: null, lon: null })
      expect(byId.u4).toMatchObject({ zone_id: null, lat: 46.5 })
    })

    it('lists the placed members and counts them per zone', async () => {
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Bob', { selector: 'strong' })
      const rows = () => Array.from(view.container.querySelectorAll<HTMLElement>(
        '.sh-map-overview__body .sh-map-overview__row',
      ))
      await waitFor(() => expect(view.container.querySelectorAll('.sh-map-overview__tabs button')).toHaveLength(2))
      expect(rows().map((r) => r.querySelector('.sh-map-overview__row-name')!.textContent))
        .toEqual(['Bob', 'Cy', 'Di'])
      fireEvent.click(view.container.querySelectorAll('.sh-map-overview__tabs button')[1])
      expect(rows().map((r) => r.querySelector('.sh-map-overview__count')!.textContent))
        .toEqual(['1 person', '1 person'])
    })

    it('counts a member in the zone the map puts them in when no zone is stored', async () => {
      // Home Assistant says not_home, but the point is inside School: the
      // map draws them in the School bubble, so the overview must too.
      zones = [
        zone('hz_home', 'Home', { is_home: true }),
        zone('hz_school', 'School', { latitude: 46.9, longitude: 7.9 }),
      ]
      people = [{
        ...row(null), user_id: 'u6', username: 'fa', display_name: 'Fa',
        state: 'not_home', zone_name: null, latitude: 46.9, longitude: 7.9,
      }]
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Fa', { selector: 'strong' })
      await waitFor(() => expect(view.container.querySelectorAll('.sh-map-overview__tabs button')).toHaveLength(2))
      fireEvent.click(view.container.querySelectorAll('.sh-map-overview__tabs button')[1])
      const counts = Array.from(view.container.querySelectorAll<HTMLElement>(
        '.sh-map-overview__body .sh-map-overview__row',
      )).map((r) => [
        r.querySelector('.sh-map-overview__row-name')!.textContent,
        r.querySelector('.sh-map-overview__count')!.textContent,
      ])
      expect(counts).toEqual([['Home', 'Nobody'], ['School', '1 person']])
    })

    it('shares the selection with the map and shows who is online', async () => {
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Bob', { selector: 'strong' })
      const bob = await waitFor(() => {
        const r = view.container.querySelector<HTMLElement>('[data-overview-id="u2"]')
        expect(r).not.toBeNull()
        return r!
      })
      fireEvent.click(bob)
      await waitFor(() => expect(mapProps.at(-1)!.selectedId).toBe('u2'))
      const facts = view.container.querySelector('.sh-map-overview__facts')!.textContent
      expect(facts).toContain('Online')
    })

    it('refetches the zones when they change', async () => {
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Bob', { selector: 'strong' })
      await waitFor(() => expect(mapProps.at(-1)!.zones).toHaveLength(2))
      zones = [...zones, zone('hz_work', 'Work')]
      emit('household_zones_changed', {})
      await waitFor(() => expect(mapProps.at(-1)!.zones).toHaveLength(3))
    })

    it('keeps the map without zones when the zones cannot load', async () => {
      get.mockImplementation(async (path: string) => {
        if (path === '/api/household/zones') throw new Error('403 FEATURE_DISABLED')
        return people
      })
      const { default: PresencePage } = await import('./PresencePage')
      const view = render(<PresencePage />)
      await view.findByText('Bob', { selector: 'strong' })
      expect(mapProps.at(-1)!.zones).toEqual([])
      expect(view.getByText('Di', { selector: 'strong' })).toBeTruthy()
    })
  })

  it('a member card is one button that opens their person sheet', async () => {
    const openPerson = vi.fn()
    vi.doMock('@/components/PersonSheet', () => ({ openPerson }))
    vi.doMock('@/api', () => ({
      api: { get: vi.fn(async () => [row({ emoji: '🌞', text: 'Holiday', expires_at: null })]) },
    }))
    const { default: PresencePage } = await import('./PresencePage')
    const view = render(<PresencePage />)
    await view.findByText('Bob', { selector: 'strong' })
    const card = view.getByTestId('presence-card')
    expect(card.tagName).toBe('BUTTON')
    expect(card.getAttribute('type')).toBe('button')
    fireEvent.click(card)
    expect(openPerson).toHaveBeenCalledWith('u2', {
      person: {
        display_name: 'Bob', picture_url: null, username: 'bob', is_local: true,
        is_online: true, is_idle: false,
        status: { emoji: '🌞', text: 'Holiday', expires_at: null },
      },
    })
  })
})
