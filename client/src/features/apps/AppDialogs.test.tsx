import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { ApiError } from '@/api'

const h = vi.hoisted(() => ({
  updates: { value: [] as any[] },
  updateApp: vi.fn(),
  loadUpdates: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
}))

vi.mock('@/store/apps', async (orig) => {
  const real = await orig<typeof import('@/store/apps')>()
  return {
    ...real,
    updates: h.updates,
    updateApp: h.updateApp,
    loadUpdates: h.loadUpdates,
  }
})
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))

import { UpdateButton } from './AppDialogs'

function newPermissions(list: string): ApiError {
  return new ApiError(409, '/api/apps/chess/update', {
    code: 'APP_NEW_PERMISSIONS', detail: 'x', params: { permissions: list },
  })
}

beforeEach(() => {
  h.updates.value = []
  h.updateApp.mockReset()
  h.loadUpdates.mockClear()
  h.showToast.mockClear()
})

describe('UpdateButton (D7)', () => {
  it('same permissions: one tap updates, no question', async () => {
    h.updateApp.mockResolvedValue(undefined)
    const onDone = vi.fn()
    const { getByRole, queryByRole } = render(
      <UpdateButton appId="chess" name="Chess" onDone={onDone} />,
    )
    fireEvent.click(getByRole('button', { name: 'Update Chess' }))
    await vi.waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(h.updateApp).toHaveBeenCalledWith('chess', [])
    expect(queryByRole('dialog')).toBeNull()
    expect(h.showToast).toHaveBeenCalledWith('Chess is up to date.', 'success')
  })

  it('the server finding new permissions asks for exactly those', async () => {
    // The Needs-you row doesn't know ahead: the server says (it checks
    // the downloaded bundle too).
    h.updateApp.mockRejectedValueOnce(newPermissions('people,federation'))
    h.updateApp.mockResolvedValueOnce(undefined)
    const { getByRole, findByRole } = render(<UpdateButton appId="chess" name="Chess" />)
    fireEvent.click(getByRole('button', { name: 'Update Chess' }))
    const dialog = await findByRole('dialog')
    expect(dialog.textContent).toContain('This version asks for 2 new permissions.')
    expect(dialog.textContent).toContain('Until you accept, nothing changes.')
    expect(dialog.textContent).toContain('Lets it invite people you know')
    expect(dialog.textContent).not.toContain('Saves its own data')
    // What's new is fetched for the dialog.
    expect(h.loadUpdates).toHaveBeenCalled()
    fireEvent.click(getByRole('button', { name: 'Accept and update' }))
    await vi.waitFor(() => expect(h.updateApp).toHaveBeenLastCalledWith('chess', ['people', 'federation']))
  })

  it('Not now changes nothing', async () => {
    h.updates.value = [{
      app_id: 'chess', name: 'Chess', current_version: '2026.10.21', latest_version: '2026.10.28',
      compatible: true, new_permissions: ['federation'], changelog: [],
    }]
    const { getByRole, findByRole, queryByRole } = render(<UpdateButton appId="chess" name="Chess" />)
    fireEvent.click(getByRole('button', { name: 'Review the update for Chess' }))
    const dialog = await findByRole('dialog')
    expect(dialog.textContent).toMatch(/keeps the .*2026 version/)
    fireEvent.click(getByRole('button', { name: 'Not now' }))
    await vi.waitFor(() => expect(queryByRole('dialog')).toBeNull())
    expect(h.updateApp).not.toHaveBeenCalled()
  })

  it('any other failure is a toast, never the dialog', async () => {
    h.updateApp.mockRejectedValueOnce(new ApiError(409, '/x', {
      code: 'APP_NEEDS_NEWER_SOCIAL_HOME', detail: 'x',
    }))
    const { getByRole, queryByRole } = render(<UpdateButton appId="chess" name="Chess" />)
    fireEvent.click(getByRole('button', { name: 'Update Chess' }))
    await vi.waitFor(() => expect(h.showToast).toHaveBeenCalledWith('Needs a newer Social Home', 'error'))
    expect(queryByRole('dialog')).toBeNull()
  })
})
