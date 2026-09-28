import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { signal } from '@preact/signals'
import type { User } from '@/types'

const baseUser: User = {
  user_id: 'u1', username: 'anna', display_name: 'Anna', is_admin: false,
  picture_url: null, picture_hash: null, bio: null, is_new_member: false,
  status: { emoji: null, text: null, expires_at: null },
}

describe('StatusEditor', () => {
  const currentUser = signal<User | null>(null)
  const toast = vi.fn()

  beforeEach(() => {
    vi.resetModules()
    toast.mockReset()
    currentUser.value = { ...baseUser }
    vi.doMock('@/store/auth', () => ({ currentUser }))
    vi.doMock('./Toast', () => ({ showToast: toast }))
  })

  async function mount(patch: ReturnType<typeof vi.fn>) {
    vi.doMock('@/api', async () => {
      const real = await vi.importActual<typeof import('@/api')>('@/api')
      return { ApiError: real.ApiError, api: { patch } }
    })
    const { StatusEditor } = await import('./StatusEditor')
    const onSave = vi.fn()
    const view = render(<StatusEditor onSave={onSave} />)
    return { view, onSave }
  }

  it('sends emoji, text and the chosen clear-after in one PATCH', async () => {
    const saved = { ...baseUser, status: { emoji: '🌴', text: 'On leave', expires_at: '2099-01-01T00:00:00+00:00' } }
    const patch = vi.fn(async () => saved)
    const { view, onSave } = await mount(patch)
    fireEvent.input(view.getByLabelText('Status emoji'), { target: { value: '🌴' } })
    fireEvent.input(view.getByLabelText('Status text'), { target: { value: ' On leave ' } })
    fireEvent.click(view.getByRole('radio', { name: '1 hour' }))
    fireEvent.click(view.getByRole('button', { name: 'Set status' }))
    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(patch).toHaveBeenCalledWith('/api/me', {
      status_emoji: '🌴', status_text: 'On leave', status_clear_after: '1h',
    })
    // The page shows the new status right away, from the response.
    expect(currentUser.value?.status?.text).toBe('On leave')
    expect(toast).toHaveBeenCalledWith('Status updated', 'success')
  })

  it('pre-fills the current status and keeps its deadline by default', async () => {
    currentUser.value = {
      ...baseUser,
      status: { emoji: '🎧', text: 'Focus', expires_at: '2099-01-01T15:30:00+00:00' },
    }
    const patch = vi.fn(async () => currentUser.value)
    const { view } = await mount(patch)
    expect((view.getByLabelText('Status text') as HTMLInputElement).value).toBe('Focus')
    const keep = view.getByRole('radio', { name: /^Until / })
    expect(keep.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(view.getByRole('button', { name: 'Set status' }))
    await waitFor(() => expect(patch).toHaveBeenCalled())
    expect(patch).toHaveBeenCalledWith('/api/me', {
      status_emoji: '🎧', status_text: 'Focus',
      status_clear_after: '2099-01-01T15:30:00+00:00',
    })
  })

  it('shows the server validation message and stays open on 422', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const patch = vi.fn(async () => {
      throw new ApiError(422, '/api/me', {
        code: 'UNPROCESSABLE', detail: 'Pick a single emoji for your status.',
      })
    })
    const { view, onSave } = await mount(patch)
    fireEvent.input(view.getByLabelText('Status emoji'), { target: { value: 'ok' } })
    fireEvent.click(view.getByRole('button', { name: 'Set status' }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Pick a single emoji for your status.', 'error'))
    expect(onSave).not.toHaveBeenCalled()
    expect(view.getByLabelText('Status emoji')).toBeTruthy()
  })

  it('disables Set status while both fields are empty', async () => {
    const { view } = await mount(vi.fn())
    expect((view.getByRole('button', { name: 'Set status' }) as HTMLButtonElement).disabled).toBe(true)
    expect(view.queryByRole('button', { name: 'Clear status' })).toBeNull()
  })

  it('Clear status sends nulls', async () => {
    currentUser.value = { ...baseUser, status: { emoji: '🎧', text: 'Focus', expires_at: null } }
    const patch = vi.fn(async () => ({ ...baseUser }))
    const { view } = await mount(patch)
    fireEvent.click(view.getByRole('button', { name: 'Clear status' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/me', {
      status_emoji: null, status_text: null,
    }))
    expect(toast).toHaveBeenCalledWith('Status cleared', 'info')
  })
})
