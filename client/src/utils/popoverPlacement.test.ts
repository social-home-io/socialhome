import { describe, it, expect } from 'vitest'
import {
  POPOVER_EDGE, POPOVER_GAP, POPOVER_MIN_HEIGHT,
  clampHorizontally, pinFixed, placeVertically, visibleArea,
} from './popoverPlacement'

describe('visibleArea', () => {
  it('is the visual viewport when there is one (phone keyboard up)', () => {
    const win = {
      innerHeight: 844, innerWidth: 390,
      visualViewport: { offsetTop: 12, height: 508, width: 390 },
    } as unknown as Window
    expect(visibleArea(win)).toEqual({ top: 12, bottom: 520, width: 390 })
  })

  it('falls back to the layout viewport', () => {
    const win = { innerHeight: 720, innerWidth: 1280, visualViewport: null } as unknown as Window
    expect(visibleArea(win)).toEqual({ top: 0, bottom: 720, width: 1280 })
  })
})

describe('placeVertically', () => {
  const area = { top: 0, bottom: 508, width: 390 }

  it('opens below a field near the top when the list fits', () => {
    const v = placeVertically({ top: 80, bottom: 120 }, area, 280)
    expect(v.placement).toBe('below')
    expect(v.top).toBe(120 + POPOVER_GAP)
    expect(v.maxHeight).toBe(508 - 120 - POPOVER_GAP - POPOVER_EDGE)
  })

  it('flips above a field sitting on the keyboard (chat composer)', () => {
    const v = placeVertically({ top: 464, bottom: 504 }, area, 280)
    expect(v.placement).toBe('above')
    expect(v.top).toBe(464 - POPOVER_GAP)
    expect(v.maxHeight).toBe(464 - POPOVER_GAP - POPOVER_EDGE)
  })

  it('caps the list to the room above on a short screen', () => {
    // 360×640 with the keyboard up: ~300 px visible.
    const v = placeVertically({ top: 220, bottom: 260 }, { top: 0, bottom: 304, width: 360 }, 280)
    expect(v.placement).toBe('above')
    expect(v.maxHeight).toBe(220 - POPOVER_GAP - POPOVER_EDGE)
  })

  it('stays below when there is no more room above', () => {
    const v = placeVertically({ top: 20, bottom: 60 }, { top: 0, bottom: 200, width: 360 }, 280)
    expect(v.placement).toBe('below')
  })

  it('measures from the visual viewport top, not 0', () => {
    const v = placeVertically({ top: 400, bottom: 440 }, { top: 300, bottom: 808, width: 390 }, 280)
    expect(v.placement).toBe('below')
    expect(v.maxHeight).toBe(808 - 440 - POPOVER_GAP - POPOVER_EDGE)
  })

  it('never squeezes below the minimum', () => {
    const v = placeVertically({ top: 10, bottom: 290 }, { top: 0, bottom: 300, width: 360 }, 280)
    expect(v.maxHeight).toBe(POPOVER_MIN_HEIGHT)
  })
})

describe('clampHorizontally', () => {
  const area = { top: 0, bottom: 600, width: 360 }
  it('keeps the start when it fits', () => {
    expect(clampHorizontally(40, 200, area)).toBe(40)
  })
  it('pulls a list back from the right edge', () => {
    expect(clampHorizontally(300, 200, area)).toBe(360 - 200 - POPOVER_EDGE)
  })
  it('never starts left of the edge margin', () => {
    expect(clampHorizontally(-30, 200, area)).toBe(POPOVER_EDGE)
  })
})

describe('pinFixed', () => {
  it('cancels a containing block offset', () => {
    const el = document.createElement('div')
    // Lands 10 px right / 50 px low of where it was asked to (a filtered ancestor).
    el.getBoundingClientRect = () => {
      const left = parseFloat(el.style.left) + 10
      const top = parseFloat(el.style.top) + 50
      return { left, top, bottom: top + 100, right: left + 200 } as DOMRect
    }
    pinFixed(el, 20, 300, 'below')
    expect(el.style.left).toBe('10px')
    expect(el.style.top).toBe('250px')
  })

  it('aligns the bottom edge for above placement', () => {
    const el = document.createElement('div')
    el.getBoundingClientRect = () => {
      const top = parseFloat(el.style.top)
      return { left: parseFloat(el.style.left), top: top - 100, bottom: top } as DOMRect
    }
    pinFixed(el, 20, 300, 'above')
    expect(el.style.top).toBe('300px')
  })
})
