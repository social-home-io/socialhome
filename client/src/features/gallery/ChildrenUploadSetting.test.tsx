import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPatch = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
  },
  ApiError: class extends Error {},
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))

beforeEach(() => { vi.resetModules(); apiGet.mockReset(); apiPatch.mockReset(); showToast.mockReset() })
afterEach(() => cleanup())

describe('ChildrenUploadSetting', () => {
  it('an admin turns "Children may add photos" off', async () => {
    apiGet.mockResolvedValue({ children_may_upload: true, may_add: true, can_edit: true })
    apiPatch.mockResolvedValue({ children_may_upload: false, may_add: true, can_edit: true })
    const { ChildrenUploadSetting } = await import('./ChildrenUploadSetting')
    const view = render(<ChildrenUploadSetting />)
    const toggle = await view.findByRole('switch', { name: 'Children may add photos' })
    expect((toggle as HTMLInputElement).checked).toBe(true)
    fireEvent.click(toggle)
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/gallery/settings', { children_may_upload: false }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Children can't add photos for now", 'success'))
  })

  it('shows nothing when the setting could not be read', async () => {
    apiGet.mockRejectedValue(new Error('offline'))
    const { ChildrenUploadSetting } = await import('./ChildrenUploadSetting')
    const view = render(<ChildrenUploadSetting />)
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(view.queryByRole('switch')).toBeNull()
  })

  it('a failed save says why', async () => {
    apiGet.mockResolvedValue({ children_may_upload: false, may_add: true, can_edit: true })
    apiPatch.mockRejectedValue(new Error('nope'))
    const { ChildrenUploadSetting } = await import('./ChildrenUploadSetting')
    const view = render(<ChildrenUploadSetting />)
    fireEvent.click(await view.findByRole('switch'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('nope', 'error'))
  })
})
