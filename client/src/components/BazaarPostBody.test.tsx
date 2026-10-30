import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('@/api', () => ({
  api: { get: apiGet, post: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'seller1' } } }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))
vi.mock('./BazaarOffersPanel', () => ({ BazaarOffersPanel: () => null }))
vi.mock('./SaveListingButton', () => ({ SaveListingButton: () => null }))
vi.mock('./FileRenderer', () => ({ ImageRenderer: () => null }))

import { BazaarPostBody } from './BazaarPostBody'
import { currentUser } from '@/store/auth'

const future = () => new Date(Date.now() + 86_400_000).toISOString()

function listing(over: Record<string, unknown> = {}) {
  return {
    post_id: 'p1', space_id: 's1', seller_user_id: 'seller1', mode: 'offer',
    title: 'Vinyl', end_time: future(), currency: 'EUR', status: 'active',
    created_at: '2026-01-01', image_urls: [], ...over,
  }
}

function mockApi(l: object, bids: object[], offers: object[]) {
  apiGet.mockImplementation((url: string) => {
    const u = String(url ?? '')
    if (u.endsWith('/bids')) return Promise.resolve(bids)
    if (u.endsWith('/offers')) return Promise.resolve(offers)
    return Promise.resolve(l)
  })
}

describe('BazaarPostBody activity count', () => {
  beforeEach(() => apiGet.mockReset())

  it('counts OFFERS from the offers table for offer mode (regression: was reading bids → always 0)', async () => {
    mockApi(
      listing({ mode: 'offer' }),
      [],  // no bids
      [{ id: 'o1', listing_post_id: 'p1', offerer_user_id: 'buyer1',
         amount: 4000, status: 'pending', created_at: 'x' }],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 offer'))
    expect(container.textContent).not.toContain('0 offer')
  })

  it('ignores non-pending offers in the count', async () => {
    mockApi(
      listing({ mode: 'negotiable', price: 1000 }),
      [],
      [
        { id: 'o1', listing_post_id: 'p1', offerer_user_id: 'b1', amount: 1, status: 'rejected', created_at: 'x' },
        { id: 'o2', listing_post_id: 'p1', offerer_user_id: 'b2', amount: 2, status: 'pending', created_at: 'x' },
      ],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 offer'))
  })

  it('counts BIDS for auction mode', async () => {
    mockApi(
      listing({ mode: 'auction', start_price: 1000 }),
      [{ id: 'b1', listing_post_id: 'p1', bidder_user_id: 'b1', amount: 1100, created_at: 'x' }],
      [],
    )
    const { container } = render(<BazaarPostBody postId="p1" />)
    await waitFor(() => expect(container.textContent).toContain('1 bid'))
  })
})

describe('BazaarPostBody — protected buyer', () => {
  beforeEach(() => apiGet.mockReset())

  it('shows the protected notice instead of Buy / bid / offer', async () => {
    const saved = currentUser.value
    ;(currentUser as { value: unknown }).value = {
      user_id: 'kid1', restrictions: ['bazaar'], protected: true,
    }
    try {
      mockApi(listing({ mode: 'fixed', price: 1000 }), [], [])
      const { container, findByRole } = render(<BazaarPostBody postId="p1" />)
      const note = await findByRole('note')
      expect(note.getAttribute('data-capability')).toBe('bazaar')
      expect(container.textContent).not.toContain('Buy for')
      expect(container.querySelector('form.sh-bazaar-bid-form')).toBeNull()
    } finally {
      ;(currentUser as { value: unknown }).value = saved
    }
  })

  it('an unrestricted buyer still gets the bid form', async () => {
    const saved = currentUser.value
    ;(currentUser as { value: unknown }).value = { user_id: 'adult1', restrictions: [] }
    try {
      mockApi(listing({ mode: 'auction', start_price: 1000 }), [], [])
      const { container } = render(<BazaarPostBody postId="p1" />)
      await waitFor(() =>
        expect(container.querySelector('form.sh-bazaar-bid-form')).not.toBeNull(),
      )
      expect(container.querySelector('[role="note"]')).toBeNull()
    } finally {
      ;(currentUser as { value: unknown }).value = saved
    }
  })
})

describe('BazaarPostBody — a follower', () => {
  beforeEach(() => apiGet.mockReset())

  it('sees the listing and a note, never Buy / bid / offer controls', async () => {
    const saved = currentUser.value
    ;(currentUser as { value: unknown }).value = { user_id: 'fan1', restrictions: [] }
    try {
      mockApi(listing({ mode: 'fixed', price: 1000, viewer_can_trade: false }), [], [])
      const { container, findByRole } = render(<BazaarPostBody postId="p1" />)
      const note = await findByRole('status')
      expect(note.textContent).toContain('buying, bidding and offers are for members')
      expect(container.textContent).toContain('Vinyl')
      expect(container.textContent).not.toContain('Buy for')
      expect(container.querySelector('form.sh-bazaar-bid-form')).toBeNull()
    } finally {
      ;(currentUser as { value: unknown }).value = saved
    }
  })
})

describe('BazaarPostBody — a declined bid', () => {
  beforeEach(() => apiGet.mockReset())

  const bid = (over: Record<string, unknown> = {}) => ({
    id: 'b1', listing_post_id: 'p1', bidder_user_id: 'bidder1', amount: 1500,
    message: null, accepted: false, rejected: true,
    rejection_reason: 'My sister wants it after all', withdrawn: false,
    created_at: 'x', ...over,
  })

  async function asBidder(bids: object[]) {
    const saved = currentUser.value
    ;(currentUser as { value: unknown }).value = { user_id: 'bidder1', restrictions: [] }
    try {
      mockApi(listing({ mode: 'auction', start_price: 1000 }), bids, [])
      const r = render(<BazaarPostBody postId="p1" />)
      await waitFor(() =>
        expect(r.container.querySelector('form.sh-bazaar-bid-form')).not.toBeNull(),
      )
      return r.container
    } finally {
      ;(currentUser as { value: unknown }).value = saved
    }
  }

  it('tells the bidder it was declined, and why', async () => {
    const container = await asBidder([bid()])
    const note = container.querySelector('.sh-bazaar-mybid--declined')
    expect(note?.textContent).toContain('was declined')
    expect(note?.querySelector('.sh-bazaar-reason')?.textContent)
      .toBe('Reason: “My sister wants it after all”')
  })

  it('says nothing more when no reason was given', async () => {
    const container = await asBidder([bid({ rejection_reason: null })])
    expect(container.querySelector('.sh-bazaar-mybid--declined')).not.toBeNull()
    expect(container.querySelector('.sh-bazaar-reason')).toBeNull()
  })

  it('a newer bid of theirs replaces the note', async () => {
    const container = await asBidder([
      bid(),
      bid({ id: 'b2', amount: 2000, rejected: false, rejection_reason: null }),
    ])
    expect(container.querySelector('.sh-bazaar-mybid--declined')).toBeNull()
  })

  it('someone else’s declined bid is not shown as theirs', async () => {
    const container = await asBidder([bid({ bidder_user_id: 'other1' })])
    expect(container.querySelector('.sh-bazaar-mybid--declined')).toBeNull()
  })
})

describe('BazaarPostBody — withdrawing a bid', () => {
  beforeEach(() => apiGet.mockReset())

  const myBid = {
    id: 'b1', listing_post_id: 'p1', bidder_user_id: 'bidder1', amount: 1500,
    message: null, accepted: false, rejected: false, rejection_reason: null,
    withdrawn: false, created_at: 'x',
  }

  async function asBidder(endTime: string) {
    const saved = currentUser.value
    ;(currentUser as { value: unknown }).value = { user_id: 'bidder1', restrictions: [] }
    try {
      mockApi(listing({ mode: 'auction', start_price: 1000, end_time: endTime }), [myBid], [])
      const r = render(<BazaarPostBody postId="p1" />)
      await waitFor(() =>
        expect(r.container.querySelector('.sh-bazaar-mybid')).not.toBeNull(),
      )
      return r.container
    } finally {
      ;(currentUser as { value: unknown }).value = saved
    }
  }

  const withdrawBtn = (c: Element) =>
    Array.from(c.querySelectorAll('.sh-bazaar-mybid button'))
      .find(b => b.textContent === 'Withdraw') ?? null

  it('offers Withdraw while the listing runs', async () => {
    const c = await asBidder(future())
    expect(withdrawBtn(c)).not.toBeNull()
  })

  it('no Withdraw once end_time passed, before the expiry sweep closes it', async () => {
    // The server refuses (409) from end_time on; the status still reads
    // active for up to a minute.
    const c = await asBidder(new Date(Date.now() - 5_000).toISOString())
    expect(c.querySelector('.sh-bazaar-mybid')?.textContent).toContain('15.00')
    expect(withdrawBtn(c)).toBeNull()
  })

  it('Withdraw goes away at end_time without a reload', async () => {
    const c = await asBidder(new Date(Date.now() + 400).toISOString())
    expect(withdrawBtn(c)).not.toBeNull()
    await waitFor(() => expect(withdrawBtn(c)).toBeNull(), { timeout: 3_000 })
  })
})
