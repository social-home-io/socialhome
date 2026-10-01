/**
 * useLoad — one loading / error / retry lifecycle for list pages.
 *
 * ``state`` is ``'loading'`` until the first successful load,
 * ``'error'`` when that load failed (render ``LoadErrorState`` with
 * ``retry``), and ``'ready'`` afterwards. When the store already holds
 * data (``cached``) the page renders it straight away and the loader
 * revalidates in the background; a failed background revalidate keeps
 * the cached data on screen and sets ``stale`` (cleared by the next
 * success) instead of swapping it for an error.
 *
 * ``key`` scopes the lifecycle (e.g. a list id or a space id): when it
 * changes the hook starts over — ``cached`` is read again *for the new
 * key*, so pass the cache state of the key you are on — and re-runs
 * the loader. In-flight results for an old key are ignored.
 *
 * With ``revalidateOnReconnect`` (default on) the loader re-runs when
 * the WebSocket comes back after a disconnect — frames sent while the
 * socket was down are gone, so the page refetches to catch up. The
 * loader receives ``{reason}`` so such a revalidation can bypass an
 * in-flight dedupe (``reason === 'reconnect'``).
 *
 * Focus contract: after an error, ``LoadErrorState``'s Retry button
 * holds focus and is unmounted once the data lands. ``onRecovered``
 * runs right after that error → ready transition so the page can move
 * focus somewhere meaningful (its add field, or the list heading).
 * After unmount nothing is set and no callback runs.
 */
import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { connectionState } from '@/ws'

export type LoadState = 'loading' | 'ready' | 'error'
export type LoadReason = 'initial' | 'retry' | 'reconnect'

export interface UseLoadOptions {
  /** The store already has data to show for the current ``key``. */
  cached?: boolean
  /** Lifecycle scope; a change resets and re-runs the loader. */
  key?: string
  /** Re-run the loader after a WebSocket reconnect. Default ``true``. */
  revalidateOnReconnect?: boolean
  /** Runs once a failed load is recovered (error → ready). */
  onRecovered?: () => void
}

export interface UseLoadResult {
  state: LoadState
  /** Cached data is on screen but the latest revalidate failed. */
  stale: boolean
  retry: () => void
}

interface Snapshot {
  key: string
  state: LoadState
  stale: boolean
}

export function useLoad(
  loader: (ctx: { reason: LoadReason }) => Promise<unknown>,
  {
    cached = false, key = '', revalidateOnReconnect = true, onRecovered,
  }: UseLoadOptions = {},
): UseLoadResult {
  const initial = (): Snapshot => ({ key, state: cached ? 'ready' : 'loading', stale: false })
  const [snap, setSnap] = useState<Snapshot>(initial)
  // A key change resets synchronously — no frame of the old key's state.
  const current: Snapshot = snap.key === key ? snap : initial()

  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const recoveredRef = useRef(onRecovered)
  recoveredRef.current = onRecovered
  const keyRef = useRef(key)
  keyRef.current = key
  /** Bumped per run so a slow earlier result can't overwrite a newer one. */
  const seq = useRef(0)
  const alive = useRef(true)
  /** Latest snapshot, for the async callbacks. */
  const snapRef = useRef(current)
  snapRef.current = current

  const run = useCallback((reason: LoadReason) => {
    const mine = ++seq.current
    const runKey = keyRef.current
    const before = snapRef.current
    if (before.state === 'error') setSnap({ ...before, key: runKey, state: 'loading' })
    loaderRef.current({ reason }).then(
      () => {
        if (!alive.current || mine !== seq.current || runKey !== keyRef.current) return
        const wasError = before.state === 'error'
        setSnap({ key: runKey, state: 'ready', stale: false })
        if (wasError) recoveredRef.current?.()
      },
      () => {
        if (!alive.current || mine !== seq.current || runKey !== keyRef.current) return
        const now = snapRef.current
        // Cached / previously loaded data stays on screen, marked stale.
        setSnap(now.state === 'ready'
          ? { key: runKey, state: 'ready', stale: true }
          : { key: runKey, state: 'error', stale: false })
      },
    )
  }, [])

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  useEffect(() => {
    run('initial')
  }, [key, run])

  useEffect(() => {
    if (!revalidateOnReconnect) return
    let prev = connectionState.value
    return connectionState.subscribe((next) => {
      if (prev === 'reconnecting' && next === 'open') run('reconnect')
      prev = next
    })
  }, [revalidateOnReconnect, run])

  const retry = useCallback(() => run('retry'), [run])
  return { state: current.state, stale: current.stale, retry }
}
