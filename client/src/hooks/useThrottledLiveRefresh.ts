/**
 * useThrottledLiveRefresh — refetch a page's bundle on live WS frames,
 * without turning a lively household into a request storm.
 *
 * The home pages (Welcome, Dashboard) refetched ``/api/me/corner`` on every
 * listed frame with a 200 ms debounce — including every message in a busy
 * space chat, which rides the ``dm.message`` frame too: ~57 GETs a minute
 * against the 60/min bucket. Here:
 *
 *  - space / household chat frames (``system_scope`` set) are ignored —
 *    they never change the corner;
 *  - at most one refresh per ``minGapMs`` (default
 *    {@link LIVE_REFRESH_MIN_GAP_MS}), counting the page's own load at
 *    mount; a burst ends in one trailing refresh;
 *  - nothing while the tab is hidden; the first frame missed there earns
 *    one refresh when the page shows again;
 *  - ``accept`` narrows the frames that count (the open item's id).
 */
import { useEffect, useRef } from 'preact/hooks'
import { ws } from '@/ws'
import { isSystemChatFrame } from '@/store/dms'
import { createLiveThrottle } from '@/utils/liveThrottle'

export const LIVE_REFRESH_MIN_GAP_MS = 10_000

export interface ThrottledLiveRefreshOptions {
  /** The gap between refreshes (default {@link LIVE_REFRESH_MIN_GAP_MS}). */
  minGapMs?: number
  /** Only frames this accepts count (e.g. the open item's id). Read at
   *  frame time, so a changed target needs no resubscribe. */
  accept?: (type: string, data: Record<string, unknown>) => boolean
}

export function useThrottledLiveRefresh(
  refresh: () => void,
  events: readonly string[],
  opts: ThrottledLiveRefreshOptions = {},
): void {
  // The latest callback / filter, so a re-render never re-subscribes.
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  const acceptRef = useRef(opts.accept)
  acceptRef.current = opts.accept
  const minGapMs = opts.minGapMs ?? LIVE_REFRESH_MIN_GAP_MS
  const eventsKey = events.join('|')

  useEffect(() => {
    const throttle = createLiveThrottle(() => refreshRef.current(), {
      minGapMs,
      startAt: Date.now(),  // the page's own load at mount
      pauseWhileHidden: true,
    })
    const offs = events.map(type => ws.on(type, (e) => {
      const data = (e.data ?? {}) as Record<string, unknown>
      if (type === 'dm.message' && isSystemChatFrame(data)) return
      if (acceptRef.current && !acceptRef.current(type, data)) return
      throttle.trigger()
    }))
    return () => {
      offs.forEach(off => off())
      throttle.cancel()
    }
    // ``eventsKey`` stands for ``events`` (a fresh array each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventsKey, minGapMs])
}
