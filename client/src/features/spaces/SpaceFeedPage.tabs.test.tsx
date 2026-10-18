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

  it('a moderator writes tasks and gets the Moderation tab', async () => {
    route.query = { tab: 'tasks' }
    wire({ todo: true }, 'moderator')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
    await tl.waitFor(() => expect(r.getByRole('tab', { name: 'Moderation' })).toBeTruthy())
  }, 20000)

  it('a plain member gets no Moderation tab', async () => {
    route.query = { tab: 'tasks' }
    wire({ todo: true }, 'member')
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
    expect(r.queryByRole('tab', { name: 'Moderation' })).toBeNull()
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

  it('empty feed and empty calendar headings sit right under the space h1', async () => {
    // An h3 straight under the space-name h1 skipped a level (axe
    // heading-order) on the feed and the calendar tab.
    for (const tab of [undefined, 'calendar']) {
      vi.resetModules()
      route.query = tab ? { tab } : {}
      wire({ calendar: true }, 'member')
      const tl = await import('@testing-library/preact')
      const { default: Page } = await import('./SpaceFeedPage')
      const r = tl.render(<Page />)
      await tl.waitFor(() => expect(r.container.querySelector('.sh-empty-state h2')).toBeTruthy())
      expect(r.container.querySelector('.sh-empty-state h3')).toBeNull()
      r.unmount()
    }
  }, 20000)

  it('a failed calendar load shows an error with Retry, not "no events"', async () => {
    // A 500 from the events list used to fall back to the empty state —
    // "nothing this month" when the truth was "couldn't fetch".
    route.query = { tab: 'calendar' }
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: { calendar: true } }
      if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }]
      if (url === '/api/spaces/s1/links') return { links: [] }
      if (url === '/api/spaces/s1/calendar/events') {
        if (fail) throw Object.assign(new Error('boom'), { status: 500 })
        return [{
          id: 'e1', calendar_id: 's1', summary: 'Game night',
          start: new Date().toISOString(), end: new Date(Date.now() + 3600e3).toISOString(),
          all_day: false, attendees: [], created_by: 'u1',
        }]
      }
      return []
    })
    const tl = await import('@testing-library/preact')
    const { default: Page } = await import('./SpaceFeedPage')
    const r = tl.render(<Page />)
    const alert = await tl.waitFor(() => r.getByRole('alert'))
    expect(alert.querySelector('h2')?.textContent).toBe("Couldn't load the calendar.")
    expect(r.container.textContent).not.toContain('No events')
    fail = false
    tl.fireEvent.click(tl.within(alert).getByRole('button', { name: 'Retry' }))
    await tl.waitFor(() => expect(r.getByText('Game night')).toBeTruthy())
    expect(r.queryByRole('alert')).toBeNull()
  }, 20000)
})
