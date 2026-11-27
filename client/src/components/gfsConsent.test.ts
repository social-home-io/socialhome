import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

import { askGfsConsent, RECHECK_TRIES } from './gfsConsent'

beforeEach(() => {
  get.mockReset(); post.mockReset(); confirm.mockReset(); toast.mockReset()
})
afterEach(() => { vi.useRealTimers() })

const NEEDS = { gfs_space: true, connected: false, may_connect: true }

describe('askGfsConsent', () => {
  it('asks nothing for a mesh space or a connected household', async () => {
    get.mockResolvedValueOnce({ gfs_space: false, connected: true, may_connect: true })
    expect(await askGfsConsent('sp', 'Choir')).toBe(true)
    get.mockResolvedValueOnce({ gfs_space: true, connected: true, may_connect: false })
    expect(await askGfsConsent('sp', 'Choir')).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
    // The server decides what the space is: no space type is sent.
    expect(get.mock.calls[0][0]).toBe('/api/public_spaces/sp/join-preflight')
  })

  it('an admin connects, checks again and joins on yes; sends nothing on cancel', async () => {
    get.mockResolvedValueOnce(NEEDS)
    confirm.mockResolvedValueOnce(false)
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(post).not.toHaveBeenCalled()
    get.mockResolvedValueOnce(NEEDS)
    get.mockResolvedValueOnce({ ...NEEDS, connected: true })
    confirm.mockResolvedValueOnce(true)
    post.mockResolvedValueOnce({})
    expect(await askGfsConsent('sp', 'Choir')).toBe(true)
    expect(post).toHaveBeenCalledWith('/api/gfs/connections/default')
  })

  it('a connected GFS that does not list the space says so and does not join', async () => {
    vi.useFakeTimers()
    get.mockResolvedValue(NEEDS)
    confirm.mockResolvedValueOnce(true).mockResolvedValueOnce(true)
    post.mockResolvedValueOnce({})
    const out = askGfsConsent('sp', 'Choir')
    await vi.runAllTimersAsync()
    expect(await out).toBe(false)
    expect(get).toHaveBeenCalledTimes(1 + RECHECK_TRIES)
    expect(confirm.mock.calls[1][1]).toMatchObject({ notice: true })
  })

  it('a failed connect says why and does not join', async () => {
    get.mockResolvedValue(NEEDS)
    confirm.mockResolvedValueOnce(true)
    post.mockRejectedValueOnce(new Error('down'))
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(toast).toHaveBeenCalledWith(expect.any(String), 'error')
  })

  it('anyone else is told to ask an admin, and nothing is sent', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: false })
    confirm.mockResolvedValueOnce(true)
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(post).not.toHaveBeenCalled()
    // A notice: one way out, no Cancel next to OK.
    expect(confirm.mock.calls[0][1]).toMatchObject({ notice: true })
  })

  it('fails closed: a failed or odd preflight blocks the join and says so', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    get.mockResolvedValueOnce(undefined)
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(toast).toHaveBeenCalledTimes(2)
    expect(toast.mock.calls[0][1]).toBe('error')
    expect(post).not.toHaveBeenCalled()
  })
})
