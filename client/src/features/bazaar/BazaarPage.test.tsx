import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, act } from '@testing-library/preact'

const { apiGet, wsHandlers } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  wsHandlers: new Map<string, Set<(e: { type: string; data: Record<string, unknown> }) => void>>(),
}))

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: apiGet,
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: Record<string, unknown> }) => void) => {
      if (!wsHandlers.has(type)) wsHandlers.set(type, new Set())
      wsHandlers.get(type)!.add(h)
      return () => { wsHandlers.get(type)?.delete(h) }
    },
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/store/pageTitle', () => ({ useTitle: () => {} }))
vi.mock('preact-iso', () => ({ useLocation: () => ({ query: {} }) }))

import BazaarPage from './BazaarPage'

const future = () => new Date(Date.now() + 86_400_000).toISOString()
function listing(id: string, over: Record<string, unknown> = {}) {
  return {
    post_id: id, space_id: 's1', seller_user_id: 'u2', mode: 'fixed',
    title: `Item ${id}`, description: null, end_time: future(), currency: 'EUR',
    status: 'active', price: 1000, start_price: null, step_price: null,
    winner_user_id: null, winning_price: null, sold_at: null,
    created_at: '2026-01-01', image_urls: [], ...over,
  }
}

function emit(type: string, data: Record<string, unknown>) {
  act(() => {
    wsHandlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
  })
}

async function mount() {
  apiGet.mockImplementation((url: string, params?: Record<string, string>) => {
    if (url === '/api/bazaar' && params?.seller === 'me') return Promise.resolve([])
    if (url === '/api/bazaar') return Promise.resolve([listing('p1'), listing('p2')])
    if (url === '/api/me/bazaar/saved') return Promise.resolve({ saved: [] })
    if (url === '/api/bazaar/p1') {
      return Promise.resolve(listing('p1', { title: 'Renamed', status: 'sold', winning_price: 800 }))
    }
    return Promise.resolve([])
  })
  const r = render(<BazaarPage />)
  await waitFor(() => expect(r.container.textContent).toContain('Item p1'))
  apiGet.mockClear()
  return r.container
}

describe('BazaarPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    wsHandlers.clear()
  })

  it('a new bid anywhere re-reads nothing (it used to reload all three lists)', async () => {
    await mount()
    emit('bazaar.bid_placed', { listing_post_id: 'p1', amount: 1200, new_end_time: future() })
    await Promise.resolve()
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('a cancelled or sold listing is patched from the frame', async () => {
    const c = await mount()
    emit('bazaar.listing_cancelled', { listing_post_id: 'p2' })
    await waitFor(() =>
      expect(c.querySelector('.sh-bazaar-tile--cancelled')?.textContent).toContain('Item p2'),
    )
    emit('bazaar.offer_accepted', { listing_post_id: 'p1', buyer_user_id: 'u3', price: 900 })
    await waitFor(() =>
      expect(c.querySelector('.sh-bazaar-tile--sold')?.textContent).toContain('9.00'),
    )
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('an edited or closed listing re-reads just that listing', async () => {
    const c = await mount()
    emit('bazaar.listing_updated', { listing_post_id: 'p1' })
    await waitFor(() => expect(c.textContent).toContain('Renamed'))
    expect(apiGet.mock.calls.map(call => call[0])).toEqual(['/api/bazaar/p1'])
  })

  it('a new listing reloads the browse list, not the saved ones', async () => {
    await mount()
    emit('bazaar.listing_created', { listing_post_id: 'p3', seller_user_id: 'u2' })
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(apiGet.mock.calls.map(call => call[0])).toEqual(['/api/bazaar'])
  })
})
