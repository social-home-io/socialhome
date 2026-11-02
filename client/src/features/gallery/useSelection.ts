/**
 * useSelection — picking several photos in a grid.
 *
 * Three ways in, so nobody depends on a hidden gesture:
 *   • the visible "Select" button (``start()``),
 *   • a long press on a tile on a touch screen (500 ms, a finger that
 *     moves more than 10 px is scrolling, not pressing),
 *   • Ctrl / ⌘ / Shift + click on a tile on a desktop.
 * While selecting, a click (or Space / Enter on the focused tile) toggles
 * one tile, Shift + click selects the run from the last tile clicked, and
 * Escape leaves selection.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { openDialogCount } from '@/components/Modal'

export const LONG_PRESS_MS = 500
const MOVE_SLOP_PX = 10

export interface Selection {
  active: boolean
  ids: ReadonlySet<string>
  start: (id?: string) => void
  clear: () => void
  /** Keep only these ids selected (after a partial delete). */
  keep: (ids: readonly string[]) => void
  toggle: (id: string, range?: boolean) => void
  /** Pointer + click handlers for a tile — see the module doc. ``open``
   *  is the tile's normal action outside selection (follow its link). */
  tileHandlers: (id: string) => {
    onClick: (e: MouseEvent) => void
    onPointerDown: (e: PointerEvent) => void
    onPointerMove: (e: PointerEvent) => void
    onPointerUp: () => void
    onPointerCancel: () => void
    onContextMenu: (e: Event) => void
  }
}

export function useSelection(order: readonly string[]): Selection {
  const [active, setActive] = useState(false)
  const [ids, setIds] = useState<ReadonlySet<string>>(new Set())
  const anchor = useRef<string | null>(null)
  const press = useRef<{ id: string; x: number; y: number; timer: ReturnType<typeof setTimeout> } | null>(null)
  // A long press ends with a click on the same tile — that click must not
  // toggle it straight back (or open it).
  const swallowClick = useRef(false)
  const orderRef = useRef(order)
  orderRef.current = order

  const start = (id?: string) => {
    setActive(true)
    if (id) {
      setIds(new Set([id]))
      anchor.current = id
    }
  }
  const clear = () => {
    setActive(false)
    setIds(new Set())
    anchor.current = null
  }
  const keep = (keepIds: readonly string[]) => {
    const next = new Set(keepIds)
    setIds(next)
    if (next.size === 0) setActive(false)
  }
  const toggle = (id: string, range = false) => {
    setIds((prev) => {
      const next = new Set(prev)
      const from = anchor.current
      if (range && from && from !== id) {
        const list = orderRef.current
        const a = list.indexOf(from)
        const b = list.indexOf(id)
        if (a >= 0 && b >= 0) {
          for (const x of list.slice(Math.min(a, b), Math.max(a, b) + 1)) next.add(x)
          return next
        }
      }
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    anchor.current = id
    setActive(true)
  }

  // Escape leaves selection — unless a dialog is open on top (its own
  // Escape closes it first).
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || openDialogCount.value > 0 || e.defaultPrevented) return
      clear()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [active])

  const cancelPress = () => {
    if (press.current) clearTimeout(press.current.timer)
    press.current = null
  }
  useEffect(() => cancelPress, [])

  const tileHandlers = (id: string) => ({
    onClick: (e: MouseEvent) => {
      if (swallowClick.current) {
        swallowClick.current = false
        e.preventDefault()
        return
      }
      if (active) {
        e.preventDefault()
        toggle(id, e.shiftKey)
        return
      }
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault()
        start(id)
      }
    },
    onPointerDown: (e: PointerEvent) => {
      // A press whose click never came (the finger slid off) must not
      // swallow the next tap.
      swallowClick.current = false
      if (e.pointerType !== 'touch') return
      cancelPress()
      press.current = {
        id, x: e.clientX, y: e.clientY,
        timer: setTimeout(() => {
          press.current = null
          swallowClick.current = true
          if (active) toggle(id)
          else start(id)
          try { navigator.vibrate?.(15) } catch { /* not on every phone */ }
        }, LONG_PRESS_MS),
      }
    },
    onPointerMove: (e: PointerEvent) => {
      const p = press.current
      if (p && Math.hypot(e.clientX - p.x, e.clientY - p.y) > MOVE_SLOP_PX) cancelPress()
    },
    onPointerUp: () => cancelPress(),
    onPointerCancel: () => cancelPress(),
    // The phone's own long-press menu ("Open in new tab…") would cover
    // the selection.
    onContextMenu: (e: Event) => {
      if (press.current || swallowClick.current || active) e.preventDefault()
    },
  })

  return { active, ids, start, clear, keep, toggle, tileHandlers }
}
