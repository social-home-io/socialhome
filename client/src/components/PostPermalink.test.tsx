import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const route = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route, url: '/', path: '/', query: {} }),
  useRoute: () => ({ params: {}, query: {} }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import type { Comment, FeedPost } from '@/types'
import { PostPermalink, postApiPath } from './PostPermalink'

function post(id: string, extra: Partial<FeedPost> = {}): FeedPost {
  return {
    id, author: 'u-anna', type: 'text', content: `post ${id}`, media_url: null,
    image_urls: [], file_meta: null, reactions: {}, comment_count: 2, pinned: false,
    created_at: new Date().toISOString(), ...extra,
  } as FeedPost
}

function comment(id: string, extra: Partial<Comment> = {}): Comment {
  return {
    id, post_id: 'p1', parent_id: null, author: 'u-bob', type: 'text',
    content: `comment ${id}`, media_url: null, edited_at: null,
    created_at: new Date().toISOString(), ...extra,
  }
}

/** Answer reads by path (an Error is thrown). */
function serve(by: Record<string, unknown>) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const hit = by[path]
    if (hit instanceof Error) throw hit
    if (hit === undefined) throw Object.assign(new Error('nope'), { status: 404 })
    return hit
  })
}

const status = (n: number) => Object.assign(new Error(`HTTP ${n}`), { status: n })

beforeEach(() => {
  route.mockReset()
  vi.mocked(api.get).mockReset()
  vi.mocked(api.post).mockReset()
  currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false } as never
  Element.prototype.scrollIntoView = vi.fn()
})

describe('postApiPath', () => {
  it('household and space posts read from their own endpoints', () => {
    expect(postApiPath('p1', null)).toBe('/api/feed/posts/p1')
    expect(postApiPath('p1', 's1')).toBe('/api/spaces/s1/posts/p1')
  })
})

describe('PostPermalink', () => {
  it('shows the household post with its comments open', async () => {
    serve({
      '/api/feed/posts/p1': post('p1'),
      '/api/feed/posts/p1/comments': [comment('c1'), comment('c2')],
    })
    const { findByText, container } = render(
      <PostPermalink postId="p1" spaceId={null} backPath="/feed" />,
    )
    await findByText('post p1')
    await findByText('comment c2')
    expect(container.querySelector('#comment-c1')).not.toBeNull()
    expect(container.querySelector('.sh-comment-item--picked')).toBeNull()
  })

  it('a link to a comment picks it out and scrolls it into view', async () => {
    serve({
      '/api/feed/posts/p1': post('p1'),
      '/api/feed/posts/p1/comments': [comment('c1'), comment('c2', { parent_id: 'c1' })],
    })
    const { container } = render(
      <PostPermalink postId="p1" spaceId={null} commentId="c2" backPath="/feed" />,
    )
    await waitFor(() => expect(container.querySelector('#comment-c2')?.className)
      .toContain('sh-comment-item--picked'))
    expect(container.querySelector('#comment-c1')?.className).not.toContain('--picked')
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
    expect(document.activeElement?.id).toBe('comment-c2')
  })

  it('a space post reads through the space endpoints', async () => {
    serve({
      '/api/spaces/s1/posts/p1': post('p1'),
      '/api/spaces/s1/posts/p1/comments': [comment('c1')],
    })
    const { findByText } = render(
      <PostPermalink postId="p1" spaceId="s1" commentId="c1" backPath="/spaces/s1" />,
    )
    await findByText('comment c1')
    expect(api.get).toHaveBeenCalledWith('/api/spaces/s1/posts/p1')
  })

  it.each([404, 403])('a post that is gone (%i) says so, with a way back', async (code) => {
    serve({ '/api/feed/posts/p1': status(code) })
    const { findByText, getAllByRole } = render(
      <PostPermalink postId="p1" spaceId={null} backPath="/feed" />,
    )
    await findByText('This post is no longer available')
    const back = getAllByRole('link', { name: 'Back to the feed' })
    expect(back.length).toBeGreaterThan(0)
    fireEvent.click(back[back.length - 1])
    expect(route).toHaveBeenCalledWith('/feed')
  })

  it('the way back anchors on the base (ingress prefix)', async () => {
    serve({ '/api/feed/posts/p1': status(404) })
    const { findAllByRole } = render(
      <PostPermalink postId="p1" spaceId="s1" backPath="/spaces/s1" />,
    )
    const links = await findAllByRole('link', { name: 'Back to the feed' })
    for (const a of links) expect(a.getAttribute('href')).toMatch(/\/spaces\/s1$/)
  })

  it('a failed load offers a retry, not "gone"', async () => {
    serve({ '/api/feed/posts/p1': status(500) })
    const { findByText, getByRole, queryByText } = render(
      <PostPermalink postId="p1" spaceId={null} backPath="/feed" />,
    )
    await findByText("Couldn't load this post")
    expect(queryByText('This post is no longer available')).toBeNull()
    serve({ '/api/feed/posts/p1': post('p1'), '/api/feed/posts/p1/comments': [] })
    fireEvent.click(getByRole('button', { name: 'Try again' }))
    await findByText('post p1')
  })

  it('a comment that is gone says so above the thread', async () => {
    serve({
      '/api/feed/posts/p1': post('p1'),
      '/api/feed/posts/p1/comments': [comment('c1')],
    })
    const { findByText } = render(
      <PostPermalink postId="p1" spaceId={null} commentId="c-gone" backPath="/feed" />,
    )
    await findByText('That comment is no longer available.')
  })
})
