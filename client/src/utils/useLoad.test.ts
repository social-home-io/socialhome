import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/preact'

vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  return { connectionState: signal('open') }
})

import { connectionState } from '@/ws'

import { useLoad } from './useLoad'

describe('useLoad', () => {
  beforeEach(() => {
    connectionState.value = 'open'
  })

  it('starts loading, then becomes ready when the loader resolves', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    const { result } = renderHook(() => useLoad(loader))
    expect(result.current.state).toBe('loading')
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('becomes error when the loader rejects, and retry() recovers', async () => {
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(undefined)
    const { result } = renderHook(() => useLoad(loader))
    await waitFor(() => expect(result.current.state).toBe('error'))
    act(() => result.current.retry())
    expect(result.current.state).toBe('loading')
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('shows cached data at once and revalidates in the background', async () => {
    let resolve!: () => void
    const loader = vi.fn(() => new Promise<void>(r => { resolve = r }))
    const { result } = renderHook(() => useLoad(loader, { cached: true }))
    expect(result.current.state).toBe('ready')
    expect(loader).toHaveBeenCalledTimes(1)
    await act(async () => { resolve() })
    expect(result.current.state).toBe('ready')
  })

  it('keeps cached data on screen when the background revalidate fails', async () => {
    const loader = vi.fn().mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useLoad(loader, { cached: true }))
    await act(async () => { await Promise.resolve() })
    expect(result.current.state).toBe('ready')
  })

  it('revalidates when the WebSocket comes back after a disconnect', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    renderHook(() => useLoad(loader))
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    act(() => { connectionState.value = 'reconnecting' })
    expect(loader).toHaveBeenCalledTimes(1)
    act(() => { connectionState.value = 'open' })
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2))
  })

  it('does not reload on the very first connect', async () => {
    connectionState.value = 'connecting'
    const loader = vi.fn().mockResolvedValue(undefined)
    renderHook(() => useLoad(loader))
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    act(() => { connectionState.value = 'open' })
    await act(async () => { await Promise.resolve() })
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('can opt out of reconnect revalidation', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    renderHook(() => useLoad(loader, { revalidateOnReconnect: false }))
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { connectionState.value = 'open' })
    await act(async () => { await Promise.resolve() })
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('ignores a slow earlier result that lands after a newer one', async () => {
    const resolvers: Array<(v?: unknown) => void> = []
    const rejecters: Array<(e: unknown) => void> = []
    const loader = vi.fn(() => new Promise((res, rej) => {
      resolvers.push(res); rejecters.push(rej)
    }))
    const { result } = renderHook(() => useLoad(loader))
    act(() => result.current.retry())
    await act(async () => { resolvers[1]() })
    expect(result.current.state).toBe('ready')
    await act(async () => { rejecters[0](new Error('late')) })
    expect(result.current.state).toBe('ready')
  })

  it('re-runs and resets to loading when the key changes', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    const { result, rerender } = renderHook(
      ({ k }: { k: string }) => useLoad(loader, { key: k }),
      { initialProps: { k: 'a' } },
    )
    await waitFor(() => expect(result.current.state).toBe('ready'))
    rerender({ k: 'b' })
    expect(result.current.state).toBe('loading')
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('reads cached per key: a cached new key shows ready at once', async () => {
    let resolve!: () => void
    const loader = vi.fn(() => new Promise<void>(r => { resolve = r }))
    const { result, rerender } = renderHook(
      ({ k, c }: { k: string; c: boolean }) => useLoad(loader, { key: k, cached: c }),
      { initialProps: { k: 'a', c: false } },
    )
    expect(result.current.state).toBe('loading')
    rerender({ k: 'b', c: true })
    expect(result.current.state).toBe('ready')
    await act(async () => { resolve() })
  })

  it('marks cached data stale when the background revalidate fails, clears it on success', async () => {
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(undefined)
    const { result } = renderHook(() => useLoad(loader, { cached: true }))
    await waitFor(() => expect(result.current.stale).toBe(true))
    expect(result.current.state).toBe('ready')
    act(() => result.current.retry())
    await waitFor(() => expect(result.current.stale).toBe(false))
  })

  it('passes the reason to the loader (reconnect revalidations can force)', async () => {
    const loader = vi.fn().mockResolvedValue(undefined)
    renderHook(() => useLoad(loader))
    await waitFor(() => expect(loader).toHaveBeenCalledWith({ reason: 'initial' }))
    act(() => { connectionState.value = 'reconnecting' })
    act(() => { connectionState.value = 'open' })
    await waitFor(() => expect(loader).toHaveBeenLastCalledWith({ reason: 'reconnect' }))
  })

  it('calls onRecovered once a Retry turns an error into data', async () => {
    const onRecovered = vi.fn()
    const loader = vi.fn()
      .mockRejectedValueOnce(new Error('x'))
      .mockResolvedValueOnce(undefined)
    const { result } = renderHook(() => useLoad(loader, { onRecovered }))
    await waitFor(() => expect(result.current.state).toBe('error'))
    expect(onRecovered).not.toHaveBeenCalled()
    act(() => result.current.retry())
    await waitFor(() => expect(onRecovered).toHaveBeenCalledTimes(1))
  })

  it('sets no state after unmount mid-flight', async () => {
    let resolve!: () => void
    const loader = vi.fn(() => new Promise<void>(r => { resolve = r }))
    const onRecovered = vi.fn()
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { result, unmount } = renderHook(() => useLoad(loader, { onRecovered }))
    const before = result.current
    unmount()
    await act(async () => { resolve() })
    expect(result.current).toBe(before)
    expect(result.current.state).toBe('loading')
    expect(errSpy).not.toHaveBeenCalled()
    errSpy.mockRestore()
  })
})
