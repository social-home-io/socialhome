import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiPost, confirm, toast } = vi.hoisted(() => ({
  apiPost: vi.fn(), confirm: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { post: apiPost } }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import { ListingThreadCard, listingThreadPrice } from './ListingThreadCard'
import type { ListingThreadInfo } from '@/types'

function card(over: Partial<ListingThreadInfo> = {}): ListingThreadInfo {
  return {
    post_id: 'post-bike', space_id: 'sp', title: 'Bike', mode: 'fixed',
    status: 'active', price: 4000, start_price: null, winning_price: null,
    currency: 'EUR', image_urls: [], seller_user_id: 'u-sam', role: 'buyer',
    other_user_id: 'u-sam', writable: true, closes_at: null, ...over,
  }
}

describe('ListingThreadCard', () => {
  beforeEach(() => { apiPost.mockReset(); confirm.mockReset(); toast.mockReset() })

  it('names the listing, links back to it and tells the buyer who sees it', () => {
    const { container, getByRole } = render(
      <ListingThreadCard card={card()} otherName="Sam" />)
    const link = getByRole('link', { name: 'Bike' })
    expect(link.getAttribute('href')).toContain('/bazaar?listing=post-bike')
    expect(container.textContent).toContain('For sale')
    expect(container.textContent).toContain('Only you and the seller see this conversation.')
    // No block button for the buyer.
    expect(container.querySelector('.sh-listing-thread-card__block')).toBeNull()
  })

  it('shows the sale price and how long the pickup chat stays open', () => {
    const c = card({ status: 'sold', winning_price: 3500, closes_at: '2026-11-13T00:00:00+00:00' })
    const { container } = render(<ListingThreadCard card={c} otherName="Sam" />)
    expect(container.textContent).toContain('Sold')
    expect(container.textContent).toContain('Open until')
    expect(listingThreadPrice(c)).toContain('35')
  })

  it('lets the seller block the buyer from the thread', async () => {
    confirm.mockResolvedValue(true)
    apiPost.mockResolvedValue({ user_id: 'u-ben' })
    const onBlocked = vi.fn()
    const { getByRole } = render(
      <ListingThreadCard card={card({ role: 'seller', other_user_id: 'u-ben' })}
                         otherName="Ben" onBlocked={onBlocked} />)
    fireEvent.click(getByRole('button', { name: 'Block Ben' }))
    await waitFor(() => expect(onBlocked).toHaveBeenCalled())
    expect(apiPost).toHaveBeenCalledWith('/api/blocks', { user_id: 'u-ben' })
  })

  it('does nothing when the seller cancels the block', async () => {
    confirm.mockResolvedValue(false)
    const { getByRole } = render(
      <ListingThreadCard card={card({ role: 'seller' })} otherName={null} />)
    fireEvent.click(getByRole('button', { name: 'Block Someone' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(apiPost).not.toHaveBeenCalled()
  })
})
