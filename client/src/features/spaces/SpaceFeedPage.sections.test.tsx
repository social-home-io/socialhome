/**
 * SpaceFeedPage sections: every section has its own path
 * (``/spaces/s1/tasks`` …, the feed at ``/spaces/s1``), the viewer's role
 * decides whether they may write there, a section the space or viewer
 * can't have falls back to the feed, and links from before sections had
 * paths (``?tab=``, ``?view=chat``, ``#join-requests``) move to the new
 * path. The bar is a ``<nav>`` of real links, never a fake tablist.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
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

const route = vi.hoisted(() => ({
  section: undefined as string | undefined,
  query: {} as Record<string, string>,
  go: vi.fn(),
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1', section: route.section }, query: route.query, path: '/spaces/s1' }),
  useLocation: () => ({ route: route.go, url: '/spaces/s1', path: '/spaces/s1', query: route.query }),
}))

vi.mock('./SpaceTimetableTab', () => ({
  SpaceTimetableTab: (p: { spaceId: string; canEdit: boolean }) => (
    <div data-testid="timetable-tab">{`${p.spaceId}:${p.canEdit}`}</div>
  ),
}))

vi.mock('./SpaceTasksTab', () => ({
  SpaceTasksTab: (p: { spaceId: string; writable: boolean; archived?: boolean }) => (
    <div data-testid="tasks-tab">{`${p.spaceId}:${p.writable}:${!!p.archived}`}</div>
  ),
}))

function wire(features: Record<string, boolean>, role: string, extra: Record<string, unknown> = {}) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features, my_role: role, ...extra }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role }, { user_id: 'u2', role: 'member' }]
    if (url === '/api/spaces/s1/links') return { links: [] }
    if (url === '/api/spaces/s1/chat') return { enabled: true, conversation_id: 'c1', unread: 0, notif_level: 'all', muted_until: null }
    return []
  })
}

async function open(section?: string) {
  route.section = section
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  route.go.mockReset()
  route.query = {}
  route.section = undefined
  window.location.hash = ''
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

describe('SpaceFeedPage /spaces/:id/:section', () => {
  it('/spaces/s1/tasks opens Tasks; a follower gets it read-only', async () => {
    wire({ todo: true }, 'subscriber')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:false:false'))
  }, 20000)

  it('until the space detail answers, the role is unknown (not read-only)', async () => {
    let answer!: (v: unknown) => void
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/spaces/s1') return new Promise((res) => { answer = res })
      if (url === '/api/spaces/s1/members') return Promise.resolve([{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }])
      if (url === '/api/spaces/s1/links') return Promise.resolve({ links: [] })
      return Promise.resolve([])
    })
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:undefined:false'))
    answer({ id: 's1', name: 'Trip', features: { todo: true }, my_role: 'member' })
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
  }, 20000)

  it('a member gets Tasks writable, its link current', async () => {
    wire({ todo: true }, 'member')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
    const tasks = r.getByRole('link', { name: 'Tasks' })
    expect(tasks.getAttribute('href')).toBe('/spaces/s1/tasks')
    expect(tasks.getAttribute('aria-current')).toBe('page')
    expect(r.getByRole('link', { name: 'Feed' }).getAttribute('aria-current')).toBeNull()
  }, 20000)

  it('the bar is a labelled nav of links, never a tablist', async () => {
    wire({ todo: true }, 'member')
    const { tl, r } = await open()
    const nav = await tl.waitFor(() => r.getByRole('navigation', { name: 'Space sections' }))
    // Chat joins once the role says "member".
    await tl.waitFor(() => expect(r.getByRole('link', { name: 'Chat' })).toBeTruthy())
    expect(r.queryAllByRole('tab')).toEqual([])
    expect(r.queryByRole('tablist')).toBeNull()
    const hrefs = Array.from(nav.querySelectorAll('a')).map((a) => a.getAttribute('href'))
    expect(hrefs.slice(0, 3)).toEqual(['/spaces/s1', '/spaces/s1/chat', '/spaces/s1/calendar'])
    // Members is a row of More, never in the bar (owner decision 1).
    expect(hrefs).not.toContain('/spaces/s1/members')
    const more = r.getByRole('button', { name: /^More/ })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(more.getAttribute('aria-controls')).toBeTruthy()
  }, 20000)

  it('a moderator writes tasks and gets Moderation in More', async () => {
    wire({ todo: true }, 'moderator')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    expect(r.getByRole('link', { name: /^Moderation/ }).getAttribute('href')).toBe('/spaces/s1/moderation')
  }, 20000)

  it('a follower on /map falls back to the feed: no Map, no proposals; Settings is theirs too', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/s1') {
        return { id: 's1', name: 'Trip', features: { location: true, todo: true }, my_role: 'subscriber' }
      }
      // A follower's roster lists the members only — never the viewer.
      if (url === '/api/spaces/s1/members') return [{ user_id: 'u9', role: 'owner' }]
      if (url === '/api/spaces/s1/links') return { links: [] }
      return []
    })
    const { tl, r } = await open('map')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1', true))
    expect(r.queryByRole('link', { name: 'Map' })).toBeNull()
    // A follower's settings: their notifications and Unfollow.
    expect(r.getByRole('link', { name: 'Space settings' }).getAttribute('href'))
      .toBe('/spaces/s1/settings')
    expect(r.getByRole('link', { name: 'Tasks' })).toBeTruthy()
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    expect(r.queryByRole('link', { name: /^Map/ })).toBeNull()
    expect(r.queryByRole('link', { name: /^Moderation/ })).toBeNull()
    expect(apiGet).not.toHaveBeenCalledWith('/api/spaces/s1/proposals')
    expect(apiGet).not.toHaveBeenCalledWith('/api/spaces/s1/presence')
  }, 20000)

  it('a plain member gets no Moderation row, but the gear', async () => {
    wire({ todo: true }, 'member')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(r.getByTestId('tasks-tab').textContent).toBe('s1:true:false'))
    expect(r.getByRole('link', { name: 'Space settings' }).getAttribute('href')).toBe('/spaces/s1/settings')
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    expect(r.queryByRole('link', { name: /^Moderation/ })).toBeNull()
  }, 20000)

  it('/moderation for a plain member falls back to the feed', async () => {
    wire({}, 'member')
    const { tl } = await open('moderation')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1', true))
  }, 20000)

  it('a section the space has turned off falls back to the feed', async () => {
    wire({ todo: false }, 'member')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1', true))
    expect(r.queryByRole('link', { name: 'Tasks' })).toBeNull()
  }, 20000)

  it('an unknown section goes to the feed', async () => {
    wire({}, 'member')
    const { tl } = await open('nope')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1', true))
  }, 20000)

  it('without a section the page opens on the feed', async () => {
    wire({ todo: true }, 'member')
    const { tl, r } = await open()
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByTestId('tasks-tab')).toBeNull()
    expect(r.getByRole('link', { name: 'Feed' }).getAttribute('aria-current')).toBe('page')
    expect(route.go).not.toHaveBeenCalled()
  }, 20000)

  it('empty feed and empty calendar headings sit right under the space h1', async () => {
    // An h3 straight under the space-name h1 skipped a level (axe
    // heading-order) on the feed and the calendar section.
    for (const section of [undefined, 'calendar']) {
      vi.resetModules()
      wire({ calendar: true }, 'member')
      const { tl, r } = await open(section)
      await tl.waitFor(() => expect(r.container.querySelector('.sh-empty-state h2')).toBeTruthy())
      expect(r.container.querySelector('.sh-empty-state h3')).toBeNull()
      r.unmount()
    }
  }, 20000)

  it('a failed calendar load shows an error with Retry, not "no events"', async () => {
    // A 500 from the events list used to fall back to the empty state —
    // "nothing this month" when the truth was "couldn't fetch".
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: { calendar: true }, my_role: 'member' }
      if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }]
      if (url === '/api/spaces/s1/links') return { links: [] }
      if (url === '/api/spaces/s1/calendar/events') {
        if (fail) throw Object.assign(new Error('boom'), { status: 500 })
        return [{
          id: 'e1', calendar_id: 's1', summary: 'Game night',
          // 10:00–11:00 today, local: "now + 1 h" crossed midnight late
          // in the evening and filed the event on two day cards.
          start: new Date(new Date().setHours(10, 0, 0, 0)).toISOString(),
          end: new Date(new Date().setHours(11, 0, 0, 0)).toISOString(),
          all_day: false, attendees: [], created_by: 'u1',
        }]
      }
      return []
    })
    const { tl, r } = await open('calendar')
    const alert = await tl.waitFor(() => r.getByRole('alert'))
    expect(alert.querySelector('h2')?.textContent).toBe("Couldn't load the calendar.")
    expect(r.container.textContent).not.toContain('No events')
    fail = false
    tl.fireEvent.click(tl.within(alert).getByRole('button', { name: 'Retry' }))
    await tl.waitFor(() => expect(r.getByText('Game night')).toBeTruthy())
    expect(r.queryByRole('alert')).toBeNull()
  }, 20000)
})

describe('the Timetable section', () => {
  it('/spaces/s1/timetable opens the space timetable (admins edit)', async () => {
    wire({ timetable: true }, 'admin')
    const { tl, r } = await open('timetable')
    await tl.waitFor(() => expect(r.getByTestId('timetable-tab').textContent).toBe('s1:true'))
    expect(route.go).not.toHaveBeenCalled()
    // Calendar holds only events now: no Events | Timetable switch.
    expect(r.queryByRole('group', { name: 'Calendar view' })).toBeNull()
  }, 20000)

  it('without the timetable feature it falls back to the feed', async () => {
    wire({ timetable: false }, 'member')
    const { tl } = await open('timetable')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1', true))
  }, 20000)

  it('an old Calendar link in a space with only a timetable opens the Timetable', async () => {
    wire({ calendar: false, timetable: true }, 'member')
    const { tl } = await open('calendar')
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1/timetable', true))
  }, 20000)

  it('keeps the HA ingress prefix on the Timetable link', async () => {
    const PREFIX = '/api/hassio_ingress/tok'
    vi.doMock('@/baseUrl', () => ({
      basePath: `${PREFIX}/`,
      addBase: (p: string) => PREFIX + (p.startsWith('/') ? p : `/${p}`),
      stripBase: (p: string) => (p.startsWith(PREFIX) ? p.slice(PREFIX.length) || '/' : p),
    }))
    try {
      wire({ timetable: true }, 'member')
      const { tl, r } = await open('timetable')
      await tl.waitFor(() => expect(r.getByTestId('timetable-tab')).toBeTruthy())
      const more = r.getByRole('button', { name: /^More/ })
      tl.fireEvent.click(more)
      const link = await tl.waitFor(() => r.getAllByRole('link', { name: /^Timetable/ })[0])
      expect(link.getAttribute('href')).toBe('/api/hassio_ingress/tok/spaces/s1/timetable')
    } finally {
      vi.doUnmock('@/baseUrl')
    }
  }, 20000)
})

describe('links from before sections had paths', () => {
  it.each([
    [{ tab: 'timetable' }, '', '/spaces/s1/timetable'],
    [{ tab: 'tasks' }, '', '/spaces/s1/tasks'],
    [{ tab: 'moderation' }, '', '/spaces/s1/moderation'],
    [{ view: 'chat' }, '', '/spaces/s1/chat'],
    [{}, '#join-requests', '/spaces/s1/moderation'],
  ])('%o %s → %s, replacing the history entry', async (query, hash, target) => {
    route.query = query as Record<string, string>
    window.location.hash = hash
    wire({ todo: true, timetable: true }, 'admin')
    const { tl } = await open()
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith(target, true))
  }, 20000)
})

describe('the Members row and the member count', () => {
  it('More opens with Members first: "N people · N admins"', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: {}, my_role: 'member' }
      if (url === '/api/spaces/s1/members') {
        return [
          { user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'owner' },
          { user_id: 'u3', role: 'admin' }, { user_id: 'u4', role: 'member' },
        ]
      }
      if (url === '/api/spaces/s1/links') return { links: [] }
      return []
    })
    const { tl, r } = await open()
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    const members = await tl.waitFor(() => r.getByRole('link', { name: /^Members/ }))
    expect(members.textContent).toContain('4 people · 2 admins')
    expect(members.getAttribute('href')).toBe('/spaces/s1/members')
    const rows = Array.from(r.container.querySelectorAll('.sh-space-more__list > li'))
    expect(rows[0].textContent).toContain('Members')
  }, 20000)

  it('the hero member count links to the Members section', async () => {
    wire({}, 'member', { about_markdown: 'Hello', cover_url: null, icon_url: null })
    const { tl, r } = await open()
    const link = await tl.waitFor(() => r.getByRole('link', { name: '2 members' }))
    expect(link.getAttribute('href')).toBe('/spaces/s1/members')
  }, 20000)
})
