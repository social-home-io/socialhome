/**
 * Priority+ navigation: show as many items of a fixed-order list as fit
 * in one row, the rest go behind a More button. The order never changes
 * — only where the cut falls — so muscle memory holds and nothing jumps
 * around by activity.
 *
 * ``fitCount`` is the pure arithmetic; ``usePriorityPlus`` measures the
 * real widths (a hidden copy of every item) and re-runs it whenever the
 * row resizes (ResizeObserver), the labels change (language, badges) or
 * the web fonts finish loading.
 */
import type { RefObject } from 'preact'
import { useLayoutEffect, useState } from 'preact/hooks'

/**
 * How many of ``widths`` fit inline in ``available`` px, keeping
 * ``moreWidth`` for the More button (always shown) and ``gap`` px
 * between neighbours. Never fewer than ``min`` (the first item anchors
 * the row) and never more than there are items.
 */
export function fitCount(
  widths: readonly number[],
  available: number,
  moreWidth: number,
  gap: number,
  min = 1,
): number {
  let used = moreWidth
  let n = 0
  for (const w of widths) {
    const next = used + gap + w
    if (next > available) break
    used = next
    n += 1
  }
  return Math.min(widths.length, Math.max(min, n))
}

/**
 * Measure and fit. ``rowRef`` is the element whose width is available
 * to the items + More; ``measureRef`` holds a hidden, unconstrained copy
 * of every item followed by a copy of the More button (same classes, so
 * the same widths). ``key`` changes whenever the copies' text does.
 * Returns how many items to show inline (all of them until measured).
 */
export function usePriorityPlus(
  rowRef: RefObject<HTMLElement | null>,
  measureRef: RefObject<HTMLElement | null>,
  total: number,
  key: string,
): number {
  const [count, setCount] = useState(total)

  useLayoutEffect(() => {
    const row = rowRef.current
    const measure = measureRef.current
    if (!row || !measure) return
    const fit = () => {
      const kids = Array.from(measure.children) as HTMLElement[]
      if (kids.length === 0) return
      const more = kids[kids.length - 1]
      const widths = kids.slice(0, -1).map((el) => el.getBoundingClientRect().width)
      const style = getComputedStyle(row)
      const gap = parseFloat(style.columnGap || style.gap || '0') || 0
      setCount(fitCount(widths, row.clientWidth, more.getBoundingClientRect().width, gap))
    }
    fit()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(fit) : null
    ro?.observe(row)
    // Labels measured in the fallback font are narrower or wider than in
    // the web font; measure again once it is in.
    let live = true
    void document.fonts?.ready?.then(() => { if (live) fit() })
    return () => { live = false; ro?.disconnect() }
  }, [rowRef, measureRef, total, key])

  return Math.min(count, total)
}
