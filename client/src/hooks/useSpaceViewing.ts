/**
 * useSpaceViewing — tell the backend "a person has this space open" (the
 * ``space.viewing`` frame, v_97) about once a minute while the tab is
 * visible. The household then watches the space's GFS channel ring (live
 * items, the latest pins) and fetches what it missed; it stops watching
 * two minutes after the last frame. A hidden tab sends nothing.
 */
import { useEffect } from 'preact/hooks'
import { ws } from '@/ws'

/** How often the frame repeats while the space stays open (ms). */
export const SPACE_VIEWING_INTERVAL_MS = 60_000

export function useSpaceViewing(spaceId: string | null): void {
  useEffect(() => {
    if (!spaceId) return
    const say = () => {
      if (document.visibilityState === 'hidden') return
      ws.send('space.viewing', { space_id: spaceId })
    }
    say()
    const timer = setInterval(say, SPACE_VIEWING_INTERVAL_MS)
    const onVis = () => { if (document.visibilityState === 'visible') say() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [spaceId])
}
