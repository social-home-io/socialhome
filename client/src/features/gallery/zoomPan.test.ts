import { describe, it, expect } from 'vitest'
import {
  FIT, MAX_SCALE, STEP_SCALE, clampView, isZoomed, panBy, swipeDirection, toggleZoom, zoomAt,
} from './zoomPan'

const stage = { w: 400, h: 800 }
const media = { w: 400, h: 300 }   // a landscape photo fitted to a phone

describe('zoomPan', () => {
  it('never zooms out past fit or in past the maximum', () => {
    expect(zoomAt(FIT, 0.3, { x: 0, y: 0 }, stage, media)).toEqual(FIT)
    expect(zoomAt(FIT, 99, { x: 0, y: 0 }, stage, media).scale).toBe(MAX_SCALE)
  })

  it('keeps the point under the fingers where it is while zooming', () => {
    const at = { x: 100, y: 50 }
    const v = zoomAt(FIT, 2, at, stage, media)
    // The photo point under ``at`` was at (100, 50) at fit; it still is.
    expect((at.x - v.x) / v.scale).toBeCloseTo(at.x)
    expect(v.x).toBe(-100)
    expect(v.y).toBe(0)   // 600 px tall in an 800 px stage: stays centred
  })

  it('a zoomed photo pans to its edges and no further', () => {
    const v = zoomAt(FIT, 2, { x: 0, y: 0 }, stage, media)  // 800 × 600
    expect(panBy(v, 1000, 0, stage, media).x).toBe(200)
    expect(panBy(v, -1000, 0, stage, media).x).toBe(-200)
    expect(panBy(v, 0, 500, stage, media).y).toBe(0)
  })

  it('double-tap zooms in on the point, and back to fit', () => {
    const v = toggleZoom(FIT, { x: 50, y: 0 }, stage, media)
    expect(v.scale).toBe(STEP_SCALE)
    expect(isZoomed(v)).toBe(true)
    expect(toggleZoom(v, { x: 0, y: 0 }, stage, media)).toEqual(FIT)
  })

  it('clamping a near-fit view snaps to fit', () => {
    expect(clampView({ scale: 1.005, x: 30, y: 30 }, stage, media)).toEqual(FIT)
  })

  it('a swipe is far enough or a flick, and mostly sideways', () => {
    expect(swipeDirection(-120, 10, 400, 400)).toBe(1)    // finger left → next
    expect(swipeDirection(120, 10, 400, 400)).toBe(-1)    // finger right → previous
    expect(swipeDirection(-40, 0, 400, 400)).toBe(0)      // too short, too slow
    expect(swipeDirection(-40, 0, 50, 400)).toBe(1)       // a quick flick
    expect(swipeDirection(-120, 200, 400, 400)).toBe(0)   // mostly down: not a swipe
  })
})
