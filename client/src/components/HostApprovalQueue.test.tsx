import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), post: vi.fn() },
}))

vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/utils/avatar', () => ({
  resolveAvatar: () => null,
  resolveDisplayName: (_s: unknown, uid: string) => `User ${uid}`,
}))

const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

import { HostApprovalQueue } from './HostApprovalQueue'

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
})

describe('HostApprovalQueue', () => {
  it('renders empty-state when no pending requests', async () => {
    apiMock.get.mockResolvedValueOnce({ pending: [] })
    const { findByText } = render(
      <HostApprovalQueue eventId="ev-1" spaceId="sp-1" />,
    )
    await findByText('No pending requests.')
  })

  it('lists pending rows with approve / deny buttons', async () => {
    apiMock.get.mockResolvedValueOnce({
      pending: [
        {
          user_id: 'u-bob',
          status: 'requested',
          occurrence_at: '2030-06-01T18:00:00+00:00',
          updated_at: new Date().toISOString(),
        },
      ],
    })
    const { findByText } = render(
      <HostApprovalQueue eventId="ev-1" spaceId="sp-1" />,
    )
    await findByText('Approve')
  })

  it('POSTs approve action with user + occurrence', async () => {
    apiMock.get.mockResolvedValueOnce({
      pending: [
        {
          user_id: 'u-bob',
          status: 'requested',
          occurrence_at: '2030-06-01T18:00:00+00:00',
          updated_at: new Date().toISOString(),
        },
      ],
    })
    apiMock.post.mockResolvedValueOnce({ ok: true, new_status: 'going' })
    apiMock.get.mockResolvedValueOnce({ pending: [] })
    const { findByText } = render(
      <HostApprovalQueue eventId="ev-1" spaceId="sp-1" />,
    )
    const approve = await findByText('Approve')
    fireEvent.click(approve.closest('button')!)
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/calendars/events/ev-1/approve',
      expect.objectContaining({
        user_id: 'u-bob',
        action: 'approve',
        occurrence_at: '2030-06-01T18:00:00+00:00',
      }),
    )
  })

  it("the requester's name opens their person sheet before deciding", async () => {
    openPersonSpy.mockReset()
    apiMock.get.mockResolvedValueOnce({
      pending: [{
        user_id: 'u-bob', status: 'requested',
        occurrence_at: '2030-06-01T18:00:00+00:00', updated_at: new Date().toISOString(),
      }],
    })
    const { findByTestId } = render(<HostApprovalQueue eventId="ev-1" spaceId="sp-1" />)
    const who = await findByTestId('host-queue-person')
    expect(who.tagName).toBe('BUTTON')
    expect(who.textContent).toBe('User u-bob')
    fireEvent.click(who)
    expect(openPersonSpy).toHaveBeenCalledWith('u-bob', { person: {} })
    expect(apiMock.post).not.toHaveBeenCalled()
  })
})
