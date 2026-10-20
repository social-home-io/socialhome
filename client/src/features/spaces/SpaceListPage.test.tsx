import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const confirmMock = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmMock(...a) }))
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

function emit(type: string, data: Record<string, unknown>): void {
  for (const h of wsHandlers[type] ?? []) h({ type, data })
}

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
    // Read as "name, meta" like every other row — not "name Archived · …".
    expect(archived.querySelector('a.sh-spaces-row__main')?.getAttribute('aria-label'))
      .toBe('Kindergarten 2024, Archived · read-only')
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
    // One member, and it is the viewer: "Just you".
    expect(r.getByText('Band').closest('a')!.textContent).toContain('Just you')
  })

  it('"Just you" only when the one member is the viewer — a followed space or a Discover row says "1 member"', async () => {
    wire({
      spaces: [
        ...SPACES,
        { id: 'sF', name: 'Solo blog', space_type: 'global', emoji: null, my_role: 'subscriber', member_count: 1, last_activity_at: null, needs_you: null },
      ],
    })
    const r = await renderPage()
    // A follower is never counted, so the one member is the owner.
    const followed = (await r.findByText('Solo blog')).closest('a')!
    expect(followed.textContent).toContain('1 member')
    expect(followed.textContent).not.toContain('Just you')
    // Discover lists spaces the viewer is not in.
    const discover = r.getByRole('region', { name: 'Discover' })
    const choir = (await r.findByText('Hottingen Community Choir')).closest('li')!
    expect(discover.contains(choir)).toBe(true)
    expect(choir.textContent).toContain('1 member')
    expect(discover.textContent).not.toContain('Just you')
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

  it('a new post by someone else bumps it live; my own post does not', async () => {
    const r = await renderPage()
    const choir = (await r.findByText('Choir')).closest('a')!
    emit('space.post.created', { space_id: 'sA', post: { author: 'u1', created_at: '2030-01-01T00:00:00+00:00' } })
    expect(choir.querySelector('.sh-spaces-new')).toBeNull()
    emit('space.post.created', { space_id: 'sA', post: { author: 'u2', created_at: '2030-01-01T00:00:01+00:00' } })
    emit('space.post.created', { space_id: 'sA', post: { author: 'u3', created_at: '2030-01-01T00:00:02+00:00' } })
    await r.waitFor(() => expect(r.getByText('Choir').closest('a')!.querySelector('.sh-spaces-new')?.textContent).toBe('2 new'))
  })

  it('a WS reconnect re-reads the list (frames missed while down)', async () => {
    const r = await renderPage()
    await r.findByText('Choir')
    wire({ spaces: SPACES.map((x) => (x.id === 'sA' ? { ...x, unread_posts: 4 } : x)) })
    conn.state!.value = 'reconnecting'
    conn.state!.value = 'open'
    await r.findByText('4 new')
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

  it('a paired household\'s local community shows its radius only, and joining asks first', async () => {
    const paired = [
      { ...PAIRED[1], space_id: 'pPlain', name: 'Chess club', local_community: null },
      { ...PAIRED[1], space_id: 'pLocal', name: 'Hottingen street party', local_community: { radius_km: 5 } },
    ]
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/peer_spaces') return paired
      if (url === '/api/friends') return { instance: { home_lat: 47.392, home_lon: 8.56 } }
      return base(url)
    })
    const r = await renderPage('/spaces?discover=paired')
    await r.findByText('Hottingen street party')
    const row = (name: string) => r.getByText(name).closest('li') as HTMLElement
    expect(row('Hottingen street party').textContent).toContain('Within 5 km')
    // No verdict: the host decides when the request arrives.
    expect(row('Hottingen street party').querySelector('[data-testid="local-area-badge"]')).toBeNull()
    expect(r.queryByTestId('discover-near-you')).toBeNull()
    // The order is the server's (members), nothing sorted by any verdict.
    const names = [...r.container.querySelectorAll('#discover .sh-spaces-row__t')].map((n) => n.textContent)
    expect(names).toEqual(['Chess club', 'Hottingen street party'])
    // Join: the consent first — No sends nothing, Yes sends the location.
    confirmMock.mockResolvedValueOnce(false)
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Hottingen street party' }))
    await r.waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1))
    expect(String(confirmMock.mock.calls[0][0])).toContain('approximate location (about 1 km)')
    expect(apiPost).not.toHaveBeenCalledWith('/api/public_spaces/pLocal/join-request', expect.anything())
    confirmMock.mockResolvedValueOnce(true)
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Hottingen street party' }))
    await r.waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/public_spaces/pLocal/join-request',
      { host_instance_id: PAIRED[1].host_instance_id, share_location: true },
    ))
    // An ordinary space sends nothing about our home.
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Chess club' }))
    await r.waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/public_spaces/pPlain/join-request',
      { host_instance_id: PAIRED[1].host_instance_id },
    ))
  })

  it('no household home: joining a local community explains where it comes from, sends nothing', async () => {
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/peer_spaces') return [{ ...PAIRED[1], space_id: 'pLocal', name: 'Rim garden', local_community: { radius_km: 5 } }]
      if (url === '/api/friends') return { instance: { home_lat: null, home_lon: null } }
      return base(url)
    })
    const r = await renderPage('/spaces?discover=paired')
    await r.findByText('Rim garden')
    confirmMock.mockResolvedValueOnce(true)
    r.fireEvent.click(r.getByRole('button', { name: 'Join: Rim garden' }))
    await r.waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1))
    expect(String(confirmMock.mock.calls[0][0])).toContain('no home location yet')
    expect(apiPost).not.toHaveBeenCalledWith('/api/public_spaces/pLocal/join-request', expect.anything())
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

  it('global spaces sort by distance from our home, worked out here; unpinned ones last', async () => {
    const g = (space_id: string, name: string, pin?: { lat: number; lon: number }) => ({
      space_id, host_instance_id: 'iid-far', host_display_name: 'iid-far', host_is_paired: false,
      name, description: null, emoji: '♟', member_count: 5, join_mode: 'request' as const,
      min_age: 0, allow_subscribers: true, lat: pin?.lat ?? null, lon: pin?.lon ?? null, radius_km: null,
    })
    wire({
      gfs: [{ id: 'g1', status: 'active' }],
      global: [
        g('gNone', 'Chess online'),
        g('gBern', 'Chess Bern', { lat: 46.9481, lon: 7.4474 }),
        g('gZh', 'Chess Zürich', { lat: 47.3769, lon: 8.5417 }),
      ],
    })
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => (
      url === '/api/friends' ? { instance: { home_lat: 47.37, home_lon: 8.563 } } : base(url)))
    const r = await renderPage('/spaces?discover=global')
    await r.findByText('Chess Bern')
    const names = () => [...r.container.querySelectorAll('#discover .sh-spaces-row__t')].map((n) => n.textContent)
    await r.waitFor(() => expect(names()).toEqual(['Chess Zürich', 'Chess Bern', 'Chess online']))
    const row = (name: string) => r.getByText(name).closest('li') as HTMLElement
    expect(row('Chess Zürich').textContent).toContain('about 2 km away')
    expect(row('Chess Bern').textContent).toContain('about 95 km away')
    expect(row('Chess online').textContent).not.toContain('away')
    // Our home stays in the browser: no request carries it.
    for (const call of apiGet.mock.calls) expect(String(call[0])).not.toMatch(/47\.37|lat=|home/)
  })

  it('a global radius hint: "Within the area" or how far outside; "Near me" narrows to them', async () => {
    const g = (space_id: string, name: string, pin?: { lat: number; lon: number; radius_km?: number }) => ({
      space_id, host_instance_id: 'iid-far', host_display_name: 'iid-far', host_is_paired: false,
      name, description: null, emoji: '♟', member_count: 5, join_mode: 'request' as const,
      min_age: 0, allow_subscribers: true, lat: pin?.lat ?? null, lon: pin?.lon ?? null,
      radius_km: pin?.radius_km ?? null,
    })
    wire({
      gfs: [{ id: 'g1', status: 'active' }],
      global: [
        g('gNone', 'Chess online'),
        g('gBern', 'Chess Bern', { lat: 46.9481, lon: 7.4474, radius_km: 10 }),
        g('gZh', 'Chess Zürich', { lat: 47.3769, lon: 8.5417, radius_km: 10 }),
        g('gPin', 'Go Zürich', { lat: 47.3769, lon: 8.5417 }),
      ],
    })
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => (
      url === '/api/friends' ? { instance: { home_lat: 47.37, home_lon: 8.563 } } : base(url)))
    const r = await renderPage('/spaces?discover=global')
    await r.findByText('Chess Bern')
    const row = (name: string) => r.getByText(name).closest('li') as HTMLElement
    await r.waitFor(() => expect(row('Chess Zürich').textContent).toContain('Within the area'))
    expect(row('Chess Bern').textContent).toContain('about 95 km away · outside the suggested 10 km')
    // A hint only: the join stays on offer either way.
    expect(r.getByRole('button', { name: /Chess Bern/ })).toBeTruthy()
    // No hint: the plain distance, as before.
    expect(row('Go Zürich').textContent).toContain('about 2 km away')
    const names = () => [...r.container.querySelectorAll('#discover .sh-spaces-row__t')].map((n) => n.textContent)
    const near = r.getByTestId('discover-near-me')
    expect(near.getAttribute('aria-pressed')).toBe('false')
    r.fireEvent.click(near)
    expect(near.getAttribute('aria-pressed')).toBe('true')
    expect(names()).toEqual(['Chess Zürich'])
    r.fireEvent.click(near)
    expect(names()).toHaveLength(4)
    // Our home stays in the browser: no request carries it.
    for (const call of [...apiGet.mock.calls, ...apiPost.mock.calls]) {
      expect(JSON.stringify(call)).not.toMatch(/47\.37[^6]|8\.563|home_lat":4|lat=|near/)
    }
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
