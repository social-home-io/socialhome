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
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

const NEEDS = { gfs_space: true, connected: false, may_connect: true, default_gfs: 'gfs.example' }

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

  it('names the GFS it connects and words the dialog for a follow', async () => {
    get.mockResolvedValueOnce(NEEDS)
    confirm.mockResolvedValueOnce(false)
    expect(await askGfsConsent('sp', 'Choir', 'follow')).toBe(false)
    const [body, opts] = confirm.mock.calls[0]
    expect(body).toContain('gfs.example')
    expect(opts.confirmLabel).not.toBe(confirm.mock.calls[0][1].cancelLabel)
    get.mockResolvedValueOnce(NEEDS)
    confirm.mockResolvedValueOnce(false)
    await askGfsConsent('sp', 'Choir', 'join')
    // Follow and join say different things.
    expect(confirm.mock.calls[1][1].confirmLabel).not.toBe(opts.confirmLabel)
    expect(post).not.toHaveBeenCalled()
  })

  it('with no default GFS to connect, an admin is sent to Connections; nothing is sent', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    get.mockResolvedValueOnce({ ...NEEDS, default_gfs: null })
    confirm.mockResolvedValueOnce(true)
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(post).not.toHaveBeenCalled()
    expect(assign).toHaveBeenCalledWith(expect.stringMatching(/\/connections$/))
  })

  it('a failed connect says why and does not join', async () => {
    get.mockResolvedValue(NEEDS)
    confirm.mockResolvedValueOnce(true)
    post.mockRejectedValueOnce(new Error('down'))
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(toast).toHaveBeenLastCalledWith(expect.any(String), 'error')
  })

  it('anyone else asks the household admins with one tap; Cancel asks nobody', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: false })
    confirm.mockResolvedValueOnce(false)
    expect(await askGfsConsent('sp', 'Choir', 'follow')).toBe(false)
    expect(post).not.toHaveBeenCalled()
    expect(confirm.mock.calls[0][1]).toMatchObject({
      confirmLabel: 'Ask an admin',
      cancelLabel: 'Cancel',
    })
    confirm.mockResolvedValueOnce(true)
    post.mockResolvedValueOnce({ asked: 2 })
    // Asking never joins: the admins connect first.
    expect(await askGfsConsent('sp', 'Choir', 'follow')).toBe(false)
    expect(post).toHaveBeenCalledWith('/api/public_spaces/sp/ask-admin', { action: 'follow' })
    expect(toast).toHaveBeenLastCalledWith(expect.stringContaining('admins were asked'), 'success')
  })

  it('says so when nobody could be asked or the ask failed', async () => {
    get.mockResolvedValue({ gfs_space: true, connected: false, may_connect: false })
    confirm.mockResolvedValue(true)
    post.mockResolvedValueOnce({ asked: 0 })
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(toast).toHaveBeenLastCalledWith(expect.stringContaining('no other admin'), 'info')
    post.mockRejectedValueOnce(new Error('offline'))
    expect(await askGfsConsent('sp', 'Choir')).toBe(false)
    expect(toast).toHaveBeenLastCalledWith(expect.stringContaining("Couldn't ask"), 'error')
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
