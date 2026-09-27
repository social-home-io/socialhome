import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import type { DirectoryEntry, Space } from '@/types'
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

const localEntry: DirectoryEntry = {
  space_id: 's1', host_instance_id: 'local',
  host_display_name: 'Your household', host_is_paired: true,
  name: 'Book Club', description: null, emoji: null,
  member_count: 0, scope: 'public', join_mode: 'open',
  // The list endpoint ships no features block, so the mapper starts here.
  allow_subscribers: undefined,
  min_age: 0,
}

describe('SpaceBrowserPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SpaceBrowserPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  }, 20000)
})

// The "Your household" tab is built from `GET /api/spaces`, which now ships
// the `features` block — so the flag arrives with the list and the hydration
// below is a fallback for an older backend that withholds it. Coercing the
// missing flag to `false` made every local public space claim 🔒 "Content is
// private" (a lie whenever the owner had followers ON) and suppressed the
// 🔔 Subscribe button, which needs an explicit `true`.
describe('hydrateLocalReadability', () => {
  it('skips the round-trip when the list row already carries the flag', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockClear()
    const [on, off] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's0', allow_subscribers: true },
      { ...localEntry, space_id: 's0b', allow_subscribers: false },
    ])
    expect(api.get).not.toHaveBeenCalled()
    expect(on.allow_subscribers).toBe(true)
    // An explicit `false` is knowledge too — it is what the 🔒 chip renders
    // off, so it must not trigger a fetch either.
    expect(off.allow_subscribers).toBe(false)
  }, 20000)

  it('fills allow_subscribers in from the space detail endpoint', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: 's1', features: { allow_subscribers: true },
    })
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's1' },
    ])
    expect(out.allow_subscribers).toBe(true)
  }, 20000)

  it('leaves the flag unknown when the detail fetch fails', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('nope'),
    )
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's2' },
    ])
    // `undefined`, never `false` — "we don't know" must not render as
    // "the content is private".
    expect(out.allow_subscribers).toBeUndefined()
  }, 20000)

  it('does not fetch anything for a household-scope space', async () => {
    const { api } = await import('@/api')
    const { hydrateLocalReadability } = await import('./SpaceBrowserPage')
    ;(api.get as ReturnType<typeof vi.fn>).mockClear()
    const [out] = await hydrateLocalReadability([
      { ...localEntry, space_id: 's3', scope: 'household' },
    ])
    expect(api.get).not.toHaveBeenCalled()
    expect(out.allow_subscribers).toBeUndefined()
  }, 20000)
})

// The "Your household" tab is where the owner of a freshly published space
// looks for it. It used to keep only `household` + `public` rows, so a
// locally-hosted `global` space — the one kind you publish deliberately —
// was invisible in Browse on the household that hosts it.
describe('buildHouseholdEntries', () => {
  const spaces = [
    { id: 'p1', name: 'Priv', space_type: 'private' },
    { id: 'h1', name: 'Home', space_type: 'household' },
    { id: 'u1', name: 'Pub', space_type: 'public' },
    {
      id: 'g1', name: 'Glob', space_type: 'global',
      features: { allow_subscribers: true },
    },
  ] as unknown as Space[]

  it('keeps locally-hosted global spaces, and still drops private ones', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const out = buildHouseholdEntries(spaces, {
      memberIds: new Set<string>(),
      subIds: new Set<string>(),
      pendingIds: new Set<string>(),
    })
    expect(out.map((e) => e.space_id)).toEqual(['h1', 'u1', 'g1'])
    // The scope drives the card's chip (🌐 Global) and the 🔒 readability
    // chip, which only speaks for public/global rows.
    expect(out[2].scope).toBe('global')
    expect(out[2].allow_subscribers).toBe(true)
  }, 20000)

  it('carries the list row features through, no detail fetch needed', async () => {
    const { buildHouseholdEntries } = await import('./SpaceBrowserPage')
    const [entry] = buildHouseholdEntries(
      [{
        id: 'u2', name: 'Closed', space_type: 'public',
        features: { allow_subscribers: false },
      } as unknown as Space],
      {
        memberIds: new Set(['u2']),
        subIds: new Set<string>(),
        pendingIds: new Set<string>(),
      },
    )
    expect(entry.allow_subscribers).toBe(false)
    expect(entry.already_member).toBe(true)
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
