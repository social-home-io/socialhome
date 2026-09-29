/**
 * A protected account sees the Bazaar's "your account is protected" notice
 * once — on the list, or inside the open listing where "Buy" would be —
 * never twice on the same screen.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const future = () => new Date(Date.now() + 86_400_000).toISOString()
const LISTING = {
  post_id: 'p1', space_id: 's1', seller_user_id: 'seller1', mode: 'fixed',
  title: 'Vinyl', price: 1000, end_time: future(), currency: 'EUR',
  status: 'active', created_at: '2026-01-01', image_urls: [],
}

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/store/auth', () => ({
  currentUser: {
    value: {
      user_id: 'kid1', username: 'kid', display_name: 'Kid', is_admin: false,
      protected: true, restrictions: ['bazaar'],
    },
  },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))
vi.mock('@/components/BazaarOffersPanel', () => ({ BazaarOffersPanel: () => null }))
vi.mock('@/components/SaveListingButton', () => ({ SaveListingButton: () => null }))
vi.mock('@/components/FileRenderer', () => ({ ImageRenderer: () => null }))

import BazaarPage from './BazaarPage'

apiGet.mockImplementation((url: string, params?: Record<string, string>) => {
  if (url === '/api/bazaar') return Promise.resolve(params?.seller ? [] : [LISTING])
  if (url === '/api/me/bazaar/saved') return Promise.resolve({ saved: [] })
  if (url.endsWith('/bids') || url.endsWith('/offers')) return Promise.resolve([])
  if (url === '/api/bazaar/p1') return Promise.resolve(LISTING)
  return Promise.resolve([])
})

const notices = (root: Element) =>
  root.querySelectorAll('.sh-protected-notice[data-capability="bazaar"]')

describe('BazaarPage — protected account', () => {
  it('shows the notice once on the list and once in an open listing', async () => {
    const { container, findByText } = render(<BazaarPage />)
    const card = await findByText('Vinyl')
    expect(notices(container)).toHaveLength(1)
    expect(container.textContent).not.toContain('+ New listing')

    fireEvent.click(card)
    await waitFor(() => expect(container.textContent).toContain('← Back to listings'))
    await waitFor(() => expect(notices(container)).toHaveLength(1))
    expect(container.textContent).not.toContain('Buy for')
  })
})
