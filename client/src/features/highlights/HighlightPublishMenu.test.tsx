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
const confirmDialog = vi.fn()
vi.mock('@/components/confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmDialog(...a) }))

import { ApiError } from '@/api'
import { HighlightPublishMenu, openPublishMenu } from './HighlightPublishMenu'

beforeEach(() => {
  apiPost.mockReset()
  apiGet.mockReset()
  apiDelete.mockReset()
  showToast.mockReset()
  confirmDialog.mockReset()
  // ``GET /api/gfs/connections`` answers a bare array (routes/gfs.py).
  apiGet.mockResolvedValue([{ id: 'gfs-old', display_name: 'Old GFS', status: 'active' }])
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
    apiGet.mockResolvedValue([
      { id: 'gfs-old', display_name: 'Old GFS', status: 'active' },
      { id: 'gfs-new', display_name: 'New GFS', status: 'active' },
    ])
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

describe('HighlightPublishMenu GFS list', () => {
  it('offers the active GFS from the bare-array answer', async () => {
    apiGet.mockResolvedValue([
      { id: 'gfs-a', display_name: 'Alpha GFS', status: 'active' },
      { id: 'gfs-p', display_name: 'Pending GFS', status: 'pending' },
    ])
    openPublishMenu('hl-4', false)
    render(<HighlightPublishMenu />)
    expect(await screen.findByDisplayValue('Alpha GFS')).toBeTruthy()
    expect(screen.queryByText(/not connected to a GFS/)).toBeNull()
    expect(screen.getByRole('button', { name: /create/i })).toHaveProperty('disabled', false)
  })

  it('with no GFS, links to Connections (a real route)', async () => {
    apiGet.mockResolvedValue([])
    openPublishMenu('hl-5', false)
    render(<HighlightPublishMenu />)
    const link = await screen.findByRole('link', { name: 'Connections' })
    expect(link.getAttribute('href')).toMatch(/\/connections$/)
    expect(link.getAttribute('href')).not.toMatch(/settings/)
  })

  it('a load failure is an error with Retry, not "not connected"', async () => {
    apiGet.mockRejectedValue(new Error('offline'))
    openPublishMenu('hl-6', false)
    render(<HighlightPublishMenu />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Couldn't load your GFS connections/)
    expect(screen.queryByText(/not connected to a GFS/)).toBeNull()
    apiGet.mockResolvedValue([{ id: 'gfs-a', display_name: 'Alpha GFS', status: 'active' }])
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByDisplayValue('Alpha GFS')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('HighlightPublishMenu remove all', () => {
  it('is offered for a highlight published earlier', async () => {
    apiGet.mockImplementation(async (path: string) => path === '/api/highlights/hl-9/publish'
      ? { published: true, gfs_id: 'gfs-old', published_at: '2026-10-25T10:00:00Z' }
      : [{ id: 'gfs-old', display_name: 'Old GFS', status: 'active' }])
    openPublishMenu('hl-9', false)
    render(<HighlightPublishMenu />)
    expect(await screen.findByRole('button', { name: /remove all public links/i })).toBeTruthy()
  })

  it('asks first and keeps the links when cancelled', async () => {
    confirmDialog.mockResolvedValue(false)
    openPublishMenu('hl-7', true)
    render(<HighlightPublishMenu />)
    fireEvent.click(await screen.findByRole('button', { name: /remove all public links/i }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('removes them once confirmed', async () => {
    confirmDialog.mockResolvedValue(true)
    apiDelete.mockResolvedValue(undefined)
    openPublishMenu('hl-8', true)
    render(<HighlightPublishMenu />)
    fireEvent.click(await screen.findByRole('button', { name: /remove all public links/i }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/highlights/hl-8/publish'))
  })
})
