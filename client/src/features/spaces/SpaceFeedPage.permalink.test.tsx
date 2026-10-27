/**
 * ``/spaces/{id}?post={pid}[&comment={cid}]`` — the link a space post,
 * comment, mention or @here notification opens: the Feed shows that post
 * alone with its comments (the comment picked out), a way back to the
 * whole feed, and no hero / feed list around it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
const route = vi.hoisted(() => ({ query: {} as Record<string, string>, go: vi.fn() }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1', section: undefined }, query: route.query, path: '/spaces/s1' }),
  useLocation: () => ({ route: route.go, url: '/spaces/s1', path: '/spaces/s1', query: route.query }),
}))

function post(id: string, content: string) {
  return {
    id, author: 'u2', type: 'text', content, media_url: null, image_urls: [], file_meta: null,
    reactions: {}, comment_count: 1, pinned: false, created_at: new Date().toISOString(),
  }
}

function wire() {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') {
      return { id: 's1', name: 'Garden', space_type: 'household', features: {}, my_role: 'member' }
    }
    if (url === '/api/spaces/s1/feed') return [post('p-new', 'newest post'), post('p1', 'the post')]
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }]
    if (url === '/api/spaces/s1/posts/p1') return post('p1', 'the post')
    if (url === '/api/spaces/s1/posts/p1/comments') {
      return [{ id: 'c1', post_id: 'p1', parent_id: null, author: 'u2', type: 'text',
                content: 'the comment', media_url: null, edited_at: null, created_at: new Date().toISOString() }]
    }
    if (url === '/api/spaces/s1/posts/p-gone') throw Object.assign(new Error('gone'), { status: 404 })
    return []
  })
}

async function open() {
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  route.go.mockReset()
  route.query = {}
  Element.prototype.scrollIntoView = vi.fn()
  wire()
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

describe('a link to a space post', () => {
  it('shows that post alone with the comment picked out', async () => {
    route.query = { post: 'p1', comment: 'c1' }
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.container.querySelector('#comment-c1')?.className)
      .toContain('sh-comment-item--picked'))
    expect(r.container.textContent).toContain('the post')
    // Not the feed list, nor the hero, around it.
    expect(r.container.textContent).not.toContain('newest post')
    expect(r.container.querySelector('.sh-space-hero')).toBeNull()
    await tl.waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled())
  })

  it('"Back to the feed" goes to the space feed', async () => {
    route.query = { post: 'p1' }
    const { tl, r } = await open()
    const back = await tl.waitFor(() => {
      const a = r.container.querySelector<HTMLAnchorElement>('.sh-post-permalink__back')
      if (!a) throw new Error('no back link yet')
      return a
    })
    expect(back.getAttribute('href')).toMatch(/\/spaces\/s1$/)
    tl.fireEvent.click(back)
    expect(route.go).toHaveBeenCalledWith('/spaces/s1')
  })

  it('a deleted post says so instead of a blank page', async () => {
    route.query = { post: 'p-gone' }
    const { tl } = await open()
    await tl.screen.findByText('This post is no longer available')
  })

  it('without ?post= the feed list shows as before', async () => {
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.container.textContent).toContain('newest post'))
    expect(r.container.querySelector('.sh-post-permalink')).toBeNull()
  })
})
