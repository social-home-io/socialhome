/**
 * Do three board columns fit side by side? Measured on the board's own
 * width (a ResizeObserver), not the viewport — the board shares the
 * page with the app's side nav. Below ``MIN_THREE_COLUMNS`` the board
 * shows one column at a time with the column switcher, never a
 * clipped column behind an inner scroll. Without ResizeObserver
 * (old browsers, tests) it assumes they fit.
 */
import { useEffect, useState } from 'preact/hooks'

/** Three 200 px columns plus two 12 px gaps (see ``.sh-board__columns``). */
export const MIN_THREE_COLUMNS = 3 * 200 + 2 * 12

export function useBoardFits(ref: { current: HTMLElement | null }): boolean {
  const [fits, setFits] = useState(true)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = (w: number) => setFits(w >= MIN_THREE_COLUMNS)
    const ro = new ResizeObserver((entries) => {
      const e = entries[entries.length - 1]
      measure(e ? e.contentRect.width : el.getBoundingClientRect().width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return fits
}
