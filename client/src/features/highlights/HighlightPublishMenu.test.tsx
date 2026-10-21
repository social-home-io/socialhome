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

const tooOld = () => new ApiError(409, '/api/highlights/hl-1/publish', {
  code: 'GFS_TOO_OLD',
  detail: 'too old',
})

describe('HighlightPublishMenu on a GFS too old for encrypted links', () => {
  it('with the only GFS: says to try again later, keeps focus in the dialog, offers Try again', async () => {
    apiPost.mockRejectedValue(tooOld())
    openPublishMenu('hl-1', false)
    render(<HighlightPublishMenu />)
    fireEvent.click(await screen.findByRole('button', { name: /create/i }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Old GFS/)
    expect(alert.textContent).toMatch(/update/)
    // There is no other GFS to pick: the copy must not ask for one.
    expect(alert.textContent).not.toMatch(/another GFS/i)
    // Focus lands on the message, not on <body> behind the dialog.
    await waitFor(() => expect(document.activeElement).toBe(alert))
    // A lasting state, said inline — not a passing error toast.
    expect(showToast).not.toHaveBeenCalled()
    // The next action is right there: try again once the GFS is updated.
    const retry = screen.getByRole('button', { name: /try again/i })
    expect(retry).toHaveProperty('disabled', false)
    apiPost.mockResolvedValue({ url: 'https://gfs.example/h/x', token: 'x', label: null })
    fireEvent.click(retry)
    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    await screen.findByText('https://gfs.example/h/x')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('with several GFSs: asks for another one and keeps create off for this one', async () => {
    apiGet.mockResolvedValue({
      connections: [
        { id: 'gfs-old', display_name: 'Old GFS', status: 'active' },
        { id: 'gfs-new', display_name: 'New GFS', status: 'active' },
      ],
    })
    apiPost.mockRejectedValue(tooOld())
    openPublishMenu('hl-3', false)
    render(<HighlightPublishMenu />)
    const select = await screen.findByRole('combobox')
    fireEvent.change(select, { target: { value: 'gfs-old' } })
    const create = screen.getByRole('button', { name: /create/i })
    fireEvent.click(create)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/another GFS/i)
    await waitFor(() => expect(create).toHaveProperty('disabled', true))
    // A disabled button never says "Try again".
    expect(create.textContent).toMatch(/create/i)
    // Focus goes to the picker — the way out.
    await waitFor(() => expect(document.activeElement).toBe(select))
    fireEvent.change(select, { target: { value: 'gfs-new' } })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(create).toHaveProperty('disabled', false)
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
