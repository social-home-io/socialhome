import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiGet, apiPost, route, toast } = vi.hoisted(() => ({
  apiGet: vi.fn(), apiPost: vi.fn(), route: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { get: apiGet, post: apiPost } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route, url: '/', path: '/' }) }))
vi.mock('./Toast', () => ({ showToast: toast }))

import { AskSellerButton, ListingQuestions } from './BazaarAskSeller'

describe('AskSellerButton', () => {
  beforeEach(() => { apiPost.mockReset(); route.mockReset(); toast.mockReset() })

  it('opens the conversation the server picked (DM or listing thread)', async () => {
    apiPost.mockResolvedValue({ kind: 'thread', conversation_id: 'bzt-1' })
    const { getByRole } = render(<AskSellerButton postId="p1" />)
    fireEvent.click(getByRole('button', { name: /Ask the seller/ }))
    await waitFor(() => expect(route).toHaveBeenCalledWith('/dms/bzt-1'))
    expect(apiPost).toHaveBeenCalledWith('/api/bazaar/p1/ask')
  })

  it('says "Message the seller" after the sale', () => {
    const { getByRole } = render(<AskSellerButton postId="p1" afterSale />)
    expect(getByRole('button').textContent).toContain('Message the seller')
  })

  it('toasts the translated refusal and stays put', async () => {
    apiPost.mockRejectedValueOnce(new Error("The seller's household needs an update"))
    const { getByRole } = render(<AskSellerButton postId="p1" />)
    fireEvent.click(getByRole('button'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "The seller's household needs an update", 'error'))
    expect(route).not.toHaveBeenCalled()
  })
})

describe('ListingQuestions', () => {
  beforeEach(() => apiGet.mockReset())

  it('lists every buyer thread with its unread count and links into it', async () => {
    apiGet.mockResolvedValue([
      { conversation_id: 'bzt-1', other_user_id: 'u-ben', other_display_name: 'Ben',
        unread: 2, last_message_at: null, writable: true, closes_at: null },
      { conversation_id: 'bzt-2', other_user_id: 'u-bea', other_display_name: null,
        unread: 0, last_message_at: null, writable: false, closes_at: '2026-01-01' },
    ])
    const { container, findByText } = render(<ListingQuestions postId="p1" />)
    await findByText('Ben')
    expect(apiGet).toHaveBeenCalledWith('/api/bazaar/p1/threads')
    const links = container.querySelectorAll('a')
    expect(links[0].getAttribute('href')).toContain('/dms/bzt-1')
    expect(container.textContent).toContain('Someone')
    expect(container.textContent).toContain('Closed')
    expect(container.querySelector('.sh-dm-unread')?.textContent).toBe('2')
  })

  it('is titled as private conversations, never "Questions" (that is the public FAQ)', async () => {
    apiGet.mockResolvedValue([
      { conversation_id: 'bzt-1', other_user_id: 'u-ben', other_display_name: 'Ben',
        unread: 0, last_message_at: null, writable: true, closes_at: null },
    ])
    const { findByRole, container } = render(<ListingQuestions postId="p1" />)
    const heading = await findByRole('heading', { level: 4 })
    expect(heading.textContent).toContain('Private conversations')
    expect(heading.textContent).not.toMatch(/^Questions/)
    expect(container.querySelector('section')?.getAttribute('aria-label'))
      .toBe('Private conversations')
  })

  it('renders nothing without questions', async () => {
    apiGet.mockResolvedValue([])
    const { container } = render(<ListingQuestions postId="p1" />)
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
