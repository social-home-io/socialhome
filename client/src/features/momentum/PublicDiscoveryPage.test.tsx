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
import { _resetGfsConnectionsForTests, follows } from '@/store/momentPublic'
import PublicDiscoveryPage, { _resetDiscoverForTests } from './PublicDiscoveryPage'

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
      return []
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
      return []
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
      return []
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
  const sophie = person('u-sophie', 'Sophie Keller', 'i-keller')
  const lena = person('u-lena', 'Lena Berger', 'i-own')

  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
    openPersonSpy.mockReset()
    _resetGfsConnectionsForTests()
    _resetDiscoverForTests()
    follows.value = []
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/gfs/connections') return [gfs('g1', 'Demo GFS')]
      if (path === '/api/gfs/g1/moments/users') return { users: [marco, sophie, lena] }
      if (path === '/api/friends') {
        return { instance: { instance_id: 'i-own' }, households: [{ instance_id: 'i-keller' }] }
      }
      if (path === '/api/moments/public/follows') return []
      return []
    })
  })

  it('names the GFS, marks paired and own people instead of offering a GFS follow, and says what a follow tells the GFS', async () => {
    const { findByRole, container, getByRole, queryAllByRole } = render(<PublicDiscoveryPage />)
    expect((await findByRole('heading', { name: 'People sharing on Demo GFS' }))).toBeTruthy()
    await waitFor(() => expect(container.textContent).toContain('Sophie Keller'))
    const rows = [...container.querySelectorAll('.sh-momentum-discover-row')] as HTMLElement[]
    const row = (name: string) => rows.find(r => r.textContent!.includes(name))!
    expect(row('Marco Rossi').querySelector('button[aria-pressed]')?.textContent).toBe('Follow')
    expect(row('Sophie Keller').textContent).toContain('Paired with you — you already see their moments')
    expect(row('Sophie Keller').querySelector('button[aria-pressed]')).toBeNull()
    expect(row('Lena Berger').textContent).toContain('In your household')
    expect(row('Lena Berger').querySelector('button[aria-pressed]')).toBeNull()
    expect(container.textContent).toContain(
      'Following someone here tells Demo GFS that your household follows them.')
    // The search box is labelled, and filters here — the GFS never sees it.
    const search = getByRole('searchbox', { name: 'Find people who share publicly' })
    fireEvent.input(search, { target: { value: 'pizza' } })
    expect(queryAllByRole('button', { name: 'Marco Rossi' }).length).toBe(1)
    expect(container.textContent).not.toContain('Sophie Keller')
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/g1/moments/users', expect.anything())
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
    const on = await findByRole('button', { name: 'Following Marco Rossi' })
    expect(on.getAttribute('aria-pressed')).toBe('true')
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

  it('a protected account sees why there is no Discover', async () => {
    me.value = { user_id: 'u-kid', restrictions: ['public_moments'] }
    const { container } = render(<PublicDiscoveryPage />)
    expect(container.querySelector('.sh-protected-notice, [data-testid="protected-notice"]')
      ?? container.textContent).toBeTruthy()
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/g1/moments/users')
    me.value = { user_id: 'u-me', restrictions: [] }
  })
})
