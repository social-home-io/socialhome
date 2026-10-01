import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ApiError } from '@/api'
import { toasts } from '@/components/Toast'
import {
  undoableDelete, pendingDeletes, flushPendingDeletes, resetPendingDeletes,
} from './undoableDelete'

function undoButtonClick() {
  const row = toasts.value[toasts.value.length - 1]
  row.action!.onClick()
  // The container drops the row on click; mimic that so later
  // assertions see the stack the user would.
  toasts.value = toasts.value.filter(t => t.id !== row.id)
}

describe('undoableDelete', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    toasts.value = []
    resetPendingDeletes()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('hides immediately, marks the ids pending and shows an Undo toast', () => {
    const hide = vi.fn()
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a', 'b'], hide, commit, message: 'Deleted Milk' })
    expect(hide).toHaveBeenCalledTimes(1)
    expect(commit).not.toHaveBeenCalled()
    expect(pendingDeletes.value.has('a')).toBe(true)
    expect(pendingDeletes.value.has('b')).toBe(true)
    expect(toasts.value).toHaveLength(1)
    expect(toasts.value[0].message).toBe('Deleted Milk')
    expect(toasts.value[0].action?.label).toBe('Undo')
  })

  it('Undo restores and never commits', async () => {
    const restore = vi.fn()
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], restore, commit, message: 'Deleted' })
    undoButtonClick()
    expect(restore).toHaveBeenCalledTimes(1)
    expect(pendingDeletes.value.has('a')).toBe(false)
    await vi.advanceTimersByTimeAsync(30000)
    expect(commit).not.toHaveBeenCalled()
  })

  it('commits once the toast expires, then clears the pending mark', async () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    const restore = vi.fn()
    undoableDelete({ ids: ['a'], restore, commit, message: 'Deleted' })
    await vi.advanceTimersByTimeAsync(9000)
    expect(commit).toHaveBeenCalledTimes(1)
    expect(restore).not.toHaveBeenCalled()
    expect(pendingDeletes.value.has('a')).toBe(false)
  })

  it('treats a 404 on commit as success (already gone)', async () => {
    const commit = vi.fn().mockRejectedValue(new ApiError(404, '/api/x', null))
    const restore = vi.fn()
    undoableDelete({ ids: ['a'], restore, commit, message: 'Deleted' })
    await vi.advanceTimersByTimeAsync(9000)
    expect(restore).not.toHaveBeenCalled()
    expect(toasts.value.some(t => t.type === 'error')).toBe(false)
  })

  it('restores and reports when the commit fails', async () => {
    const commit = vi.fn().mockRejectedValue(new ApiError(500, '/api/x', null))
    const restore = vi.fn()
    undoableDelete({ ids: ['a'], restore, commit, message: 'Deleted' })
    await vi.advanceTimersByTimeAsync(9000)
    expect(restore).toHaveBeenCalledTimes(1)
    expect(pendingDeletes.value.has('a')).toBe(false)
    expect(toasts.value.some(t => t.type === 'error')).toBe(true)
  })

  it('commits when the toast is evicted by newer toasts', async () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted 1' })
    undoableDelete({ ids: ['b'], commit, message: 'Deleted 2' })
    undoableDelete({ ids: ['c'], commit, message: 'Deleted 3' })
    undoableDelete({ ids: ['d'], commit, message: 'Deleted 4' })
    await vi.advanceTimersByTimeAsync(0)
    expect(commit).toHaveBeenCalledTimes(1)
  })

  it('flushes pending commits on pagehide, and never commits twice', async () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted' })
    window.dispatchEvent(new Event('pagehide'))
    expect(commit).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30000)
    expect(commit).toHaveBeenCalledTimes(1)
  })

  it('flushPendingDeletes ignores entries that were undone', () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted' })
    undoButtonClick()
    flushPendingDeletes()
    expect(commit).not.toHaveBeenCalled()
  })

  it('the pagehide flush commits with keepalive and dismisses the toast', () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted' })
    expect(toasts.value).toHaveLength(1)
    window.dispatchEvent(new Event('pagehide'))
    expect(commit).toHaveBeenCalledWith({ keepalive: true })
    expect(toasts.value).toHaveLength(0)
  })

  it('the normal expiry commit does not ask for keepalive', async () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted' })
    await vi.advanceTimersByTimeAsync(9000)
    expect(commit).toHaveBeenCalledWith({})
  })

  it('flushes when the tab is hidden', () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted' })
    const desc = Object.getOwnPropertyDescriptor(Document.prototype, 'visibilityState')
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    try {
      document.dispatchEvent(new Event('visibilitychange'))
    } finally {
      if (desc) Object.defineProperty(document, 'visibilityState', desc)
      else delete (document as unknown as Record<string, unknown>).visibilityState
    }
    expect(commit).toHaveBeenCalledWith({ keepalive: true })
  })

  it('flushes every entry at once', () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'D1' })
    undoableDelete({ ids: ['b'], commit, message: 'D2' })
    flushPendingDeletes()
    expect(commit).toHaveBeenCalledTimes(2)
  })

  it('overlapping entries for one id cannot un-hide each other', () => {
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], commit, message: 'Deleted a' })
    undoableDelete({ ids: ['a', 'b'], commit, message: 'Cleared 2' })
    // Undo the first (single) delete: "a" is still hidden by the clear.
    toasts.value.find(x => x.message === 'Deleted a')!.action!.onClick()
    expect(pendingDeletes.value.has('a')).toBe(true)
    toasts.value.find(x => x.message === 'Cleared 2')!.action!.onClick()
    expect(pendingDeletes.value.has('a')).toBe(false)
    expect(pendingDeletes.value.has('b')).toBe(false)
  })

  it('a settled entry ignores a late Undo — no restore, no toast', async () => {
    const restore = vi.fn()
    const commit = vi.fn().mockResolvedValue(undefined)
    undoableDelete({ ids: ['a'], restore, commit, message: 'Deleted' })
    const action = toasts.value[0].action!
    await vi.advanceTimersByTimeAsync(9000)
    toasts.value = []
    action.onClick()
    expect(restore).not.toHaveBeenCalled()
    expect(toasts.value).toHaveLength(0)
  })

  it('onUndone runs after Undo only', async () => {
    const onUndone = vi.fn()
    undoableDelete({ ids: ['a'], commit: vi.fn().mockRejectedValue(new Error('x')), message: 'D', onUndone })
    await vi.advanceTimersByTimeAsync(9000)
    expect(onUndone).not.toHaveBeenCalled()
    undoableDelete({ ids: ['b'], commit: vi.fn(), message: 'E', onUndone })
    toasts.value.find(x => x.message === 'E')!.action!.onClick()
    expect(onUndone).toHaveBeenCalledTimes(1)
  })
})
