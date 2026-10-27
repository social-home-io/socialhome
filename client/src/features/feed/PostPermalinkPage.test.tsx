/** ``/post/{id}[?comment={cid}]`` — a household post's permalink page. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const where = vi.hoisted(() => ({ query: {} as Record<string, string>, route: vi.fn() }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 'p1' }, query: where.query }),
  useLocation: () => ({ route: where.route, url: '/post/p1', path: '/post/p1', query: where.query }),
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { pageTitle } from '@/store/pageTitle'
import PostPermalinkPage from './PostPermalinkPage'

beforeEach(() => {
  where.query = {}
  currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false } as never
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.get).mockReset().mockImplementation(async (path: string) => {
    if (path === '/api/feed/posts/p1') {
      return { id: 'p1', author: 'u-anna', type: 'text', content: 'household post', media_url: null,
               image_urls: [], file_meta: null, reactions: {}, comment_count: 1, pinned: false,
               created_at: new Date().toISOString() }
    }
    if (path === '/api/feed/posts/p1/comments') {
      return [{ id: 'c9', post_id: 'p1', parent_id: null, author: 'u-bob', type: 'text',
                content: 'a comment', media_url: null, edited_at: null, created_at: new Date().toISOString() }]
    }
    return []
  })
})

describe('PostPermalinkPage', () => {
  it('reads the household post of the route and titles the page', async () => {
    const { findByText } = render(<PostPermalinkPage />)
    await findByText('household post')
    expect(api.get).toHaveBeenCalledWith('/api/feed/posts/p1')
    expect(pageTitle.value).toBe('Post')
  })

  it('?comment= picks the comment out; the way back is the feed', async () => {
    where.query = { comment: 'c9' }
    const { container } = render(<PostPermalinkPage />)
    await waitFor(() => expect(container.querySelector('#comment-c9')?.className)
      .toContain('sh-comment-item--picked'))
    expect(container.querySelector('.sh-post-permalink__back')?.getAttribute('href')).toMatch(/\/feed$/)
  })
})
