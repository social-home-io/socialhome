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
