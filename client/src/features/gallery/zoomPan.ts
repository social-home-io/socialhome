/**
 * Zoom + pan arithmetic for the gallery viewer — pure functions, no DOM.
 *
 * The photo is laid out fitted and centred in the stage; zoom is a CSS
 * ``transform: translate(x, y) scale(s)`` with ``transform-origin`` at
 * its centre. ``x`` / ``y`` are screen pixels from the centred position.
 * Points (a finger, the mouse) are given relative to the stage centre.
 */

export interface ZoomView { scale: number; x: number; y: number }
export interface Size { w: number; h: number }
export interface Point { x: number; y: number }

export const MIN_SCALE = 1
export const MAX_SCALE = 4
/** Double-tap / double-click / the zoom button go straight here. */
export const STEP_SCALE = 2.5

export const FIT: ZoomView = { scale: 1, x: 0, y: 0 }

export function isZoomed(v: ZoomView): boolean {
  return v.scale > 1.01
}

/** Keep the zoomed photo covering the stage: no pan past its edges, and
 *  centred on an axis where it is smaller than the stage. */
export function clampView(v: ZoomView, stage: Size, media: Size): ZoomView {
  const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, v.scale))
  if (scale <= 1.01) return FIT
  const maxX = Math.max(0, (media.w * scale - stage.w) / 2)
  const maxY = Math.max(0, (media.h * scale - stage.h) / 2)
  return {
    scale,
    // ``|| 0`` drops a -0 (an axis that cannot pan).
    x: Math.min(maxX, Math.max(-maxX, v.x)) || 0,
    y: Math.min(maxY, Math.max(-maxY, v.y)) || 0,
  }
}

/** Zoom to ``scale`` keeping the photo point under ``at`` where it is. */
export function zoomAt(
  v: ZoomView, scale: number, at: Point, stage: Size, media: Size,
): ZoomView {
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale))
  const k = next / v.scale
  return clampView(
    { scale: next, x: at.x - (at.x - v.x) * k, y: at.y - (at.y - v.y) * k },
    stage, media,
  )
}

/** Double-tap: fit when zoomed, else zoom in on the tapped point. */
export function toggleZoom(v: ZoomView, at: Point, stage: Size, media: Size): ZoomView {
  return isZoomed(v) ? FIT : zoomAt(v, STEP_SCALE, at, stage, media)
}

/** Move a zoomed photo by ``dx`` / ``dy`` screen pixels. */
export function panBy(v: ZoomView, dx: number, dy: number, stage: Size, media: Size): ZoomView {
  return clampView({ ...v, x: v.x + dx, y: v.y + dy }, stage, media)
}

/** A horizontal drag on a photo at fit size: which way to go, or 0 for
 *  "snap back". Far enough (a fifth of the stage, at least 50 px) or a
 *  quick flick, and mostly sideways. */
export function swipeDirection(dx: number, dy: number, ms: number, stageWidth: number): -1 | 0 | 1 {
  if (Math.abs(dx) < Math.abs(dy) * 1.2) return 0
  const far = Math.abs(dx) >= Math.max(50, stageWidth * 0.2)
  const flick = Math.abs(dx) >= 30 && ms > 0 && Math.abs(dx) / ms > 0.5
  if (!far && !flick) return 0
  // Finger moves left → the next photo comes in from the right.
  return dx < 0 ? 1 : -1
}

export function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

export function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}
