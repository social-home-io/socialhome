/**
 * Space post edit (inline) and space event delete from the agenda.
 *
 * - Edit is offered with the same rule as Delete: the author, or content
 *   authority (owner / admin / moderator) on somebody else's post; never
 *   in an archived space or where posts are admin-only for the viewer.
 *   Saving PATCHes the space post route; a 202 (held for review) closes
 *   the editor with the review toast, a 403 keeps it open and toasts.
 * - The agenda's Delete appears only on rows the server marked
 *   ``can_edit: true``; it hides the event at once and DELETEs the stored
 *   series id when the Undo toast closes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))

const toast = vi.hoisted(() => ({ show: vi.fn((..._a: unknown[]) => 1) }))
vi.mock('@/components/Toast', async (orig) => ({
  ...(await orig<object>()),
  showToast: toast.show,
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const route = vi.hoisted(() => ({ id: 's1', query: {} as Record<string, string> }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: route.id }, query: route.query, path: `/spaces/${route.id}` }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: route.query }),
}))

const edits = vi.hoisted(() => ({ results: [] as boolean[] }))
vi.mock('@/components/PostCard', () => ({
  PostCard: (p: { post: { id: string }; onEdit?: (c: string) => Promise<boolean> }) => (
    <div data-testid={`post-${p.post.id}`}>
      {p.onEdit
        ? <button onClick={async () => { edits.results.push(await p.onEdit!('new text')) }}>edit</button>
        : 'locked'}
    </div>
  ),
}))

const SOON = new Date(Date.now() + 2 * 3600_000)

function wire(role: string, opts: { archived?: boolean; features?: Record<string, unknown> } = {}) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s2') return { id: 's2', name: 'Other', features: { calendar: true } }
    if (url === '/api/spaces/s2/members') return [{ user_id: 'u1', role }]
    if (url === '/api/spaces/s1') {
      return {
        id: 's1', name: 'Trip', archived: !!opts.archived,
        features: { calendar: true, ...opts.features },
      }
    }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role }]
    if (url === '/api/spaces/s1/feed') {
      return [
        { id: 'mine', author: 'u1', type: 'text', content: 'a', created_at: '2026-01-01', reactions: {} },
        { id: 'theirs', author: 'u2', type: 'text', content: 'b', created_at: '2026-01-01', reactions: {} },
      ]
    }
    if (url === '/api/spaces/s1/calendar/events') {
      const at = (h: number) => new Date(SOON.getTime() + h * 3600_000).toISOString()
      return [
        { id: 'ev1', calendar_id: 's1', summary: 'Picnic', start: at(0), end: at(1), all_day: false, created_by: 'u2', can_edit: true },
        { id: 'ev2', calendar_id: 's1', summary: 'Locked', start: at(0), end: at(1), all_day: false, created_by: 'u2', can_edit: false },
      ]
    }
    if (url.endsWith('/links')) return { links: [] }
    return []
  })
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
  toast.show.mockClear()
  edits.results = []
  route.query = {}
  route.id = 's1'
})

async function renderPage() {
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

describe('space post edit', () => {
  it('a member edits only their own post', async () => {
    wire('member')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-mine').textContent).toBe('edit'))
    expect(r.getByTestId('post-theirs').textContent).toBe('locked')
  }, 20000)

  it('a moderator edits somebody else\'s post', async () => {
    wire('moderator')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-theirs').textContent).toBe('edit'))
  }, 20000)

  it('nobody edits in an archived space', async () => {
    wire('owner', { archived: true })
    const { tl, r } = await renderPage()
    await tl.waitFor(() => expect(r.getByTestId('post-mine').textContent).toBe('locked'))
  }, 20000)

  it('a subscriber (even a demoted author) gets no Edit', async () => {
    wire('subscriber')
    const { tl, r } = await renderPage()
    await tl.waitFor(() => r.getByTestId('post-mine'))
    await new Promise(res => setTimeout(res, 20))
    expect(r.getByTestId('post-mine').textContent).toBe('locked')
  }, 20000)

  it('no Edit until the member list says who the viewer is', async () => {
    wire('member')
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation((url: string) =>
      url === '/api/spaces/s1/members' ? new Promise(() => {}) : base(url))
    const { tl, r } = await renderPage()
    await tl.waitFor(() => r.getByTestId('post-mine'))
    await new Promise(res => setTimeout(res, 20))
    expect(r.getByTestId('post-mine').textContent).toBe('locked')
  }, 20000)

  it('saving PATCHes the space post route and updates the card', async () => {
    wire('member')
    apiPatch.mockResolvedValue({ id: 'mine', content: 'new text', edited_at: '2026-01-02T00:00:00Z' })
    const { tl, r } = await renderPage()
    await tl.waitFor(() => r.getByTestId('post-mine'))
    tl.fireEvent.click(r.getByText('edit'))
    await tl.waitFor(() => expect(edits.results).toEqual([true]))
    expect(apiPatch).toHaveBeenCalledWith('/api/spaces/s1/posts/mine', { content: 'new text' })
    expect(toast.show).toHaveBeenCalledWith('Post updated', 'success')
  }, 20000)

  it('a 202 (held for review) closes the editor with the review toast', async () => {
    wire('member')
    apiPatch.mockResolvedValue({ queued: true, item_id: 'q1', feature: 'posts', action: 'edit' })
    const { tl, r } = await renderPage()
    await tl.waitFor(() => r.getByTestId('post-mine'))
    tl.fireEvent.click(r.getByText('edit'))
    await tl.waitFor(() => expect(edits.results).toEqual([true]))
    expect(toast.show).toHaveBeenCalledWith(
      'Submitted for review — a moderator will look at it', 'info')
  }, 20000)

  it('a 403 keeps the editor open and says why', async () => {
    wire('member')
    apiPatch.mockRejectedValue(Object.assign(new Error('Only space admins can change this here.'), { status: 403 }))
    const { tl, r } = await renderPage()
    await tl.waitFor(() => r.getByTestId('post-mine'))
    tl.fireEvent.click(r.getByText('edit'))
    await tl.waitFor(() => expect(edits.results).toEqual([false]))
    expect(toast.show).toHaveBeenCalledWith(
      'Couldn’t save the edit: Only space admins can change this here.', 'error')
  }, 20000)
})

describe('space event delete', () => {
  it('offers Delete only on can_edit rows, and deletes with Undo', async () => {
    route.query = { tab: 'calendar' }
    wire('member')
    apiDelete.mockResolvedValue({ ok: true })
    const { tl, r } = await renderPage()
    const picnic = await tl.waitFor(() => r.getByRole('button', { name: /Picnic/ }))
    tl.fireEvent.click(picnic)
    const del = await tl.waitFor(() => r.getByRole('button', { name: 'Delete' }))
    tl.fireEvent.click(r.getByRole('button', { name: /Locked/ }))
    // Opening another row collapses the first: the locked one has none.
    await tl.waitFor(() => expect(r.queryByRole('button', { name: 'Delete' })).toBeNull())
    tl.fireEvent.click(r.getByRole('button', { name: /Picnic/ }))
    tl.fireEvent.click(await tl.waitFor(() => r.getByRole('button', { name: 'Delete' })))
    expect(del).toBeTruthy()
    // Hidden at once; the DELETE waits for the Undo toast to close.
    await tl.waitFor(() => expect(r.queryByRole('button', { name: /Picnic/ })).toBeNull())
    expect(apiDelete).not.toHaveBeenCalled()
    const call = toast.show.mock.calls.find(c => c[0] === 'Deleted “Picnic”') as unknown[] | undefined
    expect(call).toBeTruthy()
    const opts = call![2] as { onExpire: () => void }
    opts.onExpire()
    await tl.waitFor(() =>
      expect(apiDelete).toHaveBeenCalledWith('/api/spaces/s1/calendar/events/ev1'))
  }, 20000)
})

describe('space event delete after leaving the space', () => {
  it('a commit landing on another space does not reload this calendar', async () => {
    route.query = { tab: 'calendar' }
    wire('member')
    apiDelete.mockResolvedValue({ ok: true })
    const { tl, r } = await renderPage()
    tl.fireEvent.click(await tl.waitFor(() => r.getByRole('button', { name: /Picnic/ })))
    tl.fireEvent.click(await tl.waitFor(() => r.getByRole('button', { name: 'Delete' })))
    const call = toast.show.mock.calls.find(c => c[0] === 'Deleted “Picnic”') as unknown[]
    // Navigate to another space before the Undo window closes.
    route.id = 's2'
    route.query = {}
    tl.cleanup()
    const { default: Page } = await import('./SpaceFeedPage')
    const r2 = tl.render(<Page />)
    await tl.waitFor(() => expect(r2.container.textContent).toContain('OT'))
    apiGet.mockClear()
    ;(call[2] as { onExpire: () => void }).onExpire()
    await tl.waitFor(() =>
      expect(apiDelete).toHaveBeenCalledWith('/api/spaces/s1/calendar/events/ev1'))
    await new Promise(res => setTimeout(res, 20))
    expect(apiGet.mock.calls.map(c => c[0])).not.toContain('/api/spaces/s1/calendar/events')
  }, 20000)
})

describe('seriesEventId', () => {
  it('maps an expanded occurrence id to its stored series', async () => {
    const { seriesEventId } = await import('./SpaceFeedPage')
    expect(seriesEventId('abc@2026-01-01T10:00:00+00:00')).toBe('abc')
    expect(seriesEventId('abc')).toBe('abc')
  })
})
