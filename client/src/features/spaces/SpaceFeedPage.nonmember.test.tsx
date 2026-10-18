/**
 * A viewer with no seat in the space (the server answers its feed with
 * 403 "Not a space member.") is sent to the space's landing page —
 * ``/spaces/{id}/about`` — instead of an empty feed with a composer. The
 * landing page shows a public / global space's join options and "Couldn't
 * load this space. It may not be public…" for anything else.
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

const routeSpy = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1' }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: routeSpy, url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

function refuse(status: number) {
  return Object.assign(new Error('Not a space member.'), { status })
}

async function open(feedError: Error | null) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1/feed') {
      if (feedError) throw feedError
      return []
    }
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: {} }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }]
    if (url === '/api/spaces/s1/links') return { links: [] }
    return []
  })
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  return { tl, r: tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  routeSpy.mockReset()
})

describe('a viewer without a seat', () => {
  it('a 403 on the feed replaces the page with the space landing page', async () => {
    const { tl } = await open(refuse(403))
    await tl.waitFor(() => expect(routeSpy).toHaveBeenCalledWith('/spaces/s1/about', true))
  }, 20000)

  it('a member stays on the feed', async () => {
    const { tl, r } = await open(null)
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await tl.waitFor(() => expect(r.container.textContent).toContain('No posts in this space yet.'))
    expect(routeSpy).not.toHaveBeenCalledWith('/spaces/s1/about', true)
  }, 20000)

  it('any other feed failure is not treated as "no seat"', async () => {
    const { tl } = await open(refuse(500))
    await tl.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await new Promise((r) => setTimeout(r, 50))
    expect(routeSpy).not.toHaveBeenCalledWith('/spaces/s1/about', true)
  }, 20000)
})
