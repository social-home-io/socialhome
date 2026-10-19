import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
const { toast, confirm } = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { AdminReassignOwner } from './AdminReassignOwner'

beforeEach(() => {
  api.get.mockReset(); api.post.mockReset(); toast.mockReset(); confirm.mockReset()
  api.get.mockResolvedValue({
    owner_active: false,
    candidates: [{ user_id: 'u-lina', display_name: 'Lina', role: 'member' }],
  })
})

describe('AdminReassignOwner', () => {
  it('lists the local members, confirms and reassigns', async () => {
    api.post.mockResolvedValue({})
    confirm.mockResolvedValue(true)
    const onDone = vi.fn()
    const onClose = vi.fn()
    const view = render(<AdminReassignOwner spaceId="s1" spaceName="Family" open
                                            onClose={onClose} onDone={onDone} />)
    const select = await view.findByLabelText('New owner')
    expect(api.get).toHaveBeenCalledWith('/api/admin/spaces/s1/ownership')
    fireEvent.change(select, { target: { value: 'u-lina' } })
    fireEvent.click(view.getByRole('button', { name: 'Hand over…' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/admin/spaces/s1/ownership', { to_user_id: 'u-lina' },
    ))
    expect(confirm).toHaveBeenCalledWith('Lina will own Family.', expect.anything())
    expect(onDone).toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith('Lina now owns Family', 'success')
  })

  it('says when nobody from the household is left in the space', async () => {
    api.get.mockResolvedValue({ owner_active: false, candidates: [] })
    const view = render(<AdminReassignOwner spaceId="s1" spaceName="Family" open
                                            onClose={vi.fn()} onDone={vi.fn()} />)
    expect(await view.findByTestId('admin-reassign-empty')).toBeTruthy()
  })

  it('shows a refusal and stays open', async () => {
    api.post.mockRejectedValue(new Error('The owner is still here.'))
    confirm.mockResolvedValue(true)
    const onClose = vi.fn()
    const view = render(<AdminReassignOwner spaceId="s1" spaceName="Family" open
                                            onClose={onClose} onDone={vi.fn()} />)
    fireEvent.change(await view.findByLabelText('New owner'), { target: { value: 'u-lina' } })
    fireEvent.click(view.getByRole('button', { name: 'Hand over…' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('The owner is still here.', 'error'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('loads nothing while closed', () => {
    render(<AdminReassignOwner spaceId="s1" spaceName="Family" open={false}
                               onClose={vi.fn()} onDone={vi.fn()} />)
    expect(api.get).not.toHaveBeenCalled()
  })
})
