/**
 * A space post held for review (posts "Reviewed", §4.3): the create
 * answers 202 ``{queued: true}`` — the page says "Submitted for review",
 * not "Post shared", and doesn't hand the composer a post id. A poll rides
 * the create request. The author's pending posts show in the feed's
 * "Pending review (n)" strip.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ComposerExtras } from '@/components/Composer'

/** Matches the strip heading by its full text (label + count span). */
const heading = (text: string) => (_: string, el: Element | null) =>
  el?.tagName === 'H3' && (el.textContent ?? '').replace(/\s+/g, ' ').trim() === text


const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
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

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 's1' }, query: {}, path: '/spaces/s1' }),
  useLocation: () => ({ route: vi.fn(), url: '/spaces/s1', path: '/spaces/s1', query: {} }),
}))

const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({
  showToast: (...a: unknown[]) => showToast(...a),
  dismissToast: vi.fn(),
  toasts: { value: [] },
  ToastContainer: () => null,
}))

type Submit = (type: string, content: string, mediaUrl?: string, extras?: ComposerExtras) => Promise<string | void>
const composer = vi.hoisted(() => ({ submit: null as null | Submit }))
vi.mock('@/components/Composer', () => ({
  Composer: (p: { onSubmit: Submit }) => {
    composer.submit = p.onSubmit
    return <div data-testid="composer" />
  },
}))

function wire(mine: unknown[] = []) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Trip', features: { posts_access: 'moderated' } }
    if (url === '/api/spaces/s1/members') return [{ user_id: 'u1', role: 'member' }]
    if (url === '/api/spaces/s1/links') return { links: [] }
    if (url === '/api/spaces/s1/moderation/mine') return mine
    if (url === '/api/spaces/s1/compat') {
      return { ours: 42, min_member_proto_version: null, lagging_features: [], behind_members: [] }
    }
    return []
  })
}

async function openFeed() {
  const tl = await import('@testing-library/preact')
  tl.cleanup()
  const { default: Page } = await import('./SpaceFeedPage')
  const r = tl.render(<Page />)
  await tl.waitFor(() => expect(r.getByTestId('composer')).toBeTruthy())
  return { tl, r }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  showToast.mockReset()
  composer.submit = null
})

describe('SpaceFeedPage — posts held for review', () => {
  it('a 202 says "Submitted for review" and returns no post id', async () => {
    wire()
    await openFeed()
    apiPost.mockResolvedValueOnce({ queued: true, item_id: 'q1', feature: 'posts', action: 'create', entity: 'post', target_id: 'p1' })
    const id = await composer.submit!('text', 'hello')
    expect(id).toBeUndefined()
    expect(showToast).toHaveBeenCalledWith('Submitted for review — a moderator will look at it', 'info')
    expect(showToast).not.toHaveBeenCalledWith('Post shared', 'success')
  }, 20000)

  it('a 201 shares the post and returns its id', async () => {
    wire()
    await openFeed()
    apiPost.mockResolvedValueOnce({ id: 'p9', type: 'text', content: 'hello' })
    expect(await composer.submit!('text', 'hello')).toBe('p9')
    expect(showToast).toHaveBeenCalledWith('Post shared', 'success')
  }, 20000)

  it('a poll is sent in the create request', async () => {
    wire()
    await openFeed()
    apiPost.mockResolvedValueOnce({ id: 'p9' })
    await composer.submit!('poll', '', undefined, {
      poll: { question: 'Dinner?', options: ['Pizza', 'Tacos'], allow_multiple: false, closes_at: null },
    })
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/posts', expect.objectContaining({
      type: 'poll',
      poll: { question: 'Dinner?', options: ['Pizza', 'Tacos'], allow_multiple: false, closes_at: null },
    }))
  }, 20000)

  it('the author sees their pending posts in the feed strip', async () => {
    wire([{
      id: 'q1', space_id: 's1', feature: 'posts', action: 'create', entity: 'post', op: null,
      target_id: 'p1', submitted_by: 'u1', submitted_at: '2026-10-01T00:00:00Z',
      expires_at: '2099-01-01T00:00:00Z', status: 'pending', preview: { content: 'My pending post' },
    }])
    const { tl, r } = await openFeed()
    await tl.waitFor(() => expect(r.getByText(heading('Pending review (1)'))).toBeTruthy())
    expect(r.getByText('My pending post')).toBeTruthy()
  }, 20000)
})
