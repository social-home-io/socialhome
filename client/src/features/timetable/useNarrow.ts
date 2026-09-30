/**
 * ``true`` below 640 px — the timetable swaps its week grid for the
 * one-day-at-a-time mobile view there. Tracks ``matchMedia`` changes
 * (rotation, window resize); ``false`` where ``matchMedia`` is missing.
 */
import { useEffect, useState } from 'preact/hooks'

export const NARROW_QUERY = '(max-width: 639px)'

function matches(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia(NARROW_QUERY).matches
}

export function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(matches)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(NARROW_QUERY)
    const on = () => setNarrow(mq.matches)
    on()
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])
  return narrow
}
