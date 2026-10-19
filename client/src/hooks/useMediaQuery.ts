/**
 * useMediaQuery — whether a CSS media query matches now, re-rendering
 * when that changes (a window resized, a phone rotated).
 *
 * ``false`` where ``matchMedia`` doesn't exist (tests without a stub,
 * very old engines): callers pick the query so that ``false`` is the
 * safe default.
 */
import { useEffect, useState } from 'preact/hooks'

function matches(query: string): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia(query).matches
}

export function useMediaQuery(query: string): boolean {
  const [on, setOn] = useState(() => matches(query))
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(query)
    const update = () => setOn(mql.matches)
    update()
    mql.addEventListener?.('change', update)
    return () => mql.removeEventListener?.('change', update)
  }, [query])
  return on
}
