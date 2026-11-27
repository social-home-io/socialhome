import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const post = vi.fn()
const confirm = vi.fn()
const toast = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
  ApiError: class ApiError extends Error { status = 0 },
}))
vi.mock('./confirm', () => ({ confirmDialog: (...a: unknown[]) => confirm(...a) }))
vi.mock('./Toast', () => ({ showToast: (...a: unknown[]) => toast(...a) }))

import { askGfsConsent } from './gfsConsent'

beforeEach(() => {
  get.mockReset(); post.mockReset(); confirm.mockReset(); toast.mockReset()
})

describe('askGfsConsent', () => {
  it('asks nothing for a mesh space or a connected household', async () => {
    get.mockResolvedValueOnce({ gfs_space: false, connected: true, may_connect: true })
    expect(await askGfsConsent('sp', 'public', 'Choir')).toBe(true)
    get.mockResolvedValueOnce({ gfs_space: true, connected: true, may_connect: false })
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
    expect(get.mock.calls[0][0]).toBe('/api/public_spaces/sp/join-preflight?space_type=public')
  })

  it('an admin connects and joins on yes, sends nothing on cancel', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: true })
    confirm.mockResolvedValueOnce(false)
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(false)
    expect(post).not.toHaveBeenCalled()
    confirm.mockResolvedValueOnce(true)
    post.mockResolvedValueOnce({})
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(true)
    expect(post).toHaveBeenCalledWith('/api/gfs/connections/default')
  })

  it('a failed connect says why and does not join', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: true })
    confirm.mockResolvedValueOnce(true)
    post.mockRejectedValueOnce(new Error('down'))
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(false)
    expect(toast).toHaveBeenCalledWith(expect.any(String), 'error')
  })

  it('anyone else is told to ask an admin, and nothing is sent', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: false })
    confirm.mockResolvedValueOnce(true)
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('a failed preflight never blocks the join', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    expect(await askGfsConsent('sp', 'global', 'Choir')).toBe(true)
  })
})
