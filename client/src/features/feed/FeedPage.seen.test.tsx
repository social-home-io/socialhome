import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
const markSeen = vi.hoisted(() => vi.fn())
vi.mock('@/store/notifications', async (orig) => ({
  ...(await orig<typeof import('@/store/notifications')>()),
  markSeen,
}))

import { api } from '@/api'

describe('FeedPage reads its "new posts" notification', () => {
  beforeEach(() => {
    markSeen.mockReset()
    vi.mocked(api.get).mockReset()
  })

  it('marks /feed seen once the feed has loaded', async () => {
    vi.mocked(api.get).mockResolvedValue([])
    const { default: FeedPage } = await import('./FeedPage')
    render(<FeedPage />)
    await waitFor(() => expect(markSeen).toHaveBeenCalledWith('/feed'))
  })

  it('does not mark it seen when the feed failed to load', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.startsWith('/api/feed')) throw new Error('offline')
      return []
    })
    const { default: FeedPage } = await import('./FeedPage')
    render(<FeedPage />)
    await new Promise(r => setTimeout(r, 20))
    expect(markSeen).not.toHaveBeenCalled()
  })
})
