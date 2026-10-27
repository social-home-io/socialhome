/** ``/bazaar?listing={post_id}`` — the link a Bazaar notification opens. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const where = vi.hoisted(() => ({ query: {} as Record<string, string> }))
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn(), url: '/bazaar', path: '/bazaar', query: where.query }),
  useRoute: () => ({ params: {}, query: where.query }),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
// The listing body has its own suite; here it only marks what is open.
vi.mock('@/components/BazaarPostBody', () => ({
  BazaarPostBody: ({ postId }: { postId: string }) => <div data-testid="listing">{postId}</div>,
  formatBazaarAmount: () => '',
}))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import BazaarPage from './BazaarPage'

beforeEach(() => {
  where.query = {}
  vi.mocked(showToast).mockClear()
  vi.mocked(api.get).mockReset().mockImplementation(async (path: string) => {
    if (path === '/api/bazaar/L-1') return { post_id: 'L-1', title: 'Bike' }
    if (path.startsWith('/api/bazaar/')) throw Object.assign(new Error('gone'), { status: 404 })
    if (path === '/api/me/bazaar/saved') return { saved: [] }
    return []
  })
})

describe('BazaarPage — a link to a listing', () => {
  it('opens the linked listing', async () => {
    where.query = { listing: 'L-1' }
    const { findByTestId } = render(<BazaarPage />)
    expect((await findByTestId('listing')).textContent).toBe('L-1')
  })

  it('a listing that is gone says so and shows the grid', async () => {
    where.query = { listing: 'L-gone' }
    const { queryByTestId } = render(<BazaarPage />)
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'That listing is no longer available.', 'info'))
    expect(queryByTestId('listing')).toBeNull()
  })
})
