import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { ReminderPicker } from './ReminderPicker'

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  apiMock.delete.mockReset()
})

describe('ReminderPicker', () => {
  it('fetches reminders on mount', async () => {
    apiMock.get.mockResolvedValueOnce({ reminders: [] })
    render(<ReminderPicker eventId="ev-1" />)
    await Promise.resolve()
    expect(apiMock.get).toHaveBeenCalledWith(
      '/api/calendars/events/ev-1/reminders',
      {},
    )
  })

  it('renders existing reminders with a remove button', async () => {
    apiMock.get.mockResolvedValueOnce({
      reminders: [
        {
          event_id: 'ev-1',
          user_id: 'me',
          occurrence_at: '2030-01-01T10:00:00Z',
          minutes_before: 60,
          fire_at: '2030-01-01T09:00:00Z',
          sent_at: null,
        },
      ],
    })
    const { findByText } = render(<ReminderPicker eventId="ev-1" />)
    await findByText('1 h before')
  })

  it('POSTs when a preset chip is clicked', async () => {
    apiMock.get.mockResolvedValueOnce({ reminders: [] })
    apiMock.post.mockResolvedValueOnce({})
    apiMock.get.mockResolvedValueOnce({ reminders: [] })
    const { findByText } = render(<ReminderPicker eventId="ev-1" />)
    const chip = await findByText('1 hour before')
    fireEvent.click(chip)
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/calendars/events/ev-1/reminders',
      expect.objectContaining({ minutes_before: 60 }),
    )
  })

  it('removes an active reminder by clicking its chip again', async () => {
    apiMock.get.mockResolvedValueOnce({
      reminders: [
        {
          event_id: 'ev-1',
          user_id: 'me',
          occurrence_at: '',
          minutes_before: 60,
          fire_at: '',
          sent_at: null,
        },
      ],
    })
    apiMock.delete.mockResolvedValueOnce(undefined)
    apiMock.get.mockResolvedValueOnce({ reminders: [] })
    const { findByText } = render(<ReminderPicker eventId="ev-1" />)
    const chip = await findByText('1 hour before')
    fireEvent.click(chip)
    await Promise.resolve()
    expect(apiMock.delete).toHaveBeenCalled()
  })
})
