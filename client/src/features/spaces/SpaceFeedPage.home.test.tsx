/**
 * The space home (redesign PR 9): the hero and "Needs you" open the Feed
 * only; the hero carries the facepile, the tier and the quick links; a
 * failed load is an error with Retry, not an empty feed; a member leaves
 * from More.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const { apiGet, apiDelete, confirm } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiDelete: vi.fn(),
  confirm: vi.fn(),
}))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirm(...a) }))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const route = vi.hoisted(() => ({ section: undefined as string | undefined, go: vi.fn() }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1', section: route.section }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: route.go, url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

vi.mock('./SpaceTasksTab', () => ({ SpaceTasksTab: () => <div data-testid="tasks-tab" /> }))

const MEMBERS = [
  { user_id: 'u1', role: 'owner', display_name: 'Me', picture_url: null },
  { user_id: 'u2', role: 'member', display_name: 'Lina', picture_url: '/p/2.webp' },
  { user_id: 'u3', role: 'member', display_name: 'Noah', picture_url: null },
  { user_id: 'u4', role: 'subscriber', display_name: 'Fern', picture_url: null },
]

function wire(role: string, extra: Record<string, unknown> = {}, feed: () => Promise<unknown> = async () => []) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') {
      return { id: 's1', name: 'Neighbourhood Garden', emoji: '🥕', space_type: 'household',
               features: { todo: true }, my_role: role, about_markdown: 'Shared beds.', ...extra }
    }
    if (url === '/api/spaces/s1/feed') return feed()
    if (url === '/api/spaces/s1/members') return MEMBERS.map((m) => (m.user_id === 'u1' ? { ...m, role } : m))
    if (url === '/api/spaces/s1/links') {
      return { links: [{ id: 'l1', label: 'Watering rota', url: 'https://rota.example/', position: 0 }] }
    }
    if (url === '/api/spaces/s1/chat') return { enabled: true, conversation_id: 'c1', unread: 0, notif_level: 'all', muted_until: null }
    return []
  })
}

/** The element, or throw — so ``waitFor`` keeps waiting for it. */
function present<T extends Element>(root: Element, sel: string): T {
  const el = root.querySelector(sel)
  if (!el) throw new Error(`no ${sel} yet`)
  return el as T
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
  apiDelete.mockReset()
  confirm.mockReset()
  route.go.mockReset()
  route.section = undefined
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

describe('the hero', () => {
  it("shows the server's member count — the one the Spaces list shows", async () => {
    // The roster preview shows four faces at most; the count is the
    // server's (remote members included, followers never).
    wire('member', { member_count: 12 })
    const { tl, r } = await open()
    const hero = await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    await tl.waitFor(() => expect(hero.querySelector('a.sh-space-hero-members')?.textContent).toContain('12 members'))
  })

  it('opens the feed: name, facepile link with the member count (no followers), tier, quick links', async () => {
    wire('member')
    const { tl, r } = await open()
    const hero = await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    expect(hero.querySelector('h2')?.textContent).toBe('Neighbourhood Garden')
    await tl.waitFor(() => expect(hero.querySelector('a.sh-space-hero-members')).toBeTruthy())
    const members = hero.querySelector('a.sh-space-hero-members') as HTMLAnchorElement
    expect(members.getAttribute('href')).toBe('/spaces/s1/members')
    expect(members.textContent).toContain('3 members')
    expect(hero.querySelectorAll('.sh-space-hero-faces > *')).toHaveLength(3)
    expect(hero.textContent).toContain('Household')
    await tl.waitFor(() => expect(hero.querySelector('.sh-space-hero-link')?.textContent).toBe('Watering rota'))
    // The quick links live in the hero only — no strip above it.
    expect(r.container.querySelectorAll('a[href="https://rota.example/"]')).toHaveLength(1)
  }, 20000)

  it('no hero on another section', async () => {
    wire('member')
    const { tl, r } = await open('tasks')
    await tl.waitFor(() => r.getByTestId('tasks-tab'))
    expect(r.container.querySelector('.sh-space-hero')).toBeNull()
    expect(r.container.querySelector('a[href="https://rota.example/"]')).toBeNull()
  }, 20000)
})

describe('Needs you', () => {
  const attention = {
    pending_review: 2, reports: 0, join_requests: 1,
    proposals: [{ id: 'p1', action: 'set_public_tier', params: { space_type: 'public' }, status: 'pending',
                  approvals: 1, needed: 2, required: 2, total_admins: 2, proposed_by_user: 'u1',
                  voted_by_me: true }],
  }

  it('an admin sees one card below the hero with a row per kind', async () => {
    wire('admin', { attention })
    const { tl, r } = await open()
    const card = await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-needs-you'))
    expect(card.previousElementSibling?.classList.contains('sh-space-hero')).toBe(true)
    expect(card.textContent).toContain('Needs you')
    expect(card.textContent).toContain('1 of 2 approvals · You voted')
    expect(card.textContent).toContain('2 waiting for review')
    expect(card.textContent).toContain('1 join request')
    // The old proposals banner is gone.
    expect(r.container.querySelector('.sh-space-proposals')).toBeNull()
  }, 20000)

  it('a member has no card', async () => {
    wire('member', { attention: null })
    const { tl, r } = await open()
    await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    expect(r.container.querySelector('.sh-needs-you')).toBeNull()
  }, 20000)

  it('Members shows an admin "N join requests · Review"', async () => {
    wire('admin', { attention })
    const { tl, r } = await open('members')
    const row = await tl.waitFor(() => present<HTMLAnchorElement>(r.container, '.sh-member-join-requests'))
    expect(row.getAttribute('href')).toBe('/spaces/s1/moderation')
  }, 20000)
})

describe('a failed load', () => {
  it('the feed failing shows an error with Retry, never "no posts yet"', async () => {
    let fail = true
    wire('member', {}, async () => {
      if (fail) throw Object.assign(new Error('boom'), { status: 500 })
      return []
    })
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.container.textContent).toContain("Couldn't load the posts."))
    expect(r.container.querySelector('.sh-empty-state:not(.sh-load-error)')).toBeNull()
    fail = false
    tl.fireEvent.click(r.getByRole('button', { name: 'Retry' }))
    await tl.waitFor(() => expect(r.container.textContent).not.toContain("Couldn't load the posts."))
  }, 20000)

  it('the space itself failing shows an error with Retry', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/s1') throw new Error('offline')
      return []
    })
    const { tl, r } = await open()
    await tl.waitFor(() => expect(r.container.textContent).toContain("Couldn't load this space."))
    expect(r.getByRole('button', { name: 'Retry' })).toBeTruthy()
  }, 20000)

  it('no seat (403) still goes to the landing page', async () => {
    wire('member', {}, async () => { throw Object.assign(new Error('no'), { status: 403 }) })
    const { tl } = await open()
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces/s1/about', true))
  }, 20000)
})

describe('Leave space', () => {
  it('a member confirms, the seat is dropped and the spaces list opens', async () => {
    wire('member')
    confirm.mockResolvedValue(true)
    apiDelete.mockResolvedValue({ ok: true })
    const { tl, r } = await open()
    await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    tl.fireEvent.click(r.getByRole('button', { name: 'Leave space' }))
    await tl.waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/spaces/s1/members/u1'))
    expect(confirm.mock.calls[0][0]).toContain('Leave Neighbourhood Garden?')
    expect(confirm.mock.calls[0][1]).toMatchObject({ destructive: true, confirmLabel: 'Leave' })
    await tl.waitFor(() => expect(route.go).toHaveBeenCalledWith('/spaces', true))
  }, 20000)

  it('cancelling leaves nothing', async () => {
    wire('member')
    confirm.mockResolvedValue(false)
    const { tl, r } = await open()
    await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    tl.fireEvent.click(r.getByRole('button', { name: 'Leave space' }))
    await tl.waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(apiDelete).not.toHaveBeenCalled()
  }, 20000)

  it('the owner gets the note instead of the button', async () => {
    wire('owner')
    const { tl, r } = await open()
    await tl.waitFor(() => present<HTMLElement>(r.container, '.sh-space-hero'))
    tl.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    expect(r.queryByRole('button', { name: 'Leave space' })).toBeNull()
    expect(r.container.textContent).toContain('You own this space')
  }, 20000)
})
