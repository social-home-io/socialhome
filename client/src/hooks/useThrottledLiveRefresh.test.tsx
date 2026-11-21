import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/preact'

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
vi.mock('@/api', () => ({ api: { get: vi.fn() } }))

import { useThrottledLiveRefresh, LIVE_REFRESH_MIN_GAP_MS } from './useThrottledLiveRefresh'

function emit(type: string, data: Record<string, unknown> = {}) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

let hidden = false
function setHidden(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
}

function Probe({ refresh }: { refresh: () => void }) {
  useThrottledLiveRefresh(refresh, ['dm.message', 'task.updated'])
  return null
}

beforeEach(() => {
  handlers.clear()
  hidden = false
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('useThrottledLiveRefresh', () => {
  it('refreshes at most once per 10 s, with one trailing call for a burst', () => {
    expect(LIVE_REFRESH_MIN_GAP_MS).toBe(10_000)
    const refresh = vi.fn()
    render(<Probe refresh={refresh} />)
    // The page just loaded: a burst of 30 frames within the next seconds…
    for (let i = 0; i < 30; i++) {
      emit('task.updated')
      vi.advanceTimersByTime(200)
    }
    expect(refresh).not.toHaveBeenCalled()
    // …lands as ONE trailing refresh, 10 s after the mount's load.
    vi.advanceTimersByTime(4_000)
    expect(refresh).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(60_000)
    expect(refresh).toHaveBeenCalledTimes(1)
    // A frame long after the last refresh refreshes right away.
    emit('task.updated')
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('ignores space and household chat frames', () => {
    const refresh = vi.fn()
    render(<Probe refresh={refresh} />)
    vi.advanceTimersByTime(20_000)
    for (let i = 0; i < 50; i++) {
      emit('dm.message', { system_scope: 'space', space_id: 'sp-1' })
      emit('dm.message', { system_scope: 'household' })
    }
    vi.advanceTimersByTime(20_000)
    expect(refresh).not.toHaveBeenCalled()
    // A real DM still counts.
    emit('dm.message', { conversation_id: 'c1' })
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('pauses while the page is hidden and refreshes once on return', () => {
    const refresh = vi.fn()
    render(<Probe refresh={refresh} />)
    vi.advanceTimersByTime(20_000)
    setHidden(true)
    for (let i = 0; i < 20; i++) {
      emit('task.updated')
      vi.advanceTimersByTime(15_000)
    }
    expect(refresh).not.toHaveBeenCalled()
    setHidden(false)
    expect(refresh).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(60_000)
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('a trailing call due while hidden waits for the page to show', () => {
    const refresh = vi.fn()
    render(<Probe refresh={refresh} />)
    emit('task.updated')  // trailing call scheduled for t = 10 s
    setHidden(true)
    vi.advanceTimersByTime(30_000)
    expect(refresh).not.toHaveBeenCalled()
    setHidden(false)
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('becoming visible with nothing missed does not refresh', () => {
    const refresh = vi.fn()
    render(<Probe refresh={refresh} />)
    setHidden(true)
    setHidden(false)
    vi.advanceTimersByTime(30_000)
    expect(refresh).not.toHaveBeenCalled()
  })

  it('stops listening on unmount', () => {
    const refresh = vi.fn()
    const { unmount } = render(<Probe refresh={refresh} />)
    emit('task.updated')
    unmount()
    vi.advanceTimersByTime(30_000)
    emit('task.updated')
    setHidden(true)
    setHidden(false)
    expect(refresh).not.toHaveBeenCalled()
  })

  it('takes a shorter gap and a frame filter', () => {
    const refresh = vi.fn()
    function Filtered() {
      useThrottledLiveRefresh(refresh, ['poll.voted'], {
        minGapMs: 2_000,
        accept: (_type, data) => data.post_id === 'p1',
      })
      return null
    }
    render(<Filtered />)
    vi.advanceTimersByTime(2_000)
    emit('poll.voted', { post_id: 'other' })
    expect(refresh).not.toHaveBeenCalled()
    emit('poll.voted', { post_id: 'p1' })
    expect(refresh).toHaveBeenCalledTimes(1)
    emit('poll.voted', { post_id: 'p1' })
    emit('poll.voted', { post_id: 'p1' })
    vi.advanceTimersByTime(1_999)
    expect(refresh).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(refresh).toHaveBeenCalledTimes(2)
  })

  it('reads the latest filter, so a changed target needs no resubscribe', () => {
    const refresh = vi.fn()
    function Target({ id }: { id: string }) {
      useThrottledLiveRefresh(refresh, ['poll.voted'], {
        minGapMs: 0,
        accept: (_type, data) => data.post_id === id,
      })
      return null
    }
    const { rerender } = render(<Target id="a" />)
    rerender(<Target id="b" />)
    emit('poll.voted', { post_id: 'a' })
    expect(refresh).not.toHaveBeenCalled()
    emit('poll.voted', { post_id: 'b' })
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
