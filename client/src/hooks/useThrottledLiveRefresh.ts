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
 *  - at most one refresh per {@link LIVE_REFRESH_MIN_GAP_MS}, counting the
 *    page's own load at mount; a burst ends in one trailing refresh;
 *  - nothing while the tab is hidden; the first frame missed there earns
 *    one refresh when the page shows again.
 */
import { useEffect, useRef } from 'preact/hooks'
import { ws } from '@/ws'
import { isSystemChatFrame } from '@/store/dms'

export const LIVE_REFRESH_MIN_GAP_MS = 10_000

export function useThrottledLiveRefresh(
  refresh: () => void,
  events: readonly string[],
): void {
  // The latest callback, so a re-render never re-subscribes.
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  const eventsKey = events.join('|')

  useEffect(() => {
    let last = Date.now()  // the page's own load at mount
    let timer: ReturnType<typeof setTimeout> | null = null
    let missedWhileHidden = false

    const fire = () => {
      if (document.hidden) {
        missedWhileHidden = true
        return
      }
      last = Date.now()
      refreshRef.current()
    }
    const trigger = () => {
      if (document.hidden) {
        missedWhileHidden = true
        return
      }
      if (timer) return  // a trailing refresh is already due
      const wait = last + LIVE_REFRESH_MIN_GAP_MS - Date.now()
      if (wait <= 0) {
        fire()
        return
      }
      timer = setTimeout(() => { timer = null; fire() }, wait)
    }
    const onVisibility = () => {
      if (document.hidden || !missedWhileHidden) return
      missedWhileHidden = false
      trigger()
    }

    const offs = events.map(type => ws.on(type, (e) => {
      if (type === 'dm.message' && isSystemChatFrame(e.data)) return
      trigger()
    }))
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      offs.forEach(off => off())
      document.removeEventListener('visibilitychange', onVisibility)
      if (timer) clearTimeout(timer)
    }
    // ``eventsKey`` stands for ``events`` (a fresh array each render).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventsKey])
}
