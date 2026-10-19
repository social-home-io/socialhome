import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const wsHandlers: Record<string, Handler[]> = {}
const conn = vi.hoisted(() => ({ state: null as null | { value: string } }))
vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  const state = signal('open')
  conn.state = state
  return {
    connectionState: state,
    ws: {
      on: (type: string, h: Handler) => {
        ;(wsHandlers[type] ??= []).push(h)
        return () => { wsHandlers[type] = (wsHandlers[type] ?? []).filter(x => x !== h) }
      },
      send: vi.fn(),
    },
  }
})

const user = vi.hoisted(() => ({ is_admin: true }))
vi.mock('@/store/auth', () => ({
  currentUser: {
    get value() {
      return { user_id: 'u1', username: 'pascal', display_name: 'Pascal', is_admin: user.is_admin, picture_url: null, bio: null, is_new_member: false }
    },
  },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const ELTERNRAT = 'Elternrat der Primarschule Zürich-Hottingen und Umgebung (Schuljahr 2026/27)'

const SPACES = [
  { id: 'sA', name: 'Choir', space_type: 'private', emoji: '🎶', my_role: 'member', member_count: 8, last_activity_at: null, needs_you: null },
  { id: 'sB', name: 'Band', space_type: 'private', emoji: null, my_role: 'admin', member_count: 1, last_activity_at: null, needs_you: 0 },
  { id: 'sG', name: 'Garden', space_type: 'public', emoji: '🥕', my_role: 'owner', member_count: 4, last_activity_at: null, needs_you: 3, icon_url: '/api/spaces/sG/icon?v=1&sig=x' },
  { id: 'sE', name: ELTERNRAT, space_type: 'household', emoji: '🏫', my_role: 'member', member_count: 3, last_activity_at: null, needs_you: null },
  { id: 'sK', name: 'Kunstverein', space_type: 'global', emoji: '🎨', my_role: 'subscriber', member_count: 41, last_activity_at: null, needs_you: null },
  { id: 'sOld', name: 'Kindergarten 2024', space_type: 'private', emoji: null, my_role: 'member', member_count: 5, archived: true },
]

const HOUSEHOLD = [
  // Joinable at home.
  { space_id: 'hChoir', name: 'Hottingen Community Choir', description: null, emoji: '🎶', space_type: 'household', join_mode: 'open', category: 'general', allow_subscribers: false, min_age: 0, member_count: 1, role: null },
  // Already a member — not in Discover.
  { space_id: 'sE', name: ELTERNRAT, description: null, emoji: '🏫', space_type: 'household', join_mode: 'open', category: 'general', allow_subscribers: false, min_age: 0, member_count: 3, role: 'member' },
  // Invite-only — never in Discover.
  { space_id: 'hSecret', name: 'Secret club', description: null, emoji: null, space_type: 'public', join_mode: 'invite_only', category: 'general', allow_subscribers: false, min_age: 0, member_count: 2, role: null },
]

const PAIRED = [
  { space_id: 'pMarket', host_instance_id: 'iid-mueller', host_display_name: 'Müller household', host_is_paired: true, name: 'Müller family market', description: null, emoji: '🧺', member_count: 6, join_mode: 'request', min_age: 0 },
  { space_id: 'pBike', host_instance_id: 'iid-keller', host_display_name: 'Keller household', host_is_paired: true, name: 'Hottingen bike repair', description: null, emoji: '🚲', member_count: 12, join_mode: 'open', min_age: 0 },
]

interface Wire {
  spaces?: unknown
  unread?: Record<string, unknown>
  gfs?: unknown[]
  global?: unknown[]
}

function wire({ spaces = SPACES, unread = {}, gfs = [], global = [] }: Wire = {}) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces') {
      if (spaces instanceof Error) throw spaces
      return spaces
    }
    if (url === '/api/spaces/chat-unread') return { spaces: unread }
    if (url === '/api/household/spaces') return HOUSEHOLD
    if (url === '/api/peer_spaces') return PAIRED
    if (url === '/api/public_spaces') return global
    if (url === '/api/gfs/connections') return gfs
    if (url === '/api/me/subscriptions') return { subscriptions: [] }
    if (url === '/api/me/join-requests') return { pending_space_ids: [] }
    if (url === '/api/users') return [{ username: 'pascal', display_name: 'Pascal', is_admin: true }]
    return []
  })
}

const START = location.pathname

async function renderPage(url = '/spaces') {
  const tl = await import('@testing-library/preact')
  const { IngressLocationProvider } = await import('@/router/IngressLocationProvider')
  const { default: Page } = await import('./SpaceListPage')
  history.replaceState(null, '', url)
  return { ...tl, ...tl.render(<IngressLocationProvider><Page /></IngressLocationProvider>) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPost.mockResolvedValue({})
  user.is_admin = true
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  wire()
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
  history.replaceState(null, '', START)
})

describe('SpaceListPage — sections', () => {
  it('lists Yours, Following, Discover and a collapsed Archived, each under an h2', async () => {
    const r = await renderPage()
    await r.findByText('Choir')
    const headings = r.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headings).toEqual(['Yours', 'Following', 'Discover', 'Archived'])
    // The follower's space is under Following, read-only.
    const following = r.getByRole('region', { name: 'Following' })
    expect(following.textContent).toContain('Kunstverein')
    expect(following.textContent).toContain('Following · read-only')
    expect(r.getByRole('region', { name: 'Yours' }).textContent).not.toContain('Kunstverein')
    // Archived is collapsed and never mixed with Yours.
    const archived = r.container.querySelector('details.sh-spaces-archived') as HTMLDetailsElement
    expect(archived.open).toBe(false)
    expect(archived.textContent).toContain('Kindergarten 2024')
    expect(r.getByRole('region', { name: 'Yours' }).textContent).not.toContain('Kindergarten')
  })

  it('the Archived summary has one clear name', async () => {
    const r = await renderPage()
    await r.findByText('Choir')
    const sum = r.container.querySelector('summary.sh-spaces-archived__sum')!
    expect(sum.getAttribute('aria-label')).toBe('Archived spaces, 1')
  })

  it('a row is a link to the space with its members and picture', async () => {
    const r = await renderPage()
    const choir = (await r.findByText('Choir')).closest('a')!
    expect(choir.getAttribute('href')).toBe('/spaces/sA')
    expect(choir.textContent).toContain('8 members')
    // Uploaded icon wins; else the emoji; else the initial.
    expect(r.getByText('Garden').closest('a')!.querySelector('img')!.getAttribute('src')).toContain('/api/spaces/sG/icon')
    expect(r.getByText('Band').closest('a')!.querySelector('.sh-spaces-row__ic')!.textContent).toBe('B')
    // One member: "Just you".
    expect(r.getByText('Band').closest('a')!.textContent).toContain('Just you')
  })

  it('a long name is clamped to two lines, never cut to one', async () => {
    const r = await renderPage()
    const title = await r.findByText(ELTERNRAT)
    expect(title.classList.contains('sh-spaces-row__t')).toBe(true)
    expect(title.textContent).toBe(ELTERNRAT)
  })
})

describe('SpaceListPage — one number per row', () => {
  const withNew = (id: string, n: number) => SPACES.map((x) => (x.id === id ? { ...x, unread_posts: n } : x))

  it('needs-you wins over new posts; never both', async () => {
    wire({ spaces: withNew('sG', 5) })
    const r = await renderPage()
    const garden = (await r.findByText('Garden')).closest('a')!
    expect(garden.querySelector('.sh-spaces-attn')!.textContent).toBe('3')
    expect(garden.querySelector('.sh-spaces-new')).toBeNull()
    expect(garden.getAttribute('aria-label')).toContain('3 things need you')
  })

  it('a member sees the quiet count of new posts, named for screen readers', async () => {
    wire({ spaces: withNew('sA', 2) })
    const r = await renderPage()
    const choir = (await r.findByText('Choir')).closest('a')!
    expect(choir.querySelector('.sh-spaces-new')!.textContent).toBe('2 new')
    expect(choir.getAttribute('aria-label')).toContain('2 new posts')
    expect(choir.querySelector('.sh-spaces-attn')).toBeNull()
  })

  it('chat messages are not counted here — Chat keeps its own count', async () => {
    wire({ unread: { sA: { unread: 4, notif_level: 'all', muted_until: null } } })
    const r = await renderPage()
    const choir = (await r.findByText('Choir')).closest('a')!
    expect(choir.querySelector('.sh-spaces-new')).toBeNull()
    expect(apiGet.mock.calls.some((c) => c[0] === '/api/spaces/chat-unread')).toBe(false)
  })
})

describe('SpaceListPage — header and search', () => {
  it('has one primary "New space" and a visible "Join with a code"', async () => {
    const r = await renderPage()
    await r.findByText('Choir')
    expect(r.getAllByRole('button', { name: /New space/ })).toHaveLength(1)
    expect(r.getByRole('button', { name: 'Join with a code' })).toBeTruthy()
  })

  it('"Find a space" filters Yours, Following and Discover', async () => {
    const r = await renderPage()
    await r.findByText('Choir')
    await r.findByText('Hottingen Community Choir')
    r.fireEvent.input(r.getByPlaceholderText('Find a space'), { target: { value: 'choir' } })
    expect(r.getByText('Choir')).toBeTruthy()
    expect(r.queryByText('Band')).toBeNull()
    expect(r.queryByText('Kunstverein')).toBeNull()
    expect(r.getByText('Hottingen Community Choir')).toBeTruthy()
    r.fireEvent.input(r.getByPlaceholderText('Find a space'), { target: { value: 'zzz' } })
    expect(r.getByText('None of your spaces match “zzz”.')).toBeTruthy()
    expect(r.getByText('Nothing here matches “zzz”.')).toBeTruthy()
  })
})

describe('SpaceListPage — empty and error states', () => {
  it('no space at all: one card with the CTAs, none repeated in the header', async () => {
    wire({ spaces: [] })
    const r = await renderPage()
    await r.findByText('Start your first space')
    expect(r.getAllByRole('button', { name: /New space/ })).toHaveLength(1)
    expect(r.getAllByRole('button', { name: 'Join with a code' })).toHaveLength(1)
    expect(r.queryByPlaceholderText('Find a space')).toBeNull()
    // Discover still offers what can be joined.
    await r.findByText('Hottingen Community Choir')
  })

  it('a failed read shows the load error with Retry — never "no spaces"', async () => {
    wire({ spaces: new Error('offline') })
    const r = await renderPage()
    await r.findByText("Couldn't load your spaces.")
    expect(r.queryByText('Start your first space')).toBeNull()
    wire()
    r.fireEvent.click(r.getByRole('button', { name: 'Retry' }))
    await r.findByText('Choir')
  })
})

describe('SpaceListPage — Discover', () => {
  it('Your household lists what you can join: never invite-only, never yours', async () => {
    const r = await renderPage()
    await r.findByText('Hottingen Community Choir')
    const discover = r.getByRole('region', { name: 'Discover' })
    expect(discover.textContent).not.toContain('Secret club')
    expect(discover.textContent).not.toContain(ELTERNRAT)
    const chip = r.getByRole('button', { name: /Your household/ })
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(r.getByRole('group', { name: 'Where to look' })).toBeTruthy()
  })

  it('Join seats you through the join-request endpoint and re-reads your spaces', async () => {
    const r = await renderPage()
    await r.findByText('Hottingen Community Choir')
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Hottingen Community Choir' }))
    await r.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/spaces/hChoir/join-requests', {}))
    await r.waitFor(() => expect(apiGet.mock.calls.filter(c => c[0] === '/api/spaces').length).toBeGreaterThan(1))
  })

  it('a chip puts its source in the URL and shows paired spaces with their action', async () => {
    const r = await renderPage()
    await r.findByText('Hottingen Community Choir')
    r.fireEvent.click(r.getByRole('button', { name: /Paired households/ }))
    expect(location.pathname + location.search).toBe('/spaces?discover=paired')
    await r.findByText('Müller family market')
    expect(r.getByRole('button', { name: /Paired households/ }).getAttribute('aria-pressed')).toBe('true')
    expect(r.getByRole('button', { name: 'Ask to join: Müller family market' })).toBeTruthy()
    expect(r.getByRole('button', { name: 'Join: Hottingen bike repair' })).toBeTruthy()
    expect(r.getByText(/never listed on a GFS/)).toBeTruthy()
  })

  it('?discover=global with no GFS: an admin is offered "Add a GFS"', async () => {
    const r = await renderPage('/spaces?discover=global')
    const row = await r.findByTestId('discover-no-gfs')
    expect(row.textContent).toContain("Your household hasn't added one yet.")
    expect(r.getByRole('link', { name: 'Add a GFS' }).getAttribute('href')).toBe('/connections')
  })

  it('…and a member is told whom to ask', async () => {
    user.is_admin = false
    const r = await renderPage('/spaces?discover=global')
    const row = await r.findByTestId('discover-no-gfs')
    await r.waitFor(() => expect(row.textContent).toContain('Ask Pascal'))
    expect(r.queryByRole('link', { name: 'Add a GFS' })).toBeNull()
  })

  it('with a GFS, global spaces list; one you can only read along offers Follow', async () => {
    wire({
      gfs: [{ id: 'g1', status: 'active' }],
      global: [{
        space_id: 'gArt', host_instance_id: 'iid-far', host_display_name: 'iid-far', host_is_paired: false,
        name: 'Global art', description: null, emoji: '🖼', member_count: 90, join_mode: 'request',
        min_age: 0, allow_subscribers: true,
      }],
    })
    const r = await renderPage('/spaces?discover=global')
    await r.findByText('Global art')
    expect(r.getByRole('button', { name: 'Follow: Global art' })).toBeTruthy()
    r.fireEvent.click(r.getByRole('button', { name: 'Follow: Global art' }))
    await r.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/spaces/gArt/subscribe', {}))
  })

  it('asking to join shows "Requested" at once, no reload', async () => {
    const r = await renderPage('/spaces?discover=paired')
    await r.findByText('Müller family market')
    r.fireEvent.click(r.getByRole('button', { name: 'Ask to join: Müller family market' }))
    const dialog = await r.findByRole('dialog')
    const send = [...dialog.querySelectorAll('button')].find((b) => /send/i.test(b.textContent ?? ''))!
    r.fireEvent.click(send)
    await r.findByRole('button', { name: 'Requested: Müller family market' })
    expect(apiPost).toHaveBeenCalledWith('/api/public_spaces/pMarket/join-request', expect.objectContaining({ host_instance_id: 'iid-mueller' }))
  })

  it('a local Join that turns into a request shows "Requested" in place', async () => {
    apiPost.mockResolvedValue({ request_id: 'r1', joined: false })
    const r = await renderPage()
    await r.findByText('Hottingen Community Choir')
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Hottingen Community Choir' }))
    await r.findByRole('button', { name: 'Requested: Hottingen Community Choir' })
  })

  it('a global space you can neither join nor follow: no button, never "pair"', async () => {
    wire({
      gfs: [{ id: 'g1', status: 'active' }],
      global: [{
        space_id: 'gClosed', host_instance_id: 'iid-far', host_display_name: 'iid-far', host_is_paired: false,
        name: 'Closed circle', description: null, emoji: null, member_count: 4, join_mode: 'request',
        min_age: 0, allow_subscribers: false,
      }],
    })
    const r = await renderPage('/spaces?discover=global')
    const name = await r.findByText('Closed circle')
    const row = name.closest('li')!
    expect(row.querySelector('button')).toBeNull()
    expect(row.textContent).toContain('members can invite you with a link')
    expect(row.textContent).not.toMatch(/connect|pair/i)
  })
})
