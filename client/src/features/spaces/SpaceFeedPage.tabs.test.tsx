/**
 * SpaceFeedPage tab deep links: ``/spaces/{id}?tab=tasks`` (what task
 * notifications link to) opens the Tasks tab, with the viewer's role
 * deciding whether they may write; a tab the space doesn't have falls
 * back to the feed.
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

const route = vi.hoisted(() => ({ query: {} as Record<string, string> }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1' }, query: route.query, path: '/spaces/s1' }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: route.query }),
}))

vi.mock('./SpaceTasksTab', () => ({
  SpaceTasksTab: (p: { spaceId: string; writable: boolean; archived?: boolean }) => (
    <div data-testid="tasks-tab">{`${p.spaceId}:${p.writable}:${!!p.archived}`}</div>
  ),
}))

function wire(features: Record<string, boolean>, role: string) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role }]
    if (url === '/api/spaces/s1/links') return { links: [] }
    return []
  })
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
})

describe('SpaceFeedPage ?tab=', () => {
  it('?tab=tasks opens the Tasks tab; a subscriber gets it read-only', async () => {
    route.query = { tab: 'tasks' }
    wire({ todo: true }, 'subscriber')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:false:false'))
  }, 20000)

  it('until the member list answers, the role is unknown (not read-only)', async () => {
    route.query = { tab: 'tasks' }
    let answer!: (v: unknown) => void
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/spaces/s1') return Promise.resolve({ id: 's1', name: 'Trip', features: { todo: true } })
      if (url === '/api/spaces/s1/members') return new Promise((res) => { answer = res })
      if (url === '/api/spaces/s1/links') return Promise.resolve({ links: [] })
      return Promise.resolve([])
    })
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:undefined:false'))
    answer([{ user_id: 'u1', role: 'member' }])
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
  }, 20000)

  it('a member gets the Tasks tab writable', async () => {
    route.query = { tab: 'tasks' }
    wire({ todo: true }, 'member')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
  }, 20000)

  it('a tab the space has turned off falls back to the feed', async () => {
    route.query = { tab: 'tasks' }
    wire({ todo: false }, 'member')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1'))
    await new Promise(res => setTimeout(res, 50))
    await tl.waitFor(() => expect(r.queryByTestId('tasks-tab')).toBeNull())
  }, 20000)

  it('without ?tab= the page opens on the feed', async () => {
    route.query = {}
    wire({ todo: true }, 'member')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByTestId('tasks-tab')).toBeNull()
  }, 20000)
})
