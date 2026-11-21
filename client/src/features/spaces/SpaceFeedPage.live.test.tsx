/**
 * SpaceFeedPage — new posts land from the ``space.post.created`` frame,
 * not a whole-feed refetch per post. Only a post the frame can't render
 * (a bot's, a video) refetches, at most once per 5 s.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn(async (..._a: unknown[]) => ({}))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const wsHandlers: Record<string, Handler[]> = {}
vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  return {
    connectionState: signal('open'),
    ws: {
      on: (type: string, h: Handler) => {
        ;(wsHandlers[type] ??= []).push(h)
        return () => { wsHandlers[type] = (wsHandlers[type] ?? []).filter(x => x !== h) }
      },
      send: vi.fn(),
    },
  }
})
function emit(type: string, data: Record<string, unknown>): void {
  for (const h of wsHandlers[type] ?? []) h({ type, data })
}

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1', section: undefined }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

vi.mock('@/components/PostCard', () => ({
  PostCard: (p: { post: { id: string } }) => <div data-testid={`post-${p.post.id}`} class="probe-post" />,
}))

const post = (id: string, extra: Record<string, unknown> = {}) => ({
  id, author: 'u2', type: 'text', content: `post ${id}`, created_at: '2026-11-21T10:00:00+00:00',
  reactions: {}, comment_count: 0, deleted: false, hidden_from_feed: false, bot_id: null, ...extra,
})

beforeEach(() => {
  vi.resetModules()
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  apiGet.mockReset()
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: {}, my_role: 'member' }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }]
    if (url === '/api/spaces/s1/feed') return [post('first')]
    if (url.endsWith('/links')) return { links: [] }
    return []
  })
})
afterEach(() => {
  vi.useRealTimers()
})

const feedGets = () => apiGet.mock.calls.filter(c => c[0] === '/api/spaces/s1/feed').length

async function renderFeed() {
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  const r = tl.render(<Page />)
  await tl.waitFor(() => expect(r.queryByTestId('post-first')).not.toBeNull())
  return { tl, r }
}

describe('new space posts land from the frame', () => {
  it('19 posts: each on top, no feed refetch, a replayed frame shows once', async () => {
    const { tl, r } = await renderFeed()
    const before = feedGets()
    await tl.act(async () => {
      for (let i = 0; i < 19; i++) emit('space.post.created', { space_id: 's1', post: post(`p${i}`) })
      emit('space.post.created', { space_id: 's1', post: post('p18') })
    })
    const ids = [...r.container.querySelectorAll('.probe-post')].map(e => e.getAttribute('data-testid'))
    expect(ids.length).toBe(20)
    expect(ids[0]).toBe('post-p18')
    expect(ids[19]).toBe('post-first')
    expect(feedGets()).toBe(before)
  })

  it('another space\'s post and a hidden anchor change nothing here', async () => {
    const { tl, r } = await renderFeed()
    const before = feedGets()
    await tl.act(async () => {
      emit('space.post.created', { space_id: 's2', post: post('elsewhere') })
      emit('space.post.created', { space_id: 's1', post: post('anchor', { hidden_from_feed: true }) })
    })
    expect(r.queryByTestId('post-elsewhere')).toBeNull()
    expect(r.queryByTestId('post-anchor')).toBeNull()
    expect(feedGets()).toBe(before)
  })

  it('marks the feed read once on open, then at most once per 5 s as posts arrive', async () => {
    apiPost.mockClear()
    const { tl } = await renderFeed()
    const reads = () => apiPost.mock.calls.filter(c => c[0] === '/api/spaces/s1/feed/read').length
    await tl.waitFor(() => expect(reads()).toBe(1))
    vi.useFakeTimers()
    // Let any effect still queued from the load settle: still one.
    await tl.act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(reads()).toBe(1)
    for (let i = 0; i < 19; i++) {
      await tl.act(async () => {
        emit('space.post.created', { space_id: 's1', post: post(`r${i}`) })
        await vi.advanceTimersByTimeAsync(200)
      })
    }
    // 19 posts over ~4 s: inside the gap after the open's read → one
    // trailing read at the end of it.
    expect(reads()).toBe(1)
    await tl.act(async () => { await vi.advanceTimersByTimeAsync(1_500) })
    expect(reads()).toBe(2)
    await tl.act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(reads()).toBe(2)
  })

  it('bot posts refetch the feed at most once per 5 s, with one trailing refetch', async () => {
    const { tl } = await renderFeed()
    vi.useFakeTimers()
    const before = feedGets()
    await tl.act(async () => {
      for (let i = 0; i < 6; i++) {
        emit('space.post.created', { space_id: 's1', post: post(`b${i}`, { bot_id: 'bot-1', author: 'system-integration' }) })
      }
    })
    // The page's own load just happened: the burst waits for the gap…
    expect(feedGets()).toBe(before)
    await tl.act(async () => { await vi.advanceTimersByTimeAsync(5_100) })
    // …and lands as one refetch.
    expect(feedGets()).toBe(before + 1)
    await tl.act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(feedGets()).toBe(before + 1)
  })
})
