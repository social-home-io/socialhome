import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/preact'

const apiPost = vi.fn()
const apiGet = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', async () => {
  const actual = await vi.importActual<typeof import('@/api')>('@/api')
  return {
    ApiError: actual.ApiError,
    api: {
      get: (...a: unknown[]) => apiGet(...a),
      post: (...a: unknown[]) => apiPost(...a),
      delete: (...a: unknown[]) => apiDelete(...a),
    },
  }
})
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

import { ApiError } from '@/api'
import { HighlightPublishMenu, openPublishMenu } from './HighlightPublishMenu'

beforeEach(() => {
  apiPost.mockReset()
  apiGet.mockReset()
  showToast.mockReset()
  apiGet.mockResolvedValue({
    connections: [{ id: 'gfs-old', display_name: 'Old GFS', status: 'active' }],
  })
})

describe('HighlightPublishMenu on a GFS too old for encrypted links', () => {
  it('says so next to the GFS and keeps the create button off for it', async () => {
    apiPost.mockRejectedValue(
      new ApiError(409, '/api/highlights/hl-1/publish', {
        code: 'GFS_TOO_OLD',
        detail: 'too old',
      }),
    )
    openPublishMenu('hl-1', false)
    render(<HighlightPublishMenu />)
    const create = await screen.findByRole('button', { name: /create/i })
    fireEvent.click(create)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Old GFS/)
    expect(alert.textContent).toMatch(/update/)
    await waitFor(() => expect(create).toHaveProperty('disabled', true))
    // A lasting state, said inline — not a passing error toast.
    expect(showToast).not.toHaveBeenCalled()
  })

  it('another failure is still a toast', async () => {
    apiPost.mockRejectedValue(new Error('offline'))
    openPublishMenu('hl-2', false)
    render(<HighlightPublishMenu />)
    fireEvent.click(await screen.findByRole('button', { name: /create/i }))
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
