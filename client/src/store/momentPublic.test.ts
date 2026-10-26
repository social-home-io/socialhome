import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiDelete = vi.fn()
const apiPatch = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
  },
}))

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiDelete.mockReset()
  apiPatch.mockReset()
})

describe('momentPublic store', () => {
  it('loadRegistrations populates the cache from the API', async () => {
    apiGet.mockResolvedValueOnce([
      {
        user_id: 'u1',
        gfs_id: 'g1',
        registered_at: '2026-05-06',
        default_share: true,
      },
    ])
    const mod = await import('./momentPublic')
    await mod.loadRegistrations()
    expect(mod.registrations.value).toHaveLength(1)
    expect(mod.registrations.value[0].gfs_id).toBe('g1')
  })

  it('registerOnGfs writes through and updates cache', async () => {
    const mod = await import('./momentPublic')
    mod.registrations.value = []
    apiPost.mockResolvedValueOnce({
      user_id: 'u1',
      gfs_id: 'g2',
      registered_at: '2026-05-06',
      default_share: true,
    })
    await mod.registerOnGfs('g2', true)
    expect(mod.registrations.value.some((r) => r.gfs_id === 'g2')).toBe(true)
    expect(apiPost).toHaveBeenCalledWith('/api/moments/public/registrations', {
      gfs_id: 'g2',
      default_share: true,
    })
  })

  it('registerOnGfs without a choice leaves default_share to the server', async () => {
    const mod = await import('./momentPublic')
    apiPost.mockResolvedValueOnce({
      user_id: 'u1', gfs_id: 'g3', registered_at: '2026-05-06', default_share: false,
    })
    await mod.registerOnGfs('g3')
    expect(apiPost).toHaveBeenLastCalledWith('/api/moments/public/registrations', { gfs_id: 'g3' })
  })

  it('deregisterFromGfs drops the row from the cache', async () => {
    const mod = await import('./momentPublic')
    mod.registrations.value = [
      {
        user_id: 'u1',
        gfs_id: 'g1',
        registered_at: '2026-05-06',
        default_share: true,
      },
    ]
    apiDelete.mockResolvedValueOnce(undefined)
    await mod.deregisterFromGfs('g1')
    expect(mod.registrations.value).toHaveLength(0)
  })

  it('followUser caches the returned follow row', async () => {
    const mod = await import('./momentPublic')
    mod.follows.value = []
    apiPost.mockResolvedValueOnce({
      follower_user_id: 'u1',
      followed_user_id: 'u-remote',
      gfs_id: 'g1',
      followed_username: 'bob',
      followed_display_name: 'Bob',
      created_at: '2026-05-06',
    })
    const f = await mod.followUser('g1', 'u-remote')
    expect(f.followed_username).toBe('bob')
    expect(mod.follows.value).toHaveLength(1)
  })

  it('fetchGfsDirectory returns the users array', async () => {
    apiGet.mockResolvedValueOnce({
      users: [{ user_id: 'u-remote', display_name: 'Bob' }],
    })
    const mod = await import('./momentPublic')
    const list = await mod.fetchGfsDirectory('g1')
    expect(list).toHaveLength(1)
  })
})
