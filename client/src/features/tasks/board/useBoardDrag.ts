/**
 * useBoardDrag — pointer-event drag for board cards (mouse, pen and
 * touch alike; HTML5 drag-and-drop has no touch support).
 *
 * - **Mouse / pen:** the drag starts once the pointer moved
 *   ``MOUSE_THRESHOLD`` px with the button down — a plain click still
 *   opens the card.
 * - **Touch:** a ``LONG_PRESS_MS`` press starts it. Moving more than
 *   ``TOUCH_SLOP`` px first is a scroll: the page scrolls as usual and
 *   nothing drags. Once dragging, the card's ``touchmove`` is
 *   cancelled so the page holds still under the finger.
 * - While dragging, a ghost follows the pointer (``drag`` has its
 *   position), the drop target under it is ``over`` — a column with an
 *   insertion index, or a column chip (phones) meaning "the bottom of
 *   that column" — and the scroll container / window scrolls when the
 *   pointer nears its edge.
 * - Release over a target calls ``onDrop``; Escape, a cancelled pointer
 *   or a release elsewhere drops nothing. The click that follows a
 *   drag is swallowed so it doesn't open the card.
 *
 * Drop zones are marked in the DOM: ``data-board-drop="<status>"``
 * (plus ``data-board-drop-kind="chip"`` for a chip) and the cards in a
 * column ``data-board-card`` + ``data-task-id``. The geometry is pure
 * (``insertionIndex``, ``edgeScrollDelta``, ``passedThreshold``) and
 * tested on its own.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { TaskStatus } from '@/store/tasks'

export const MOUSE_THRESHOLD = 4
export const LONG_PRESS_MS = 300
export const TOUCH_SLOP = 8
/** Distance from an edge where auto-scroll starts, and its top speed. */
export const EDGE = 56
export const MAX_SCROLL_STEP = 18
/** How long after a drop a stray click is still swallowed. */
export const CLICK_GUARD_MS = 400

export interface DropTarget {
  status: TaskStatus
  /** Index among the column's cards without the dragged one; a chip
   *  target is ``Infinity`` (the bottom). */
  index: number
  kind: 'column' | 'chip'
}

export interface DragState {
  id: string
  /** Pointer position (viewport px). */
  x: number
  y: number
  /** Where the card was grabbed, relative to its top-left corner. */
  offsetX: number
  offsetY: number
  width: number
  pointerType: string
}

export function passedThreshold(dx: number, dy: number, px: number = MOUSE_THRESHOLD): boolean {
  return Math.hypot(dx, dy) > px
}

/** How many cards (by vertical midpoint, top to bottom) sit above ``y``. */
export function insertionIndex(midpoints: readonly number[], y: number): number {
  let i = 0
  while (i < midpoints.length && midpoints[i] < y) i++
  return i
}

/** Scroll step for a pointer at ``pos`` between ``min`` and ``max``:
 *  negative near ``min``, positive near ``max``, faster closer to the
 *  edge, 0 in the middle. */
export function edgeScrollDelta(
  pos: number, min: number, max: number, edge: number = EDGE, maxStep: number = MAX_SCROLL_STEP,
): number {
  if (max - min <= edge * 2) return 0
  if (pos < min + edge) return -Math.ceil(maxStep * Math.min(1, (min + edge - pos) / edge))
  if (pos > max - edge) return Math.ceil(maxStep * Math.min(1, (pos - (max - edge)) / edge))
  return 0
}

/** The drop target under a viewport point, ignoring the dragged card. */
export function hitTest(x: number, y: number, draggedId: string): DropTarget | null {
  const el = typeof document.elementFromPoint === 'function'
    ? document.elementFromPoint(x, y) as HTMLElement | null
    : null
  const zone = el?.closest<HTMLElement>('[data-board-drop]')
  if (!zone) return null
  const status = zone.dataset.boardDrop as TaskStatus
  if (zone.dataset.boardDropKind === 'chip') return { status, index: Number.POSITIVE_INFINITY, kind: 'chip' }
  const mids = Array.from(zone.querySelectorAll<HTMLElement>('[data-board-card]'))
    .filter(c => c.dataset.taskId !== draggedId)
    .map((c) => {
      const r = c.getBoundingClientRect()
      return r.top + r.height / 2
    })
  return { status, index: insertionIndex(mids, y), kind: 'column' }
}

function scrollParent(el: HTMLElement | null): HTMLElement | null {
  for (let n = el?.parentElement ?? null; n; n = n.parentElement) {
    const oy = getComputedStyle(n).overflowY
    if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight) return n
  }
  return null
}

interface Opts {
  /** May this card be dragged? (read-only cards can't) */
  canDrag: (id: string) => boolean
  onDrop: (id: string, target: DropTarget) => void
  /** Scrolls sideways when the pointer nears its left / right edge. */
  scrollRef?: { current: HTMLElement | null }
}

export interface CardDragProps {
  onPointerDown: (e: PointerEvent) => void
  onTouchMove: (e: TouchEvent) => void
  onContextMenu: (e: MouseEvent) => void
}

interface Gesture {
  phase: 'pending' | 'dragging'
  id: string
  el: HTMLElement
  pointerId: number
  pointerType: string
  startX: number
  startY: number
  x: number
  y: number
  timer: ReturnType<typeof setTimeout> | null
  over: DropTarget | null
  raf: number | null
  scroller: HTMLElement | null
}

export function useBoardDrag({ canDrag, onDrop, scrollRef }: Opts) {
  const [drag, setDrag] = useState<DragState | null>(null)
  const [over, setOver] = useState<DropTarget | null>(null)
  const [pressingId, setPressingId] = useState<string | null>(null)
  const g = useRef<Gesture | null>(null)
  const optsRef = useRef({ canDrag, onDrop, scrollRef })
  optsRef.current = { canDrag, onDrop, scrollRef }
  const cleanupRef = useRef<() => void>(() => {})

  useEffect(() => () => cleanupRef.current(), [])

  function updateOver(cur: Gesture) {
    const next = hitTest(cur.x, cur.y, cur.id)
    const prev = cur.over
    if (prev?.status === next?.status && prev?.index === next?.index && prev?.kind === next?.kind) return
    cur.over = next
    setOver(next)
  }

  function tick() {
    const cur = g.current
    if (!cur || cur.phase !== 'dragging') return
    let moved = false
    // The edges are the inner scroller's when the board scrolls in one.
    const vr = cur.scroller?.getBoundingClientRect()
    const dy = vr ? edgeScrollDelta(cur.y, vr.top, vr.bottom) : edgeScrollDelta(cur.y, 0, window.innerHeight)
    if (dy) {
      if (cur.scroller) cur.scroller.scrollTop += dy
      else window.scrollBy(0, dy)
      moved = true
    }
    const box = optsRef.current.scrollRef?.current
    if (box && box.scrollWidth > box.clientWidth) {
      const r = box.getBoundingClientRect()
      const dx = edgeScrollDelta(cur.x, r.left, r.right)
      if (dx) { box.scrollLeft += dx; moved = true }
    }
    if (moved) updateOver(cur)
    cur.raf = requestAnimationFrame(tick)
  }

  function begin(cur: Gesture) {
    cur.phase = 'dragging'
    if (cur.timer) { clearTimeout(cur.timer); cur.timer = null }
    setPressingId(null)
    const r = cur.el.getBoundingClientRect()
    cur.scroller = scrollParent(cur.el)
    document.body.classList.add('sh-board-dragging')
    window.getSelection?.()?.removeAllRanges()
    if (cur.pointerType === 'touch') navigator.vibrate?.(8)
    setDrag({
      id: cur.id, x: cur.x, y: cur.y,
      offsetX: cur.startX - r.left, offsetY: cur.startY - r.top,
      width: r.width, pointerType: cur.pointerType,
    })
    updateOver(cur)
    cur.raf = requestAnimationFrame(tick)
  }

  function end(drop: boolean) {
    const cur = g.current
    cleanupRef.current()
    if (!cur) return
    if (cur.phase === 'dragging') {
      // The click that ends a drag must not open the card — mouse or
      // touch (a long-press release can still produce one). Browsers
      // send it after pointerup, sometimes in a later task — so the
      // guard takes the first click, or lapses after a moment.
      guardClick()
      if (drop && cur.over) optsRef.current.onDrop(cur.id, cur.over)
    }
  }

  function guardClick() {
    let timer: ReturnType<typeof setTimeout> | null = null
    const swallow = (e: Event) => {
      e.stopPropagation()
      e.preventDefault()
      window.removeEventListener('click', swallow, true)
      if (timer) clearTimeout(timer)
    }
    window.addEventListener('click', swallow, true)
    timer = setTimeout(() => window.removeEventListener('click', swallow, true), CLICK_GUARD_MS)
  }

  function onMove(e: PointerEvent) {
    const cur = g.current
    if (!cur || e.pointerId !== cur.pointerId) return
    cur.x = e.clientX
    cur.y = e.clientY
    const dx = cur.x - cur.startX
    const dy = cur.y - cur.startY
    if (cur.phase === 'pending') {
      if (cur.pointerType === 'touch') {
        // Moved before the long-press fired: that's a scroll.
        if (passedThreshold(dx, dy, TOUCH_SLOP)) end(false)
        return
      }
      if (!passedThreshold(dx, dy)) return
      begin(cur)
    }
    e.preventDefault()
    setDrag(d => d && { ...d, x: cur.x, y: cur.y })
    updateOver(cur)
  }

  function onUp(e: PointerEvent) {
    const cur = g.current
    if (!cur || e.pointerId !== cur.pointerId) return
    cur.x = e.clientX
    cur.y = e.clientY
    if (cur.phase === 'dragging') updateOver(cur)
    end(true)
  }

  function onCancel(e: PointerEvent) {
    if (g.current && e.pointerId === g.current.pointerId) end(false)
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape' && g.current) {
      e.preventDefault()
      e.stopPropagation()
      end(false)
    }
  }

  function cardProps(id: string): CardDragProps {
    return {
      onPointerDown: (e: PointerEvent) => {
        if (!optsRef.current.canDrag(id)) return
        if (e.button !== 0 || !e.isPrimary || g.current) return
        const target = e.target as HTMLElement | null
        // The ⋯ menu and other marked controls never start a drag.
        if (target?.closest('[data-no-drag]')) return
        const el = e.currentTarget as HTMLElement
        const cur: Gesture = {
          phase: 'pending', id, el, pointerId: e.pointerId, pointerType: e.pointerType || 'mouse',
          startX: e.clientX, startY: e.clientY, x: e.clientX, y: e.clientY,
          timer: null, over: null, raf: null, scroller: null,
        }
        g.current = cur
        if (cur.pointerType === 'touch') {
          setPressingId(id)
          cur.timer = setTimeout(() => {
            if (g.current === cur && cur.phase === 'pending') begin(cur)
          }, LONG_PRESS_MS)
        }
        window.addEventListener('pointermove', onMove, { passive: false })
        window.addEventListener('pointerup', onUp)
        window.addEventListener('pointercancel', onCancel)
        window.addEventListener('keydown', onKey, true)
        cleanupRef.current = () => {
          window.removeEventListener('pointermove', onMove)
          window.removeEventListener('pointerup', onUp)
          window.removeEventListener('pointercancel', onCancel)
          window.removeEventListener('keydown', onKey, true)
          if (cur.timer) clearTimeout(cur.timer)
          if (cur.raf !== null) cancelAnimationFrame(cur.raf)
          document.body.classList.remove('sh-board-dragging')
          if (g.current === cur) g.current = null
          setDrag(null)
          setOver(null)
          setPressingId(null)
          cleanupRef.current = () => {}
        }
      },
      // Holds the page still under a dragging finger (non-passive: a
      // card's own listener, so the browser waits for it).
      onTouchMove: (e: TouchEvent) => {
        if (g.current?.phase === 'dragging') e.preventDefault()
      },
      // A long-press would open the browser's context menu.
      onContextMenu: (e: MouseEvent) => {
        if (g.current?.pointerType === 'touch') e.preventDefault()
      },
    }
  }

  return { drag, over, pressingId, cardProps, cancel: () => end(false) }
}
