import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createLiveThrottle } from './liveThrottle'

let hidden = false
function setHidden(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  hidden = false
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('createLiveThrottle', () => {
  it('fires the first trigger at once, then at most once per gap with one trailing call', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000 })
    th.trigger()
    expect(fn).toHaveBeenCalledTimes(1)
    // A burst inside the gap: nothing yet…
    for (let i = 0; i < 10; i++) { th.trigger(); vi.advanceTimersByTime(100) }
    expect(fn).toHaveBeenCalledTimes(1)
    // …then ONE trailing call when the gap is over.
    vi.advanceTimersByTime(1_000)
    expect(fn).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(10_000)
    expect(fn).toHaveBeenCalledTimes(2)
    th.cancel()
  })

  it('counts a load at start when startAt is given', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 5_000, startAt: Date.now() })
    th.trigger()
    expect(fn).not.toHaveBeenCalled()
    vi.advanceTimersByTime(5_000)
    expect(fn).toHaveBeenCalledTimes(1)
    th.cancel()
  })

  it('cancel drops a pending trailing call', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000 })
    th.trigger()
    th.trigger()
    th.cancel()
    vi.advanceTimersByTime(10_000)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('flush runs a pending trailing call now', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000 })
    th.trigger()
    th.trigger()
    th.flush()
    expect(fn).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(10_000)
    expect(fn).toHaveBeenCalledTimes(2)
    // Nothing pending: flush is a no-op.
    th.flush()
    expect(fn).toHaveBeenCalledTimes(2)
    th.cancel()
  })

  it('pauses while hidden and fires once on becoming visible', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000, pauseWhileHidden: true })
    setHidden(true)
    for (let i = 0; i < 5; i++) th.trigger()
    vi.advanceTimersByTime(60_000)
    expect(fn).not.toHaveBeenCalled()
    setHidden(false)
    expect(fn).toHaveBeenCalledTimes(1)
    // Becoming visible again with nothing missed fires nothing.
    setHidden(true)
    setHidden(false)
    expect(fn).toHaveBeenCalledTimes(1)
    th.cancel()
  })

  it('a trailing call due while hidden waits for the page to show', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000, pauseWhileHidden: true })
    th.trigger()
    th.trigger()
    setHidden(true)
    vi.advanceTimersByTime(5_000)
    expect(fn).toHaveBeenCalledTimes(1)
    setHidden(false)
    expect(fn).toHaveBeenCalledTimes(2)
    th.cancel()
  })

  it('without pauseWhileHidden it fires while hidden', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000 })
    setHidden(true)
    th.trigger()
    expect(fn).toHaveBeenCalledTimes(1)
    th.cancel()
  })

  it('cancel stops listening for visibility', () => {
    const fn = vi.fn()
    const th = createLiveThrottle(fn, { minGapMs: 2_000, pauseWhileHidden: true })
    setHidden(true)
    th.trigger()
    th.cancel()
    setHidden(false)
    expect(fn).not.toHaveBeenCalled()
  })
})
