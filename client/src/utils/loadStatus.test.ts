import { describe, it, expect } from 'vitest'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded } from './loadStatus'

describe('loadStatus', () => {
  it('a first load that fails is a failure, nothing to show', () => {
    const s = loadFailed(INITIAL_LOAD)
    expect(s.failed).toBe(true)
    expect(s.staleSince).toBeNull()
  })

  it('a refresh that fails keeps what loaded and says since when', () => {
    const ok = loadSucceeded(INITIAL_LOAD, 1000)
    expect(ok).toEqual({ failed: false, loadedAt: 1000, staleSince: null })
    const stale = loadFailed(ok)
    expect(stale.failed).toBe(false)
    expect(stale.staleSince).toBe(1000)
    // The next success clears it.
    expect(loadSucceeded(stale, 5000)).toEqual({ failed: false, loadedAt: 5000, staleSince: null })
  })

  it('clockTime is the local clock of a load', () => {
    expect(clockTime(new Date(2026, 9, 25, 13, 2).getTime())).toMatch(/^(13[:.]02|01:02\sPM)$/)
  })
})
