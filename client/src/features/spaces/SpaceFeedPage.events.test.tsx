/**
 * SpaceFeedPage — a calendar event's feed card leaves the open feed the
 * moment the event is deleted (the ``calendar.deleted`` frame every space
 * member gets), without a reload. The server already soft-deletes the
 * card (``CalendarFeedBridge``); this is the live half.
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
  PostCard: (p: { post: { id: string } }) => <div data-testid={`post-${p.post.id}`} />,
}))

beforeEach(() => {
  vi.resetModules()
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  apiGet.mockReset()
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: { calendar: true }, my_role: 'member' }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }, { user_id: 'u2', role: 'member' }]
    if (url === '/api/spaces/s1/feed') {
      return [
        { id: 'card', author: 'u2', type: 'event', content: 'Picnic', linked_event_id: 'ev9', created_at: '2026-01-02', reactions: {} },
        { id: 'text', author: 'u2', type: 'text', content: 'hi', created_at: '2026-01-01', reactions: {} },
      ]
    }
    if (url.endsWith('/links')) return { links: [] }
    return []
  })
})

async function renderFeed() {
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  const r = tl.render(<Page />)
  await tl.waitFor(() => expect(r.queryByTestId('post-card')).not.toBeNull())
  return { tl, r }
}

describe('a deleted calendar event leaves the open space feed', () => {
  it('its card goes at once; other posts stay', async () => {
    const { tl, r } = await renderFeed()
    await tl.act(async () => { emit('calendar.deleted', { event_id: 'ev9', calendar_id: 's1' }) })
    expect(r.queryByTestId('post-card')).toBeNull()
    expect(r.queryByTestId('post-text')).not.toBeNull()
  })

  it('an event deleted in another space changes nothing here', async () => {
    const { tl, r } = await renderFeed()
    await tl.act(async () => { emit('calendar.deleted', { event_id: 'ev9', calendar_id: 's2' }) })
    expect(r.queryByTestId('post-card')).not.toBeNull()
  })
})
