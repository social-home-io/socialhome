import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import type { DirectoryEntry, HouseholdSpaceRow } from '@/types'
import { openPairing } from '@/components/PairingFlow'
import { showToast } from '@/components/Toast'

vi.mock('@/components/PairingFlow', () => ({
  openPairing: vi.fn(),
  PairingFlow: () => null,
}))
vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: {
    value: {
      user_id: 'u1',
      username: 'a',
      display_name: 'A',
      is_admin: true,
      picture_url: null,
      picture_hash: null,
      bio: null,
      is_new_member: false,
    },
  },
  token: { value: 'tok' },
  isAuthed: { value: true },
}))

describe('SpaceBrowserPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SpaceBrowserPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)
})

// "Your household" is built from `GET /api/household/spaces`: the server
// lists what the caller may find (never a private space) with the real
// member count and the caller's seat. It used to be built from
// `GET /api/spaces` — only spaces you were already in, every card "0
// members" — so a household space you weren't in could not be found.
describe('buildHouseholdEntries', () => {
  const row = (over: Partial<HouseholdSpaceRow>): HouseholdSpaceRow => ({
    space_id: 'h1', name: 'Home', description: null, emoji: '🏠',
    space_type: 'household', join_mode: 'open', category: 'general',
    allow_subscribers: false, min_age: 0, member_count: 4, role: 'member',
    ...over,
  })
  const sets = {
    subIds: new Set<string>(),
    pendingIds: new Set<string>(),
  }

  it('carries the real member count and min_age through', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const [entry] = buildHouseholdEntries([row({ member_count: 7, min_age: 13 })], sets)
    expect(entry.member_count).toBe(7)
    expect(entry.min_age).toBe(13)
    expect(entry.scope).toBe('household')
    expect(entry.host_instance_id).toBe('local')
  }, 20000)

  it('a household space the caller left is offered to join again', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const [entry] = buildHouseholdEntries([row({ role: null })], sets)
    expect(entry.already_member).toBe(false)
    expect(entry.join_mode).toBe('open')
  }, 20000)

  it('a follower is subscribed, not a member', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const [entry] = buildHouseholdEntries(
      [row({ space_type: 'public', role: 'subscriber', allow_subscribers: true })],
      sets,
    )
    expect(entry.already_member).toBe(false)
    expect(entry.already_subscribed).toBe(true)
    expect(entry.allow_subscribers).toBe(true)
  }, 20000)

  it('marks a request the caller already sent as pending', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const [entry] = buildHouseholdEntries(
      [row({ space_type: 'public', join_mode: 'request', role: null })],
      { subIds: new Set<string>(), pendingIds: new Set(['h1']) },
    )
    expect(entry.request_pending).toBe(true)
  }, 20000)
})

describe('Your household tab', () => {
  afterEach(() => cleanup())

  it('lists a household space the caller is not in with its real count and a Join button', async () => {
    const { api } = await import('@/api')
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/household/spaces') {
        return Promise.resolve([{
          space_id: 'h7', name: 'Hottingen Community Choir', description: null,
          emoji: '🎶', space_type: 'household', join_mode: 'open',
          category: 'general', allow_subscribers: false, min_age: 0,
          member_count: 9, role: null,
        }])
      }
      if (url === '/api/me/subscriptions') return Promise.resolve({ subscriptions: [] })
      if (url === '/api/me/join-requests') return Promise.resolve({ pending_space_ids: [] })
      return Promise.resolve([])
    })
    const { default: SpaceBrowserPage } = await import('./SpaceBrowserPage')
    const r = render(
      <LocationProvider>
        <SpaceBrowserPage />
      </LocationProvider>,
    )
    fireEvent.click(await r.findByRole('tab', { name: /Your household/ }))
    await r.findByText('Hottingen Community Choir')
    expect(r.container.textContent).toContain('9 members')
    expect(r.container.textContent).not.toContain('0 members')
    expect(await r.findByRole('button', { name: /Join/ })).toBeTruthy()
  }, 20000)
})

// Pairing with another household is admin-only on the backend (a non-admin
// gets 403), so the browser must not hand a member a pairing button that
// can only fail. Admins keep the CTA; members get a short "ask an admin".
describe('pairing affordances follow the household role', () => {
  const unpairedGlobal: DirectoryEntry = {
    space_id: 'g9', host_instance_id: 'far-away',
    host_display_name: 'Far Household', host_is_paired: false,
    name: 'Far Club', description: null, emoji: null,
    member_count: 3, scope: 'global', join_mode: 'open',
    min_age: 0,
  }

  async function renderAs(isAdmin: boolean, global: DirectoryEntry[] = []) {
    const { currentUser } = await import('@/store/auth')
    ;(currentUser.value as { is_admin: boolean }).is_admin = isAdmin
    const { api } = await import('@/api')
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/public_spaces') return Promise.resolve(global)
      if (url === '/api/me/subscriptions') return Promise.resolve({ subscriptions: [] })
      if (url === '/api/me/join-requests') return Promise.resolve({ pending_space_ids: [] })
      return Promise.resolve([])
    })
    const { default: SpaceBrowserPage } = await import('./SpaceBrowserPage')
    const r = render(
      <LocationProvider>
        <SpaceBrowserPage />
      </LocationProvider>,
    )
    await waitFor(() => {
      expect(r.container.querySelector('.sh-browser-tabs')).not.toBeNull()
    })
    return r
  }

  async function openTab(r: ReturnType<typeof render>, label: RegExp) {
    const tab = [...r.container.querySelectorAll('[role="tab"]')]
      .find(b => label.test(b.textContent ?? ''))!
    fireEvent.click(tab)
  }

  afterEach(() => {
    cleanup()
    vi.mocked(openPairing).mockClear()
    vi.mocked(showToast).mockClear()
  })

  it('an admin gets the "Pair with another household" button on the friends tab', async () => {
    const r = await renderAs(true)
    await openTab(r, /From friends/)
    const btn = await r.findByRole('button', { name: /Pair with another household/ })
    fireEvent.click(btn)
    expect(openPairing).toHaveBeenCalledWith('household')
    expect(r.container.textContent).not.toContain('Ask a household admin')
  }, 20000)

  it('a non-admin gets a hint instead of a button that would be refused', async () => {
    const r = await renderAs(false)
    await openTab(r, /From friends/)
    await waitFor(() => {
      expect(r.container.textContent).toContain('Ask a household admin')
    })
    expect(r.queryByRole('button', { name: /Pair with another household/ })).toBeNull()
  }, 20000)

  it('a non-admin tapping "Connect with … first" is told to ask an admin', async () => {
    const r = await renderAs(false, [unpairedGlobal])
    await openTab(r, /Global/)
    const cta = await r.findByText(/Connect with Far Household first/)
    fireEvent.click(cta)
    expect(openPairing).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('Ask a household admin'),
      'info',
    )
  }, 20000)

  it('an admin tapping "Connect with … first" opens pairing', async () => {
    const r = await renderAs(true, [unpairedGlobal])
    await openTab(r, /Global/)
    fireEvent.click(await r.findByText(/Connect with Far Household first/))
    expect(openPairing).toHaveBeenCalledWith('household')
  }, 20000)
})

describe('SpaceBrowserPage — Open space routes in-app', () => {
  // ``window.location.href = …`` reloads the whole SPA; the router's
  // ``route()`` pushes history and swaps the page in place. JSDOM never
  // moves ``location`` on a hard navigate, so a changed pathname proves
  // the router did it.
  const before = location.pathname
  afterEach(() => {
    cleanup()
    history.replaceState(null, '', before)
  })

  it("a member's Open space goes to the space without a full page reload", async () => {
    const { api } = await import('@/api')
    ;(api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/household/spaces') {
        return Promise.resolve([{
          space_id: 'sp-9', name: 'Neighbourhood Garden', description: null,
          emoji: null, space_type: 'household', join_mode: 'open',
          category: 'general', allow_subscribers: false, min_age: 0,
          member_count: 3, role: 'member',
        }])
      }
      if (url === '/api/me/subscriptions') return Promise.resolve({ subscriptions: [] })
      if (url === '/api/me/join-requests') return Promise.resolve({ pending_space_ids: [] })
      return Promise.resolve([])
    })
    const { IngressLocationProvider } = await import('@/router/IngressLocationProvider')
    const { default: SpaceBrowserPage } = await import('./SpaceBrowserPage')
    history.replaceState(null, '', '/spaces/browse')
    const r = render(
      <IngressLocationProvider>
        <SpaceBrowserPage />
      </IngressLocationProvider>,
    )
    fireEvent.click(await r.findByRole('tab', { name: /Your household/ }))
    const open = await r.findByRole('button', { name: /Open space/ })
    fireEvent.click(open)
    expect(location.pathname).toBe('/spaces/sp-9')
  }, 20000)
})
