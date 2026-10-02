/**
 * ADMIN_ONLY features (§4.3) in a member's view of a space: the composer,
 * pages tab, tasks board, sticky board and calendar drop their create /
 * edit controls for a short "only admins …" note; the owner / admins keep
 * them. The server refuses the writes anyway (403 ACCESS_ADMIN_ONLY).
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
  SpaceTasksTab: (p: { writable: boolean | undefined; adminOnly?: boolean }) => (
    <div data-testid="tasks-tab">{`${p.writable}:${!!p.adminOnly}`}</div>
  ),
}))
vi.mock('@/features/stickies/StickyBoardPage', () => ({
  default: (p: { readOnly?: string | null }) => (
    <div data-testid="sticky-board">{p.readOnly ?? 'writable'}</div>
  ),
}))
vi.mock('./SpaceBazaarTab', () => ({
  SpaceBazaarTab: (p: { canSell?: boolean }) => (
    <div data-testid="bazaar-tab">{String(p.canSell ?? true)}</div>
  ),
}))
vi.mock('@/components/ModerationQueue', () => ({
  // ``canApprove`` is per feature: report it for posts.
  ModerationQueue: (p: { canApprove?: boolean | ((feature: string) => boolean) }) => (
    <div data-testid="moderation">
      {String(typeof p.canApprove === 'function' ? p.canApprove('posts') : (p.canApprove ?? true))}
    </div>
  ),
}))
vi.mock('@/components/Composer', () => ({
  Composer: () => <div data-testid="composer" />,
}))

const ADMIN_ONLY_ALL = {
  todo: true,
  bazaar: true,
  pages: true,
  stickies: true,
  calendar: true,
  posts_access: 'admin_only',
  pages_access: 'admin_only',
  tasks_access: 'admin_only',
  stickies_access: 'admin_only',
  calendar_access: 'admin_only',
}

function wire(
  role: string,
  features: Record<string, unknown> = ADMIN_ONLY_ALL,
  extra: Record<string, unknown> = {},
) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features, ...extra }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role }]
    if (url === '/api/spaces/s1/links') return { links: [] }
    if (url === '/api/spaces/s1/compat') {
      return { ours: 42, min_member_proto_version: null, lagging_features: [], behind_members: [] }
    }
    return []
  })
}

async function open(
  tab: string,
  role: string,
  features?: Record<string, unknown>,
  extra?: Record<string, unknown>,
) {
  route.query = tab === 'feed' ? {} : { tab }
  wire(role, features, extra)
  const tl = await import('@testing-library/preact')
  tl.cleanup() // a test may open several tabs, one page at a time
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
})

describe('a member in an admin-only space', () => {
  it.each(['member', 'moderator'])('%s: the feed shows a note instead of the composer', async (role) => {
    const { tl, r } = await open('feed', role)
    await tl.waitFor(() => expect(r.getByText('Only admins can post here.')).toBeTruthy())
    expect(r.queryByTestId('composer')).toBeNull()
  }, 20000)

  it('the tasks board is read-only with the admin-only reason', async () => {
    const { tl, r } = await open('tasks', 'member')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('false:true'))
  }, 20000)

  it('the sticky board is read-only with the admin-only note', async () => {
    const { tl, r } = await open('stickies', 'moderator')
    await tl.waitFor(() =>
      expect(r.getByTestId('sticky-board').textContent)
        .toBe('Only admins can add or move sticky notes here.'))
  }, 20000)

  it('the pages tab says only admins add pages', async () => {
    const { tl, r } = await open('pages', 'member')
    await tl.waitFor(() =>
      expect(r.getByText('Only admins can add or edit pages here.')).toBeTruthy())
  }, 20000)

  it('the calendar hides + New event behind the admin-only note', async () => {
    const { tl, r } = await open('calendar', 'member')
    await tl.waitFor(() =>
      expect(r.getByText('Only admins can add or change events here.')).toBeTruthy())
    expect(r.queryByRole('button', { name: '+ New event' })).toBeNull()
  }, 20000)
})

describe('the leftovers of an admin-only posts feed', () => {
  it('a member gets no Bazaar "New listing"', async () => {
    const { tl, r } = await open('bazaar', 'member')
    await tl.waitFor(() => expect(r.getByTestId('bazaar-tab').textContent).toBe('false'))
  }, 20000)

  async function moderationTab(role: string) {
    const { tl, r } = await open('feed', role)
    const tab = await tl.waitFor(() => r.getByRole('tab', { name: /Moderation/ }))
    tl.fireEvent.click(tab)
    return { tl, r }
  }

  it('a moderator may reject but not approve in the moderation queue', async () => {
    const { tl, r } = await moderationTab('moderator')
    await tl.waitFor(() => expect(r.getByTestId('moderation').textContent).toBe('false'))
  }, 20000)

  it('an admin keeps both', async () => {
    let { tl, r } = await open('bazaar', 'admin')
    await tl.waitFor(() => expect(r.getByTestId('bazaar-tab').textContent).toBe('true'))
    ;({ tl, r } = await moderationTab('admin'))
    await tl.waitFor(() => expect(r.getByTestId('moderation').textContent).toBe('true'))
  }, 30000)
})

describe('an admin in an admin-only space', () => {
  it('keeps the composer, a writable board and + New event', async () => {
    let { tl, r } = await open('feed', 'admin')
    await tl.waitFor(() => expect(r.getByTestId('composer')).toBeTruthy())
    expect(r.queryByText('Only admins can post here.')).toBeNull()
    ;({ tl, r } = await open('tasks', 'owner'))
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('true:true'))
    ;({ tl, r } = await open('stickies', 'admin'))
    await tl.waitFor(() => expect(r.getByTestId('sticky-board').textContent).toBe('writable'))
    ;({ tl, r } = await open('calendar', 'admin'))
    await tl.waitFor(() => expect(r.getByRole('button', { name: '+ New event' })).toBeTruthy())
  }, 30000)
})

describe('an open space', () => {
  it('a member keeps every control', async () => {
    const open_ = { todo: true, pages: true, stickies: true, calendar: true }
    let { tl, r } = await open('feed', 'member', open_)
    await tl.waitFor(() => expect(r.getByTestId('composer')).toBeTruthy())
    ;({ tl, r } = await open('stickies', 'member', open_))
    await tl.waitFor(() => expect(r.getByTestId('sticky-board').textContent).toBe('writable'))
    ;({ tl, r } = await open('tasks', 'member', open_))
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('true:false'))
  }, 30000)
})

describe('a sticky board the viewer can only read', () => {
  const open_ = { todo: true, pages: true, stickies: true, calendar: true }

  it('a follower sees it read-only (so notes carry their own Report)', async () => {
    const { tl, r } = await open('stickies', 'subscriber', open_)
    await tl.waitFor(() =>
      expect(r.getByTestId('sticky-board').textContent)
        .toBe('You can read these notes but not add or change them.'))
  }, 20000)

  it('an archived space is read-only even for its owner', async () => {
    const { tl, r } = await open('stickies', 'owner', open_, { archived: true })
    await tl.waitFor(() =>
      expect(r.getByTestId('sticky-board').textContent)
        .toBe('This space is archived — its sticky notes are read-only.'))
  }, 20000)
})
