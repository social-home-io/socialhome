/**
 * SpaceFeedPage — "Not sent yet" moves to the posts: the viewer's OWN
 * posts that no other household has yet (``sync.unsent_posts``) carry the
 * mark in their meta line, live with the ``space.sync_state`` frame;
 * nobody else's post ever does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn(async () => ({})),
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
  PostCard: (p: { post: { id: string }; metaEnd?: unknown }) => (
    <div data-testid={`post-${p.post.id}`} class="probe-post">{p.metaEnd as never}</div>
  ),
}))

const post = (id: string, author: string) => ({
  id, author, type: 'text', content: `post ${id}`, created_at: '2026-11-21T10:00:00+00:00',
  reactions: {}, comment_count: 0, deleted: false, hidden_from_feed: false, bot_id: null,
})

const SYNC = {
  state: 'host_offline', last_ok_at: null, waiting_to_send: 2, may_be_missing: false,
  unsent_posts: ['mine', 'theirs'],
}

beforeEach(() => {
  vi.resetModules()
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  apiGet.mockReset()
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') {
      return { id: 's1', name: 'Family', features: {}, my_role: 'member', owner_instance_id: 'host-x', sync: SYNC }
    }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }]
    if (url === '/api/spaces/s1/feed') return [post('mine', 'u1'), post('theirs', 'u2'), post('sent', 'u1')]
    if (url === '/api/connections') return [{ instance_id: 'host-x', display_name: 'The Müllers' }]
    if (url.endsWith('/links')) return { links: [] }
    return []
  })
})

async function renderFeed() {
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  const r = tl.render(<Page />)
  await tl.waitFor(() => expect(r.queryByTestId('post-mine')).not.toBeNull())
  return { tl, r }
}

const mark = (r: { getByTestId: (id: string) => HTMLElement }, id: string) =>
  r.getByTestId(`post-${id}`).querySelector('[data-testid="post-not-sent"]')

describe('"Not sent yet" on the viewer\'s own posts', () => {
  it('marks only my own unsent post, naming the host household', async () => {
    const { tl, r } = await renderFeed()
    await tl.waitFor(() => expect(mark(r, 'mine')).not.toBeNull())
    expect(mark(r, 'theirs')).toBeNull()  // someone else's
    expect(mark(r, 'sent')).toBeNull()    // reached a household
    tl.fireEvent.click(mark(r, 'mine')!)
    await tl.waitFor(() => expect(mark(r, 'mine')!.textContent)
      .toBe('○Not sent yet — goes out when The Müllers is back'))
  })

  it('the mark goes once the post went out (live frame)', async () => {
    const { tl, r } = await renderFeed()
    await tl.waitFor(() => expect(mark(r, 'mine')).not.toBeNull())
    await tl.act(async () => {
      emit('space.sync_state', { space_id: 's1', sync: { ...SYNC, state: 'up_to_date', unsent_posts: [] } })
    })
    expect(mark(r, 'mine')).toBeNull()
  })
})
