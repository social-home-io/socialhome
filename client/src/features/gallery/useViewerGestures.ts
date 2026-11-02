/**
 * Touch, pen and mouse handling for the gallery viewer.
 *
 * - One finger at fit size: a sideways swipe goes to the next / previous
 *   item; the photo follows the finger unless the person asked for
 *   reduced motion (then it simply changes on release).
 * - Two fingers: pinch-zoom around the fingers, and pan with them.
 * - One finger on a zoomed photo: pan.
 * - Double-tap (touch) / double-click (mouse): zoom in on that point, or
 *   back to fit.
 * - Mouse wheel / trackpad pinch: zoom around the pointer.
 *
 * The arithmetic lives in ``zoomPan.ts``; this hook only turns pointer
 * events into calls to it. A press that starts on a video (its scrubber,
 * play button) or on a control is left alone.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { RefObject } from 'preact'
import {
  FIT, STEP_SCALE, clampView, distance, isZoomed, midpoint, panBy,
  swipeDirection, toggleZoom, zoomAt,
  type Point, type Size, type ZoomView,
} from './zoomPan'

interface Options {
  stageRef: RefObject<HTMLElement | null>
  mediaRef: RefObject<HTMLElement | null>
  /** Photos zoom; a video doesn't (its own controls take the presses). */
  zoomable: boolean
  reducedMotion: boolean
  onSwipe: (dir: 1 | -1) => void
  /** The item shown — a new one starts at fit size. */
  resetKey: string
}

interface Press {
  start: Point
  /** Client coordinates, for the swipe distance. */
  startClient: Point
  startView: ZoomView
  t0: number
  moved: boolean
}

const DOUBLE_TAP_MS = 300
const TAP_SLOP_PX = 10

export function useViewerGestures({
  stageRef, mediaRef, zoomable, reducedMotion, onSwipe, resetKey,
}: Options) {
  const [view, setViewState] = useState<ZoomView>(FIT)
  const [dragX, setDragX] = useState(0)
  const viewRef = useRef(view)
  const pointers = useRef(new Map<number, Point>())
  const press = useRef<Press | null>(null)
  const pinch = useRef<{ dist: number; mid: Point; view: ZoomView } | null>(null)
  const lastTap = useRef<{ t: number; at: Point } | null>(null)
  const lastPointer = useRef<string>('mouse')
  const onSwipeRef = useRef(onSwipe)
  onSwipeRef.current = onSwipe

  const setView = (v: ZoomView) => {
    viewRef.current = v
    setViewState(v)
  }

  useEffect(() => {
    setView(FIT)
    setDragX(0)
    press.current = null
    pinch.current = null
    pointers.current.clear()
  }, [resetKey])

  const sizes = (): { stage: Size; media: Size } | null => {
    const s = stageRef.current
    const m = mediaRef.current
    if (!s || !m) return null
    return {
      stage: { w: s.clientWidth, h: s.clientHeight },
      // Layout size: the fitted photo, unaffected by the transform.
      media: { w: m.offsetWidth, h: m.offsetHeight },
    }
  }

  /** A client point relative to the stage centre. */
  const local = (clientX: number, clientY: number): Point => {
    const r = stageRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0 }
    return { x: clientX - r.left - r.width / 2, y: clientY - r.top - r.height / 2 }
  }

  const zoomTo = (scale: number, at: Point = { x: 0, y: 0 }) => {
    const z = sizes()
    if (!z || !zoomable) return
    setView(zoomAt(viewRef.current, scale, at, z.stage, z.media))
  }

  // Wheel needs a non-passive listener to keep the page from scrolling.
  useEffect(() => {
    const el = stageRef.current
    if (!el || !zoomable) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const z = sizes()
      if (!z) return
      // Trackpad pinches arrive as ctrl+wheel with small deltas.
      const factor = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002))
      const v = viewRef.current
      setView(zoomAt(v, v.scale * factor, local(e.clientX, e.clientY), z.stage, z.media))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomable, resetKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const ignore = (e: PointerEvent) =>
    !!(e.target as Element | null)?.closest?.('video, button, a, input')

  const onPointerDown = (e: PointerEvent) => {
    lastPointer.current = e.pointerType
    if (ignore(e)) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    stageRef.current?.setPointerCapture?.(e.pointerId)
    const p = local(e.clientX, e.clientY)
    pointers.current.set(e.pointerId, p)
    if (pointers.current.size === 2 && zoomable) {
      const [a, b] = [...pointers.current.values()]
      pinch.current = { dist: distance(a, b), mid: midpoint(a, b), view: viewRef.current }
      press.current = null
      setDragX(0)
    } else if (pointers.current.size === 1) {
      press.current = {
        start: p, startClient: { x: e.clientX, y: e.clientY },
        startView: viewRef.current, t0: e.timeStamp || Date.now(), moved: false,
      }
    }
  }

  const onPointerMove = (e: PointerEvent) => {
    if (!pointers.current.has(e.pointerId)) return
    const p = local(e.clientX, e.clientY)
    pointers.current.set(e.pointerId, p)
    const z = sizes()
    if (!z) return
    const pz = pinch.current
    if (pz && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()]
      const mid = midpoint(a, b)
      const scale = pz.view.scale * (distance(a, b) / Math.max(1, pz.dist))
      // Zoom around where the fingers started, then follow them.
      const zoomed = zoomAt(pz.view, scale, pz.mid, z.stage, z.media)
      setView(clampView(
        { ...zoomed, x: zoomed.x + mid.x - pz.mid.x, y: zoomed.y + mid.y - pz.mid.y },
        z.stage, z.media,
      ))
      return
    }
    const pr = press.current
    if (!pr) return
    const dx = e.clientX - pr.startClient.x
    const dy = e.clientY - pr.startClient.y
    if (Math.abs(dx) > TAP_SLOP_PX || Math.abs(dy) > TAP_SLOP_PX) pr.moved = true
    if (isZoomed(pr.startView)) {
      setView(panBy(pr.startView, dx, dy, z.stage, z.media))
    } else if (!reducedMotion && Math.abs(dx) > Math.abs(dy)) {
      setDragX(dx)
    }
  }

  const finish = (e: PointerEvent, cancelled: boolean) => {
    if (!pointers.current.has(e.pointerId)) return
    pointers.current.delete(e.pointerId)
    if (pinch.current) {
      if (pointers.current.size < 2) {
        pinch.current = null
        // A pinch that ends near fit snaps to fit.
        if (viewRef.current.scale < 1.05) setView(FIT)
        // The finger still down starts no swipe.
        press.current = null
      }
      return
    }
    const pr = press.current
    press.current = null
    setDragX(0)
    if (!pr || cancelled) return
    const dx = e.clientX - pr.startClient.x
    const dy = e.clientY - pr.startClient.y
    const now = e.timeStamp || Date.now()
    if (!pr.moved) {
      if (e.pointerType !== 'mouse' && zoomable) {
        const at = local(e.clientX, e.clientY)
        const last = lastTap.current
        if (last && now - last.t < DOUBLE_TAP_MS && distance(last.at, at) < 40) {
          lastTap.current = null
          const z = sizes()
          if (z) setView(toggleZoom(viewRef.current, at, z.stage, z.media))
        } else {
          lastTap.current = { t: now, at }
        }
      }
      return
    }
    if (isZoomed(pr.startView)) return
    const width = stageRef.current?.clientWidth ?? 0
    const dir = swipeDirection(dx, dy, now - pr.t0, width)
    if (dir !== 0) onSwipeRef.current(dir)
  }

  const onDblClick = (e: MouseEvent) => {
    // A touch double-tap is handled on pointerup; the browser's own
    // dblclick after it would zoom straight back out.
    if (lastPointer.current !== 'mouse') return
    if (!zoomable || (e.target as Element | null)?.closest?.('video, button, a')) return
    const z = sizes()
    if (!z) return
    setView(toggleZoom(viewRef.current, local(e.clientX, e.clientY), z.stage, z.media))
  }

  return {
    view,
    dragX,
    zoomIn: () => zoomTo(isZoomed(viewRef.current) ? viewRef.current.scale * 1.5 : STEP_SCALE),
    zoomOut: () => zoomTo(viewRef.current.scale / 1.5),
    reset: () => setView(FIT),
    pan: (dx: number, dy: number) => {
      const z = sizes()
      if (z) setView(panBy(viewRef.current, dx, dy, z.stage, z.media))
    },
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp: (e: PointerEvent) => finish(e, false),
      onPointerCancel: (e: PointerEvent) => finish(e, true),
      onDblClick,
    },
  }
}
