import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy }),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
}))

// LocationMap pulls in Leaflet which doesn't initialise cleanly in
// jsdom — stub it to a deterministic placeholder so the tests assert
// on layout, not map internals.
vi.mock('@/components/LocationMap', () => ({
  LocationMap: ({ markers }: { markers: { id: string }[] }) => (
    <div data-testid="map" data-markers={markers.length}>
      {markers.map(m => (
        <span key={m.id} class="map-marker" data-id={m.id} />
      ))}
    </div>
  ),
}))

vi.mock('@/components/Avatar', () => ({
  Avatar: ({ name }: { name: string }) => <span class="avatar">{name}</span>,
}))

vi.mock('@/components/OnlinePill', () => ({
  OnlinePill: () => null,
}))

import FriendsPage from './FriendsPage'
import { api } from '@/api'

const apiMock = api as unknown as {
  get: ReturnType<typeof vi.fn>
}

function payload(over: Partial<{
  households: Record<string, unknown>[]
  instance: Record<string, unknown>
  totals: { households: number; people: number }
}> = {}) {
  return {
    instance: over.instance ?? {
      instance_id: 'us',
      display_name: 'Vizeli Home',
      home_lat: 52.0907,
      home_lon: 5.1214,
      members: [
        {
          user_id: 'u-me', username: 'pascal', display_name: 'Pascal',
          picture_url: null, is_online: true, is_idle: false,
        },
      ],
      member_count: 1,
    },
    households: over.households ?? [],
    totals: over.totals ?? { households: 1, people: 1 },
  }
}

const apiPost = (api as unknown as { post: ReturnType<typeof vi.fn> }).post

describe('FriendsPage', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    apiPost.mockReset()
    routeSpy.mockReset()
  })

  it('renders the local household block + empty-state copy when no pairs', async () => {
    apiMock.get.mockResolvedValueOnce(payload({ households: [] }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-friends')).not.toBeNull()
    })
    expect(container.textContent).toContain('Vizeli Home')
    expect(container.textContent).toContain('your household')
    expect(container.textContent).toContain('No connected households yet')
  })

  it('a non-admin empty state asks for an admin instead of offering pairing', async () => {
    // The mocked viewer carries no is_admin, i.e. a regular member.
    apiMock.get.mockResolvedValueOnce(payload({ households: [] }))
    const { container, queryByText } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-friends')).not.toBeNull()
    })
    expect(queryByText(/Pair a household/)).toBeNull()
    expect(container.textContent).toContain('Ask a household admin')
    // No link into an admin-only page, and never an origin-absolute href
    // that would skip the ingress prefix.
    expect(container.querySelector('a[href="/connections"]')).toBeNull()
  })

  it('hides the map when no household has coords', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      instance: {
        instance_id: 'us', display_name: 'No-coords Home',
        home_lat: null, home_lon: null,
        members: [], member_count: 0,
      },
      households: [],
    }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-friends')).not.toBeNull()
    })
    expect(container.querySelector('[data-testid="map"]')).toBeNull()
  })

  it('renders one map marker per household with coords', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      households: [
        {
          instance_id: 'p-a', display_name: 'A Home',
          home_lat: 52.0, home_lon: 4.0,
          paired_at: '2026-04-01T10:00:00Z', reachable: true,
          members: [], member_count: 0,
        },
        {
          instance_id: 'p-b', display_name: 'B Home',
          home_lat: null, home_lon: null,  // skipped on the map
          paired_at: '2026-04-15T12:00:00Z', reachable: false,
          members: [], member_count: 0,
        },
      ],
      totals: { households: 3, people: 1 },
    }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('[data-testid="map"]')).not.toBeNull()
    })
    // Local + one paired with coords = 2 markers (B has no coords).
    const map = container.querySelector('[data-testid="map"]') as HTMLElement
    expect(map.getAttribute('data-markers')).toBe('2')
  })

  it('renders one card per paired household with member chips', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      households: [
        {
          instance_id: 'p-a', display_name: 'A Home',
          home_lat: 52.0, home_lon: 4.0,
          paired_at: '2026-04-01T10:00:00Z', reachable: true,
          members: [
            { user_id: 'ru-1', instance_id: 'p-a', remote_username: 'bob',
              display_name: 'Bob', picture_url: null },
            { user_id: 'ru-2', instance_id: 'p-a', remote_username: 'carol',
              display_name: 'Carol', picture_url: null },
          ],
          member_count: 2,
        },
      ],
      totals: { households: 2, people: 3 },
    }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.textContent).toContain('A Home')
    })
    const cards = container.querySelectorAll('.sh-friends-household')
    // Local + one paired = 2 cards.
    expect(cards.length).toBe(2)
    expect(container.textContent).toContain('Bob')
    expect(container.textContent).toContain('Carol')
  })

  it('shows the totals headline', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      totals: { households: 4, people: 17 },
    }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.textContent).toContain('17')
    })
    expect(container.textContent).toContain('households')
  })

  // ── Quick DM affordance (PR C) ───────────────────────────────────────

  it('local member chip opens the action sheet for everyone except the viewer', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      instance: {
        instance_id: 'us', display_name: 'Vizeli Home',
        home_lat: null, home_lon: null,
        members: [
          { user_id: 'u-me', username: 'pascal', display_name: 'Pascal',
            picture_url: null, is_online: true, is_idle: false },
          { user_id: 'u-mom', username: 'maria', display_name: 'Maria',
            picture_url: null, is_online: false, is_idle: false },
        ],
        member_count: 2,
      },
    }))
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-friends')).not.toBeNull()
    })
    // Pascal (viewer) is rendered as a passive ``--self`` card; only
    // Maria becomes a click-to-actions button. Chip click now opens
    // the action sheet (Message / Rename) rather than going straight
    // to a DM.
    expect(container.querySelector('.sh-friends-member-chip--self'))
      .not.toBeNull()
    const dmButtons = container.querySelectorAll(
      'button.sh-friends-member-chip',
    )
    expect(dmButtons.length).toBe(1)
    expect(
      (dmButtons[0] as HTMLButtonElement).getAttribute('aria-label'),
    ).toBe('Open actions for Maria')
  })

  it('clicking a local member chip then Message POSTs /api/conversations/dm with {username}', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      instance: {
        instance_id: 'us', display_name: 'Vizeli Home',
        home_lat: null, home_lon: null,
        members: [
          { user_id: 'u-me', username: 'pascal', display_name: 'Pascal',
            picture_url: null, is_online: true, is_idle: false },
          { user_id: 'u-mom', username: 'maria', display_name: 'Maria',
            picture_url: null, is_online: false, is_idle: false },
        ],
        member_count: 2,
      },
    }))
    apiPost.mockResolvedValueOnce({ id: 'cdm-local' })
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('button.sh-friends-member-chip'))
        .not.toBeNull()
    })
    fireEvent.click(
      container.querySelector('button.sh-friends-member-chip') as HTMLElement,
    )
    // Action sheet appears with Message + Rename. Click Message.
    const messageBtn = await waitFor(() => {
      const b = container.querySelector('[data-testid="friend-action-message"]')
      if (!b) throw new Error('action sheet did not open')
      return b as HTMLButtonElement
    })
    fireEvent.click(messageBtn)
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/conversations/dm',
        { username: 'maria' },
      )
    })
    await waitFor(() => {
      expect(routeSpy).toHaveBeenCalledWith('/dms/cdm-local')
    })
  })

  it('clicking a remote member chip then Message POSTs /api/conversations/dm with {user_id}', async () => {
    apiMock.get.mockResolvedValueOnce(payload({
      households: [
        {
          instance_id: 'p-bro', display_name: "Brother's house",
          home_lat: null, home_lon: null,
          paired_at: '2026-04-01T10:00:00Z', reachable: true,
          members: [
            { user_id: 'u-bro', instance_id: 'p-bro', remote_username: 'bob',
              display_name: 'Bob', picture_url: null },
          ],
          member_count: 1,
        },
      ],
      totals: { households: 2, people: 2 },
    }))
    apiPost.mockResolvedValueOnce({ id: 'cdm-cross' })
    const { container } = render(<FriendsPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-friends-member-chip--button'))
        .not.toBeNull()
    })
    fireEvent.click(
      container.querySelector('.sh-friends-member-chip--button') as HTMLElement,
    )
    const messageBtn = await waitFor(() => {
      const b = container.querySelector('[data-testid="friend-action-message"]')
      if (!b) throw new Error('action sheet did not open')
      return b as HTMLButtonElement
    })
    fireEvent.click(messageBtn)
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/conversations/dm',
        { user_id: 'u-bro' },
      )
    })
    await waitFor(() => {
      expect(routeSpy).toHaveBeenCalledWith('/dms/cdm-cross')
    })
  })
})
