/**
 * The space feed offers Delete on a post to its author and to content
 * authority (owner / admin / moderator) — never to a plain member on
 * somebody else's post. The Moderation tab is for the host's queue only.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
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

vi.mock('@/store/instance', async (orig) => ({
  ...(await orig<object>()),
  instanceConfig: { value: { instance_id: 'iid-us' } },
}))

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1' }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

vi.mock('@/components/PostCard', () => ({
  PostCard: (p: { post: { id: string }; onDelete?: () => void }) => (
    <div data-testid={`post-${p.post.id}`}>{p.onDelete ? 'deletable' : 'locked'}</div>
  ),
}))

function wire(role: string, host = 'iid-us') {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: {}, owner_instance_id: host }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role }]
    if (url === '/api/spaces/s1/feed') {
      return [
        { id: 'mine', author: 'u1', type: 'text', content: 'a', created_at: '2026-01-01', reactions: {} },
        { id: 'theirs', author: 'u2', type: 'text', content: 'b', created_at: '2026-01-01', reactions: {} },
      ]
    }
    if (url === '/api/spaces/s1/links') return { links: [] }
    return []
  })
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
})

async function renderPage() {
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

describe('SpaceFeedPage post menu', () => {
  it.each(['moderator', 'admin', 'owner'])('a %s may delete somebody else\'s post', async (role) => {
    wire(role)
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-theirs').textContent).toBe('deletable'))
    expect(r.getByTestId('post-mine').textContent).toBe('deletable')
  }, 20000)

  it('a member deletes only their own post', async () => {
    wire('member')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-mine').textContent).toBe('deletable'))
    expect(r.getByTestId('post-theirs').textContent).toBe('locked')
  }, 20000)

  it('a moderator on a member stub gets the Moderation tab (v_43)', async () => {
    // Items reach every household holding a moderator seat, so the queue
    // is worked from the stub too.
    wire('moderator', 'iid-elsewhere')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-theirs').textContent).toBe('deletable'))
    expect(r.getByRole('tab', { name: 'Moderation' })).toBeTruthy()
  }, 20000)

  it('a plain member on a stub has no Moderation tab', async () => {
    wire('member', 'iid-elsewhere')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-mine').textContent).toBe('deletable'))
    expect(r.queryByRole('tab', { name: 'Moderation' })).toBeNull()
  }, 20000)
})
