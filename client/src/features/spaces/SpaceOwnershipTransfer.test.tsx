/**
 * The owner hands the space to someone else in the household: only local
 * full members are offered, a confirm step precedes the POST, and the
 * page reloads afterwards (the viewer is an admin now).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { mockGet, mockPost, toast, confirm } = vi.hoisted(() => ({
  mockGet: vi.fn(), mockPost: vi.fn(), toast: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { get: mockGet, post: mockPost } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { currentUser } from '@/store/auth'
import { SpaceOwnershipTransfer, ownerCandidates } from './SpaceOwnershipTransfer'

const ROSTER = [
  { user_id: 'u-pascal', role: 'owner', display_name: 'Pascal' },
  { user_id: 'u-melanie', role: 'admin', display_name: 'Melanie' },
  { user_id: 'u-lina', role: 'member', display_name: 'Lina', space_display_name: 'Lini' },
  { user_id: 'u-fan', role: 'subscriber', display_name: 'Fan' },
  { user_id: 'u-rita', role: 'member', display_name: 'Rita', instance_id: 'peer-1' },
]

beforeEach(() => {
  mockGet.mockReset()
  mockPost.mockReset()
  toast.mockReset()
  confirm.mockReset()
  currentUser.value = { user_id: 'u-pascal', username: 'pascal' } as never
})

describe('ownerCandidates', () => {
  it('offers local full members except the owner; flags other households', () => {
    const { candidates, hasRemote } = ownerCandidates(ROSTER, 'u-pascal')
    expect(candidates.map(c => c.user_id)).toEqual(['u-melanie', 'u-lina'])
    expect(hasRemote).toBe(true)
  })

  it('has nobody to offer in a one-person space', () => {
    const { candidates, hasRemote } = ownerCandidates([ROSTER[0]], 'u-pascal')
    expect(candidates).toEqual([])
    expect(hasRemote).toBe(false)
  })
})

describe('SpaceOwnershipTransfer', () => {
  it('confirms, posts the pick and reports back', async () => {
    mockGet.mockResolvedValue(ROSTER)
    mockPost.mockResolvedValue({ space_id: 's1', new_owner_user_id: 'u-lina' })
    confirm.mockResolvedValue(true)
    const onTransferred = vi.fn()
    const { findByLabelText, getByRole, getByTestId } = render(
      <SpaceOwnershipTransfer spaceId="s1" spaceName="Family" onTransferred={onTransferred} />,
    )
    const select = await findByLabelText('New owner') as HTMLSelectElement
    const options = Array.from(select.options).map(o => o.textContent)
    expect(options).toEqual(['Choose a member', 'Melanie', 'Lini'])
    expect(getByTestId('owner-transfer-remote-note')).toBeTruthy()
    const button = getByRole('button', { name: 'Hand over…' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(select, { target: { value: 'u-lina' } })
    fireEvent.click(button)
    await waitFor(() => expect(onTransferred).toHaveBeenCalled())
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining('Lini will own Family'),
      expect.objectContaining({ title: 'Make Lini the owner?', confirmLabel: 'Make owner' }),
    )
    expect(mockPost).toHaveBeenCalledWith('/api/spaces/s1/ownership', { to_user_id: 'u-lina' })
    expect(toast).toHaveBeenCalledWith('Lini now owns Family', 'success')
  })

  it('does nothing when the confirm is cancelled', async () => {
    mockGet.mockResolvedValue(ROSTER)
    confirm.mockResolvedValue(false)
    const onTransferred = vi.fn()
    const { findByLabelText, getByRole } = render(
      <SpaceOwnershipTransfer spaceId="s1" spaceName="Family" onTransferred={onTransferred} />,
    )
    fireEvent.change(await findByLabelText('New owner'), { target: { value: 'u-melanie' } })
    fireEvent.click(getByRole('button', { name: 'Hand over…' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(mockPost).not.toHaveBeenCalled()
    expect(onTransferred).not.toHaveBeenCalled()
  })

  it('shows the server refusal and keeps the owner', async () => {
    mockGet.mockResolvedValue(ROSTER)
    confirm.mockResolvedValue(true)
    mockPost.mockRejectedValue(new Error('Only someone in this household can own the space.'))
    const onTransferred = vi.fn()
    const { findByLabelText, getByRole } = render(
      <SpaceOwnershipTransfer spaceId="s1" spaceName="Family" onTransferred={onTransferred} />,
    )
    fireEvent.change(await findByLabelText('New owner'), { target: { value: 'u-melanie' } })
    fireEvent.click(getByRole('button', { name: 'Hand over…' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'Only someone in this household can own the space.', 'error',
    ))
    expect(onTransferred).not.toHaveBeenCalled()
  })

  it('says who to invite when nobody else can own it', async () => {
    mockGet.mockResolvedValue([ROSTER[0]])
    const { findByTestId, queryByLabelText } = render(
      <SpaceOwnershipTransfer spaceId="s1" spaceName="Family" onTransferred={vi.fn()} />,
    )
    expect((await findByTestId('owner-transfer-empty')).textContent).toContain('Invite someone first')
    expect(queryByLabelText('New owner')).toBeNull()
  })

  it('offers a retry when the roster fails to load', async () => {
    mockGet.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(ROSTER)
    const { findByRole, findByLabelText } = render(
      <SpaceOwnershipTransfer spaceId="s1" spaceName="Family" onTransferred={vi.fn()} />,
    )
    fireEvent.click(await findByRole('button', { name: 'Retry' }))
    expect(await findByLabelText('New owner')).toBeTruthy()
  })
})
