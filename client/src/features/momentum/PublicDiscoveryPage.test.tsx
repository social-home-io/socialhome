import { describe, it, expect } from 'vitest'

describe('PublicDiscoveryPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./PublicDiscoveryPage')
    expect(typeof m.default).toBe('function')
  })
})

describe('discoveryAvatarUrl', () => {
  const user = (over: Record<string, unknown>) => ({
    user_id: 'u1', instance_id: 'i', username: 'a', display_name: 'A',
    picture_url: 'https://tracker.example/p.png', bio: null,
    picture_digest: null, ...over,
  }) as never

  it('never falls back to the directory-supplied picture_url', async () => {
    // A household registers any picture_url it likes with the GFS — an
    // https tracker, or a path that resolves against OUR origin. Only the
    // GFS-mirrored picture, fetched through our own backend, is safe.
    const { discoveryAvatarUrl } = await import('./PublicDiscoveryPage')
    expect(discoveryAvatarUrl(user({}), 'gfs-1')).toBeNull()
    expect(discoveryAvatarUrl(user({ picture_digest: 'abc' }), null)).toBeNull()
  })

  it('uses the GFS-mirrored picture through our backend when there is a digest', async () => {
    const m = await import('./PublicDiscoveryPage')
    expect(m.discoveryAvatarUrl(user({ picture_digest: 'abc' }), 'gfs-1')).toBe(
      'api/gfs/gfs-1/moments/users/u1/picture?v=abc',
    )
  })
})
