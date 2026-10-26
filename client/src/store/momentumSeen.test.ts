import { describe, it, expect, beforeEach, vi } from 'vitest'
import { momentumSeenAt, markMomentumSeen, noteRoute, _resetRouteForTests } from './momentumSeen'

describe('momentumSeen', () => {
  beforeEach(() => {
    window.localStorage.clear()
    _resetRouteForTests()
  })

  it('knows nothing until Momentum was opened', () => {
    expect(momentumSeenAt('u1')).toBeNull()
  })

  it('remembers when you last had Momentum open, per person', () => {
    markMomentumSeen('u1', 1000)
    expect(momentumSeenAt('u1')).toBe(1000)
    expect(momentumSeenAt('u2')).toBeNull()
  })

  it('marks Momentum seen on opening it and on leaving it', () => {
    vi.useFakeTimers()
    vi.setSystemTime(5000)
    noteRoute('/feed', 'u1')
    expect(momentumSeenAt('u1')).toBeNull()
    noteRoute('/momentum?tab=yours', 'u1')
    expect(momentumSeenAt('u1')).toBe(5000)
    // Moments that arrived while the page was open were seen too.
    vi.setSystemTime(9000)
    noteRoute('/highlights', 'u1')
    expect(momentumSeenAt('u1')).toBe(9000)
    vi.setSystemTime(12000)
    noteRoute('/feed', 'u1')
    expect(momentumSeenAt('u1')).toBe(9000)
    vi.useRealTimers()
  })

  it('never throws when storage is unavailable', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    const spySet = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    expect(() => markMomentumSeen('u1', 1)).not.toThrow()
    expect(momentumSeenAt('u1')).toBeNull()
    spy.mockRestore()
    spySet.mockRestore()
  })
})
