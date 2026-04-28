import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), post: vi.fn() },
}))

vi.mock('@/api', () => ({ api: apiMock }))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'me', is_admin: false } },
}))

import { EventPostCard } from './EventPostCard'
import { rsvpCounts, myRsvpStatus } from '@/store/calendar'

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  rsvpCounts.value = {}
  myRsvpStatus.value = {}
})

const futureEvent = {
  id: 'ev-1',
  calendar_id: 'sp-1',
  summary: 'Friday party',
  description: null,
  start: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
  end: new Date(Date.now() + 26 * 60 * 60 * 1000).toISOString(),
  all_day: false,
  created_by: 'someone-else',
  capacity: null as number | null,
}

describe('EventPostCard', () => {
  it('renders an orphan card when eventId is null', () => {
    const { container } = render(<EventPostCard eventId={null} />)
    expect(container.textContent).toContain('Event removed')
  })

  it('renders the summary + RSVP buttons after fetching', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    const { container, findByText } = render(<EventPostCard eventId="ev-1" />)
    await findByText('Going')
    expect(apiMock.get).toHaveBeenCalledWith('/api/calendars/events/ev-1')
    expect(container.textContent).toContain('Going')
    expect(container.textContent).toContain('Maybe')
    expect(container.textContent).toContain("Can't make it")
  })

  it('switches the going button copy to "Request to join" when capacity is set', async () => {
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-cap',
      capacity: 5,
    })
    const { container, findByText } = render(<EventPostCard eventId="ev-cap" />)
    // Wait for any RSVP button to render before checking for the
    // request-to-join copy (which is also a button label, just split
    // across an emoji span + the label text).
    await findByText('Maybe')
    expect(container.textContent).toContain('Request to join')
  })

  it('disables RSVP buttons when the event has ended', async () => {
    const past = {
      ...futureEvent,
      start: new Date(Date.now() - 2 * 3600_000).toISOString(),
      end: new Date(Date.now() - 1 * 3600_000).toISOString(),
    }
    apiMock.get.mockResolvedValueOnce({ ...past, id: 'ev-past' })
    const { container, findByText } = render(<EventPostCard eventId="ev-past" />)
    await findByText('Maybe')
    expect(container.textContent).toContain('This event has ended')
    const buttons = container.querySelectorAll('button')
    let disabledCount = 0
    buttons.forEach((b) => {
      if (b.disabled) disabledCount++
    })
    expect(disabledCount).toBeGreaterThan(0)
  })

  it('POSTs to the rsvp endpoint when a button is clicked', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    apiMock.post.mockResolvedValueOnce({ ok: true })
    const { findByText } = render(<EventPostCard eventId="ev-1" />)
    const goingBtn = await findByText('Going')
    fireEvent.click(goingBtn.closest('button')!)
    // microtask boundary
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/calendars/events/ev-1/rsvp',
      expect.objectContaining({ status: 'going' }),
    )
  })
})
