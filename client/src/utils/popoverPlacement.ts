/**
 * Placement maths for the popovers that hang off a text field or a
 * button — the ``:emoji`` and ``@mention`` autocompletes and the emoji
 * picker (``ReactionPicker`` with an ``anchor``).
 *
 * On a phone with the keyboard up the area the user can actually see is
 * the *visual* viewport, which ends well above ``innerHeight`` on iOS
 * Safari and Android Chrome (only the visual viewport shrinks there). A
 * popover measured against ``innerHeight`` opens below the field —
 * straight behind the keyboard. These helpers measure against the
 * visual viewport, flip above the field when there's more room there,
 * and cap the height to the room that's left so the list never runs
 * off the top of a short screen either.
 *
 * All coordinates are layout-viewport (``getBoundingClientRect`` /
 * ``position: fixed``) px, the same space ``visualViewport.offsetTop``
 * is expressed in.
 */

/** The on-screen rectangle, in layout-viewport coordinates. */
export interface VisibleArea {
  top: number
  bottom: number
  width: number
}

export interface VerticalPlacement {
  /** ``top`` for ``below``; the popover's *bottom* edge for ``above``
   *  (render with ``translateY(-100%)``). */
  top: number
  placement: 'above' | 'below'
  /** Room on the chosen side, minus the edge margin. */
  maxHeight: number
}

/** Gap between the field and the popover. */
export const POPOVER_GAP = 4
/** Margin kept free at the visible area's edges. */
export const POPOVER_EDGE = 8
/** Never squeeze a list below this — two or three rows stay usable. */
export const POPOVER_MIN_HEIGHT = 96

/** What the user can see right now: the visual viewport when the
 *  browser has one, else the layout viewport. */
export function visibleArea(win: Window = window): VisibleArea {
  const vv = win.visualViewport
  if (vv) return { top: vv.offsetTop, bottom: vv.offsetTop + vv.height, width: vv.width }
  return { top: 0, bottom: win.innerHeight, width: win.innerWidth }
}

/**
 * Below the anchor when ``preferred`` px fit there, else on whichever
 * side has more room. ``maxHeight`` caps the list to that side's room.
 */
export function placeVertically(
  anchor: { top: number, bottom: number },
  area: VisibleArea,
  preferred: number,
): VerticalPlacement {
  const below = area.bottom - anchor.bottom - POPOVER_GAP - POPOVER_EDGE
  const above = anchor.top - area.top - POPOVER_GAP - POPOVER_EDGE
  const flip = below < preferred && above > below
  const room = flip ? above : below
  return {
    top: flip ? anchor.top - POPOVER_GAP : anchor.bottom + POPOVER_GAP,
    placement: flip ? 'above' : 'below',
    maxHeight: Math.max(POPOVER_MIN_HEIGHT, Math.floor(room)),
  }
}

/** Keep ``[start, start + width]`` inside the visible width. */
export function clampHorizontally(start: number, width: number, area: VisibleArea): number {
  return Math.max(POPOVER_EDGE, Math.min(start, area.width - width - POPOVER_EDGE))
}

export interface AnchoredPlacement extends VerticalPlacement {
  left: number
  /** The popover's width, narrowed to the visible width when needed. */
  width: number
}

/**
 * A ``width`` × ``height`` popover hung off ``anchor`` (a button): below
 * it when it fits there, else on the side with more room, its height
 * capped to that room; horizontally lined up with the anchor's ``end``
 * (right) or ``start`` (left) edge and kept inside the visible width.
 */
export function placeAnchored(
  anchor: { top: number, bottom: number, left: number, right: number },
  size: { width: number, height: number },
  area: VisibleArea,
  align: 'start' | 'end' = 'end',
): AnchoredPlacement {
  const width = Math.min(size.width, area.width - 2 * POPOVER_EDGE)
  const start = align === 'end' ? anchor.right - width : anchor.left
  return {
    ...placeVertically(anchor, area, size.height),
    left: clampHorizontally(start, width, area),
    width,
  }
}

/**
 * ``position: fixed`` resolves against the nearest transformed /
 * filtered ancestor rather than the viewport (the comment sheet's
 * backdrop uses ``backdrop-filter``). Place ``el`` at the wanted
 * viewport coordinates, measure where it actually landed and cancel the
 * difference. ``top`` is the bottom edge for ``above`` placement.
 */
export function pinFixed(
  el: HTMLElement,
  left: number,
  top: number,
  placement: 'above' | 'below',
): void {
  el.style.left = `${left}px`
  el.style.top = `${top}px`
  const r = el.getBoundingClientRect()
  const dx = r.left - left
  const dy = (placement === 'above' ? r.bottom : r.top) - top
  if (dx) el.style.left = `${left - dx}px`
  if (dy) el.style.top = `${top - dy}px`
}
