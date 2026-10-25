import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import PublicDiscoveryPage from './PublicDiscoveryPage'

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

  beforeEach(() => { vi.mocked(api.get).mockReset() })

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
