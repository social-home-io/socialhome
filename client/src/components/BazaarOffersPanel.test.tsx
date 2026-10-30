import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const { apiGet, apiPost } = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }))
vi.mock('@/api', () => ({
  api: { get: apiGet, post: apiPost, delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn() }))

import { BazaarOffersPanel } from './BazaarOffersPanel'
import type { BazaarListing, BazaarOffer } from '@/types'

const listing = {
  post_id: 'p1', space_id: 's1', seller_user_id: 'seller1', mode: 'negotiable',
  title: 'Blue bike', end_time: new Date(Date.now() + 86_400_000).toISOString(),
  currency: 'EUR', status: 'active', price: 12000, image_urls: [],
  created_at: '2026-01-01',
} as unknown as BazaarListing

function offer(over: Partial<BazaarOffer> = {}): BazaarOffer {
  return {
    id: 'o1', listing_post_id: 'p1', offerer_user_id: 'buyer1', amount: 5000,
    message: null, status: 'pending', created_at: 'x', responded_at: null,
    rejection_reason: null, ...over,
  }
}

describe('BazaarOffersPanel', () => {
  beforeEach(() => { apiGet.mockReset(); apiPost.mockReset() })
  afterEach(() => vi.restoreAllMocks())

  it('shows the buyer the seller’s reason under a declined offer', async () => {
    apiGet.mockResolvedValue([
      offer({ status: 'rejected', rejection_reason: 'Sold it at the door, sorry' }),
    ])
    const { container } = render(
      <BazaarOffersPanel listing={listing} currentUserId="buyer1" />,
    )
    await waitFor(() =>
      expect(container.querySelector('.sh-bazaar-reason')?.textContent)
        .toBe('Reason: “Sold it at the door, sorry”'),
    )
    expect(container.textContent).toContain('declined')
  })

  it('a declined offer without a reason shows no reason line', async () => {
    apiGet.mockResolvedValue([offer({ status: 'rejected' })])
    const { container } = render(
      <BazaarOffersPanel listing={listing} currentUserId="buyer1" />,
    )
    await waitFor(() => expect(container.textContent).toContain('declined'))
    expect(container.querySelector('.sh-bazaar-reason')).toBeNull()
  })

  it('a pending offer shows no reason line', async () => {
    apiGet.mockResolvedValue([offer({ rejection_reason: 'stale' })])
    const { container } = render(
      <BazaarOffersPanel listing={listing} currentUserId="buyer1" />,
    )
    await waitFor(() => expect(container.textContent).toContain('pending'))
    expect(container.querySelector('.sh-bazaar-reason')).toBeNull()
  })

  it('the seller’s decline sends the trimmed reason', async () => {
    apiGet.mockResolvedValue([offer()])
    apiPost.mockResolvedValue({})
    vi.spyOn(window, 'prompt').mockReturnValue('  too low  ')
    const { findByText } = render(
      <BazaarOffersPanel listing={listing} currentUserId="seller1" />,
    )
    fireEvent.click(await findByText('Decline'))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/bazaar/p1/offers/o1/reject', { reason: 'too low' },
    ))
  })

  it('Cancel on the reason prompt declines nothing', async () => {
    apiGet.mockResolvedValue([offer()])
    vi.spyOn(window, 'prompt').mockReturnValue(null)
    const { findByText } = render(
      <BazaarOffersPanel listing={listing} currentUserId="seller1" />,
    )
    fireEvent.click(await findByText('Decline'))
    await Promise.resolve()
    expect(apiPost).not.toHaveBeenCalled()
  })
})
