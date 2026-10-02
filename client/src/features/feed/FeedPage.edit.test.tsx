/** Household feed: Edit on the author's own posts (and every post for a
 *  household admin — the server's rule); Save PATCHes the feed post. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiPatch = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockImplementation(async (url: string) => (url.startsWith('/api/feed') ? [
      { id: 'mine', author: 'u1', type: 'text', content: 'a', created_at: '2026-01-01', reactions: {}, comment_count: 0 },
      { id: 'theirs', author: 'u2', type: 'text', content: 'b', created_at: '2026-01-01', reactions: {}, comment_count: 0 },
    ] : [])),
    post: vi.fn().mockResolvedValue({}),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

const me = vi.hoisted(() => ({ value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } }))
vi.mock('@/store/auth', () => ({
  currentUser: me,
  token: { value: 'tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/components/PostCard', () => ({
  PostCard: (p: { post: { id: string }; onEdit?: (c: string) => Promise<boolean> }) => (
    <div data-testid={`post-${p.post.id}`}>
      {p.onEdit ? <button onClick={() => void p.onEdit!('edited')}>edit {p.post.id}</button> : 'locked'}
    </div>
  ),
}))

beforeEach(() => { cleanup(); apiPatch.mockReset() })

describe('FeedPage post edit', () => {
  it('a member edits their own post only, via PATCH /api/feed/posts/{id}', async () => {
    me.value = { ...me.value, is_admin: false }
    apiPatch.mockResolvedValue({ id: 'mine', author: 'u1', type: 'text', content: 'edited', created_at: '2026-01-01', reactions: {}, comment_count: 0 })
    const { default: FeedPage } = await import('./FeedPage')
    const r = render(<FeedPage />)
    await waitFor(() => r.getByText('edit mine'))
    expect(r.getByTestId('post-theirs').textContent).toBe('locked')
    fireEvent.click(r.getByText('edit mine'))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/feed/posts/mine', { content: 'edited' }))
  })

  it('a household admin may edit anyone\'s post', async () => {
    me.value = { ...me.value, is_admin: true }
    const { default: FeedPage } = await import('./FeedPage')
    const r = render(<FeedPage />)
    await waitFor(() => r.getByText('edit theirs'))
  })
})
