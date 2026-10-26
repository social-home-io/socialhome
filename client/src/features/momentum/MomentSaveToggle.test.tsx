import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { MomentSaveToggle } from './MomentSaveToggle'

describe('MomentSaveToggle', () => {
  beforeEach(() => {
    vi.mocked(api.put).mockReset()
    vi.mocked(api.delete).mockReset()
    vi.mocked(showToast).mockReset()
  })

  it('is hidden where Save is not offered and nothing is saved', () => {
    const { container } = render(
      <MomentSaveToggle moment={{ id: 'm1', saved_by_me: false, can_save: false }} />,
    )
    expect(container.querySelector('button')).toBeNull()
  })

  it('saves with aria-pressed and a toast', async () => {
    vi.mocked(api.put).mockResolvedValue({ moment_id: 'm1', saved: true })
    const onChange = vi.fn()
    const { getByRole } = render(
      <MomentSaveToggle moment={{ id: 'm1', saved_by_me: false, can_save: true }} onChange={onChange} />,
    )
    const btn = getByRole('button', { name: /Save/ })
    expect(btn.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(btn)
    await waitFor(() => expect(btn.getAttribute('aria-pressed')).toBe('true'))
    expect(api.put).toHaveBeenCalledWith('/api/moments/m1/save', {})
    expect(btn.textContent).toContain('Saved')
    expect(showToast).toHaveBeenCalledWith('Saved', 'success')
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('removes a save even where Save is no longer offered', async () => {
    vi.mocked(api.delete).mockResolvedValue({ moment_id: 'm1', saved: false })
    const { getByRole, container } = render(
      <MomentSaveToggle moment={{ id: 'm1', saved_by_me: true, can_save: false }} variant="chip" />,
    )
    const btn = getByRole('button', { name: /Saved/ })
    expect(btn.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(btn)
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/m1/save'))
    expect(showToast).toHaveBeenCalledWith('Removed from saved', 'success')
    // Not offered any more, so the toggle goes once it is off.
    await waitFor(() => expect(container.querySelector('button')).toBeNull())
  })

  it('keeps the state and says why when the server refuses', async () => {
    vi.mocked(api.put).mockRejectedValue(new Error('This moment can’t be saved'))
    const { getByRole } = render(
      <MomentSaveToggle moment={{ id: 'm1', saved_by_me: false, can_save: true }} />,
    )
    const btn = getByRole('button')
    fireEvent.click(btn)
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(vi.mocked(showToast).mock.calls[0][1]).toBe('error')
    expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain('can’t be saved')
    expect(btn.getAttribute('aria-pressed')).toBe('false')
  })

  it('does not open the row it sits in', async () => {
    vi.mocked(api.put).mockResolvedValue({})
    const open = vi.fn()
    const { getByRole } = render(
      <div onClick={open}>
        <MomentSaveToggle moment={{ id: 'm1', can_save: true }} variant="chip" />
      </div>,
    )
    fireEvent.click(getByRole('button'))
    await waitFor(() => expect(api.put).toHaveBeenCalled())
    expect(open).not.toHaveBeenCalled()
  })
})
