import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({ openPerson: openPersonSpy }))
const me = vi.hoisted(() => ({ value: { user_id: 'u-me', restrictions: [] as string[] } }))
vi.mock('@/store/auth', () => ({ currentUser: me }))

import { api } from '@/api'
import { blockedUserIds } from '@/store/blocks'
import { _resetGfsConnectionsForTests, follows } from '@/store/momentPublic'
import PublicDiscoveryPage, { _resetDiscoverForTests } from './PublicDiscoveryPage'

/** ``GET /api/friends`` for a household paired with no one. */
const NO_FRIENDS = {
  instance: { instance_id: 'i-own', display_name: 'Home', members: [], member_count: 0 },
  households: [], totals: { households: 1, people: 0 },
}

describe('PublicDiscoveryPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./PublicDiscoveryPage')
    expect(typeof m.default).toBe('function')
  })
})

describe('discoveryAvatarUrl', () => {
  const user = (over: Record<string, unknown>) => ({
    user_id: 'u1', instance_id: 'i', username: 'a', display_name: 'A',
    picture_url: null, bio: null, picture_digest: null, ...over,
  }) as never

  it('uses the signed proxy URL the backend put in picture_url', async () => {
    // An <img> carries no bearer, so the backend replaces the row's
    // picture_url with our own signed picture-proxy path.
    const { discoveryAvatarUrl } = await import('./PublicDiscoveryPage')
    const signed = 'api/gfs/gfs-1/moments/users/u1/picture?v=abc&exp=9&sig=s'
    expect(discoveryAvatarUrl(user({ picture_url: signed, picture_digest: 'abc' })))
      .toBe(signed)
  })

  it('shows initials when there is no mirrored picture', async () => {
    const { discoveryAvatarUrl } = await import('./PublicDiscoveryPage')
    expect(discoveryAvatarUrl(user({}))).toBeNull()
  })

  it('never loads a URL that is not our own picture proxy', async () => {
    // A household registers any picture_url it likes with the GFS — an
    // https tracker, or a path that resolves against OUR origin. Only
    // our backend's picture proxy is ever loaded.
    const { discoveryAvatarUrl } = await import('./PublicDiscoveryPage')
    for (const bad of [
      'https://tracker.example/p.png',
      '//tracker.example/p.png',
      'api/users/u1/picture?v=abc',
      'api/media/x.webp',
      'api/gfs/gfs-1/moments/users/u1/../../../media/x?v=1',
    ]) {
      expect(discoveryAvatarUrl(user({ picture_url: bad, picture_digest: 'abc' })))
        .toBeNull()
    }
  })
})

describe('PublicDiscoveryPage when a load fails', () => {
  const gfs = (id: string, name: string) => ({ id, display_name: name, status: 'active' })
  const marco = { user_id: 'u-marco', instance_id: 'i', username: 'marco', display_name: 'Marco Rossi',
    picture_url: null, bio: null, picture_digest: null }

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    _resetGfsConnectionsForTests()
    _resetDiscoverForTests()
  })

  it('failed GFS connections are an error with Try again, not "not connected to a GFS"', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') {
        if (fail) throw new Error('offline')
        return [gfs('g1', 'Demo GFS')]
      }
      if (path === '/api/gfs/g1/moments/users') return { users: [marco] }
      return path === '/api/friends' ? NO_FRIENDS : []
    })
    const { findByRole, container, unmount } = render(<PublicDiscoveryPage />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load your GFS connections")
    expect(container.textContent).not.toContain('Not connected to a GFS yet')
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Marco Rossi'))
    unmount()
  })

  it('an unreachable GFS says so — never "no one has signed up"', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return [gfs('g2', 'Quiet GFS')]
      if (path === '/api/gfs/g2/moments/users') throw new Error('offline')
      return path === '/api/friends' ? NO_FRIENDS : []
    })
    const { findByRole, container, unmount } = render(<PublicDiscoveryPage />)
    expect((await findByRole('alert')).textContent).toContain("Can't reach Quiet GFS.")
    expect(container.textContent).not.toContain('No one has signed up')
    unmount()
  })

  it('keeps the last results with their age when the GFS stops answering', async () => {
    let fail = false
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return [gfs('g3', 'Demo GFS')]
      if (path === '/api/gfs/g3/moments/users') {
        if (fail) throw new Error('offline')
        return { users: [marco] }
      }
      return path === '/api/friends' ? NO_FRIENDS : []
    })
    const first = render(<PublicDiscoveryPage />)
    await waitFor(() => expect(first.container.textContent).toContain('Marco Rossi'))
    first.unmount()
    fail = true
    const { findByRole, container } = render(<PublicDiscoveryPage />)
    expect((await findByRole('alert')).textContent)
      .toMatch(/Can't reach Demo GFS\. These results are from just now\./)
    expect(container.textContent).toContain('Marco Rossi')
  })
})

describe('Discover tab', () => {
  const gfs = (id: string, name: string) => ({ id, display_name: name, status: 'active' })
  const person = (id: string, name: string, instance: string, over: Record<string, unknown> = {}) => ({
    user_id: id, instance_id: instance, username: name.toLowerCase().split(' ')[0],
    display_name: name, picture_url: null, bio: null, picture_digest: null, ...over,
  })
  const marco = person('u-marco', 'Marco Rossi', 'i-rossi', { bio: 'Pizza and bikes' })
  const meRow = person('u-me', 'Me Myself', 'i-own')
  const blockedRow = person('u-bert', 'Blocked Bert', 'i-far')
  const member = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
    user_id: id, username: name.toLowerCase(), remote_username: name.toLowerCase(),
    display_name: name, picture_url: null, ...extra,
  })
  const friendsPayload = (withPeople = true) => ({
    instance: {
      instance_id: 'i-own', display_name: 'Our home', home_lat: null, home_lon: null,
      members: withPeople ? [member('u-me', 'Me Myself'), member('u-lena', 'Lena Berger')] : [member('u-me', 'Me Myself')],
      member_count: withPeople ? 2 : 1,
    },
    households: withPeople
      ? [{
        instance_id: 'i-keller', display_name: 'Kellers', home_lat: null, home_lon: null,
        paired_at: null, reachable: true, member_count: 1,
        members: [member('u-sophie', 'Sophie Keller', { instance_id: 'i-keller' })],
      }]
      : [],
    totals: { households: 2, people: 3 },
  })
  let gfses = [gfs('g1', 'Demo GFS')]
  let withFriends = true

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
    openPersonSpy.mockReset()
    _resetGfsConnectionsForTests()
    _resetDiscoverForTests()
    follows.value = []
    gfses = [gfs('g1', 'Demo GFS')]
    withFriends = true
    blockedUserIds.value = new Set(['u-bert'])
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return gfses
      // The household's proxy already left out your friends; a stale one
      // might still list you, and someone you blocked stays the client's job.
      if (path === '/api/gfs/g1/moments/users') return { users: [meRow, marco, blockedRow] }
      if (path === '/api/friends') return friendsPayload(withFriends)
      if (path === '/api/moments/public/follows') return []
      if (path === '/api/moments/follows') return { follows: [] }
      if (path === '/api/blocks') return { blocks: [{ user_id: 'u-bert', blocked_at: '' }] }
      return path === '/api/friends' ? NO_FRIENDS : []
    })
  })

  const headings = (c: Element) => [...c.querySelectorAll('h2')].map((h) => h.textContent)

  it('lists friends first, then the GFS — each person once, with what each follow tells whom', async () => {
    const { findByRole, container, getByRole, queryAllByRole } = render(<PublicDiscoveryPage />)
    const gfsList = await findByRole('list', { name: 'People sharing on Demo GFS' })
    await waitFor(() => expect(gfsList.textContent).toContain('Marco Rossi'))
    expect(headings(container)).toEqual(['Friends', 'People sharing on Demo GFS'])
    // You and people you blocked are never in the GFS list; your friends are
    // in Friends (the proxy dropped them from the directory).
    expect(gfsList.textContent).not.toContain('Me Myself')
    expect(gfsList.textContent).not.toContain('Blocked Bert')
    const friendsList = getByRole('list', { name: 'Friends' })
    expect(friendsList.textContent).toContain('Sophie Keller')
    expect(friendsList.textContent).toContain('Lena Berger')
    expect(friendsList.textContent).not.toContain('Me Myself')
    expect(getByRole('button', { name: 'Follow Marco Rossi' }).getAttribute('aria-pressed')).toBe('false')
    expect(container.textContent).toContain(
      'Following someone here tells Demo GFS that your household follows them.')
    // One search box filters both sections here — the GFS never sees it.
    const search = getByRole('searchbox', { name: 'Find people' })
    fireEvent.input(search, { target: { value: 'pizza' } })
    expect(queryAllByRole('button', { name: 'Marco Rossi' }).length).toBe(1)
    expect(container.textContent).not.toContain('Sophie Keller')
    const directoryParams = vi.mocked(api.get).mock.calls
      .filter(([path]) => path === '/api/gfs/g1/moments/users')
      .map(([, params]) => params as Record<string, string> | undefined)
    expect(directoryParams.length).toBeGreaterThan(0)
    expect(directoryParams.every((p) => !p || !('q' in p))).toBe(true)
  })

  it('Follow goes through the GFS and flips to Following', async () => {
    vi.mocked(api.post).mockResolvedValue({
      follower_user_id: 'u-me', followed_user_id: 'u-marco', gfs_id: 'g1',
      followed_username: 'marco', followed_display_name: 'Marco Rossi', created_at: '',
    })
    const { findByRole } = render(<PublicDiscoveryPage />)
    const btn = await findByRole('button', { name: 'Follow Marco Rossi' })
    fireEvent.click(btn)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/moments/public/follows', { gfs_id: 'g1', followed_user_id: 'u-marco' }))
    await waitFor(async () => expect((await findByRole('button', { name: 'Follow Marco Rossi' }))
      .getAttribute('aria-pressed')).toBe('true'))
  })

  it('a name opens the person sheet, as someone known through this GFS', async () => {
    const { findByRole } = render(<PublicDiscoveryPage />)
    fireEvent.click(await findByRole('button', { name: 'Marco Rossi' }))
    expect(openPersonSpy).toHaveBeenCalledWith('u-marco', {
      person: expect.objectContaining({
        display_name: 'Marco Rossi', bio: 'Pizza and bikes', public_via: { gfs_id: 'g1' },
      }),
    })
  })

  it('a protected account gets Friends only — no GFS is asked', async () => {
    me.value = { user_id: 'u-kid', restrictions: ['public_moments'] }
    const { findByRole, container } = render(<PublicDiscoveryPage />)
    expect(await findByRole('list', { name: 'Friends' })).toBeTruthy()
    expect(headings(container)).toEqual(['Friends'])
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/connections')
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/g1/moments/users')
    me.value = { user_id: 'u-me', restrictions: [] }
  })

  it('no friends yet but a GFS: the GFS list leads, Friends is one line under it', async () => {
    withFriends = false
    const { findByRole, container } = render(<PublicDiscoveryPage />)
    await findByRole('list', { name: 'People sharing on Demo GFS' })
    await waitFor(() => expect(container.textContent).toContain('Pair with a household'))
    expect(headings(container)).toEqual(['People sharing on Demo GFS'])
    const text = container.textContent!
    expect(text.indexOf('Marco Rossi')).toBeLessThan(text.indexOf('Pair with a household'))
  })

  it('no friends and no GFS: one card, not two empty states', async () => {
    withFriends = false
    gfses = []
    const { findByText, container, getByRole } = render(<PublicDiscoveryPage />)
    await findByText('Pair with a household to follow its people here.')
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/gfs/connections'))
    await Promise.resolve()
    expect(container.textContent).not.toContain('Connect to a GFS')
    expect(container.querySelector('input[type="search"]')).toBeNull()
    expect(getByRole('link', { name: 'Open Connections' }).getAttribute('href')).toBe('/connections')
  })

  it('friends that fail to load are an error in Friends only; the GFS list still shows', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return gfses
      if (path === '/api/gfs/g1/moments/users') return { users: [marco] }
      if (path === '/api/friends') throw new Error('offline')
      return []
    })
    const { findByRole } = render(<PublicDiscoveryPage />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load your friends")
    const gfsList = await findByRole('list', { name: 'People sharing on Demo GFS' })
    await waitFor(() => expect(gfsList.textContent).toContain('Marco Rossi'))
  })

  it('never shows the friends read on an earlier visit (another account)', async () => {
    const first = render(<PublicDiscoveryPage />)
    await waitFor(() => expect(first.container.textContent).toContain('Sophie Keller'))
    first.unmount()
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return gfses
      if (path === '/api/friends') throw new Error('offline')
      return []
    })
    const { findByRole, container } = render(<PublicDiscoveryPage />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load your friends")
    expect(container.textContent).not.toContain('Sophie Keller')
  })

  it('friends but no GFS: Friends, then a short line about a GFS', async () => {
    gfses = []
    const { findByRole, findByText } = render(<PublicDiscoveryPage />)
    expect(await findByRole('list', { name: 'Friends' })).toBeTruthy()
    expect(await findByText('Connect to a GFS (Global Federation Server) first to find other people.')).toBeTruthy()
  })
})

describe('Discover › ranking the GFS list', () => {
  const gfs = { id: 'g1', display_name: 'Demo GFS', status: 'active' }
  const author = (id: string, name: string, count: number) => ({
    user_id: id, instance_id: `i-${id}`, username: id, display_name: name,
    picture_url: null, bio: null, picture_digest: null, follower_count: count,
  })
  const ranked = [author('dana', 'Dana Rivers', 1204), author('ivo', 'Ivo', 1), author('gus', 'Gus', 0)]
  const newest = [author('gus', 'Gus', 0), author('ivo', 'Ivo', 1), author('dana', 'Dana Rivers', 1204)]
  let ranks = true
  let down = false

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    _resetGfsConnectionsForTests()
    _resetDiscoverForTests()
    follows.value = []
    ranks = true
    down = false
    vi.mocked(api.get).mockImplementation(async (path: string, params?: Record<string, string>) => {
      if (path === '/api/gfs/connections') return [gfs]
      if (path === '/api/gfs/g1/moments/users') {
        if (down) throw new Error('offline')
        const sort = params?.sort
        return ranks
          ? { users: sort === 'new' ? newest : ranked, sort }
          : { users: newest.map((u) => ({ ...u, follower_count: undefined })) }
      }
      if (path === '/api/friends') return NO_FRIENDS
      return []
    })
  })

  const names = (list: HTMLElement) =>
    [...list.querySelectorAll('.sh-momentum-discover-name')].map((b) => b.textContent)

  it('starts with Most followed and shows compact counts — nothing for 0', async () => {
    const { findByRole, getByRole } = render(<PublicDiscoveryPage />)
    const list = await findByRole('list', { name: 'People sharing on Demo GFS' })
    await waitFor(() => expect(names(list)).toEqual(['Dana Rivers', 'Ivo', 'Gus']))
    expect(api.get).toHaveBeenCalledWith('/api/gfs/g1/moments/users', { sort: 'followers' })
    const group = getByRole('group', { name: 'Order' })
    const most = getByRole('button', { name: 'Most followed' })
    expect(group.contains(most)).toBe(true)
    expect(most.getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: 'Newest' }).getAttribute('aria-pressed')).toBe('false')
    const rows = [...list.querySelectorAll('li')].map((li) => li.textContent)
    expect(rows[0]).toContain('1.2K followers')
    expect(rows[1]).toContain('1 follower')
    expect(rows[1]).not.toContain('1 followers')
    expect(rows[2]).not.toContain('follower')
  })

  it('Newest reads the list in that order and says so', async () => {
    const { findByRole, getByRole } = render(<PublicDiscoveryPage />)
    await findByRole('button', { name: 'Most followed' })
    fireEvent.click(getByRole('button', { name: 'Newest' }))
    await waitFor(() => expect(names(getByRole('list', { name: 'People sharing on Demo GFS' })))
      .toEqual(['Gus', 'Ivo', 'Dana Rivers']))
    expect(api.get).toHaveBeenCalledWith('/api/gfs/g1/moments/users', { sort: 'new' })
    expect(getByRole('button', { name: 'Newest' }).getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelector('[role="status"].sh-discover-sort__said')!.textContent)
      .toBe('Sorted by newest')
  })

  it('a GFS that does not rank: no sort buttons and no counts', async () => {
    ranks = false
    const { findByRole, queryByRole } = render(<PublicDiscoveryPage />)
    const list = await findByRole('list', { name: 'People sharing on Demo GFS' })
    await waitFor(() => expect(names(list)).toEqual(['Gus', 'Ivo', 'Dana Rivers']))
    expect(queryByRole('button', { name: 'Most followed' })).toBeNull()
    expect(list.textContent).not.toContain('follower')
  })

  it('an unreachable GFS keeps its list; the sort buttons do nothing', async () => {
    const first = render(<PublicDiscoveryPage />)
    await first.findByRole('button', { name: 'Most followed' })
    first.unmount()
    down = true
    const { findByRole, getByRole } = render(<PublicDiscoveryPage />)
    await findByRole('alert')
    const newestBtn = getByRole('button', { name: 'Newest' })
    expect(newestBtn.getAttribute('aria-disabled')).toBe('true')
    const calls = vi.mocked(api.get).mock.calls.length
    fireEvent.click(newestBtn)
    expect(vi.mocked(api.get).mock.calls.length).toBe(calls)
  })
})
