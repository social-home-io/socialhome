import { describe, it, expect } from 'vitest'
import {
  CX, CY, RADIUS, emptySlots, layoutConstellation, linkPath, slotAngle,
} from './constellationLayout'

const dist = (p: { x: number; y: number }) => Math.hypot(p.x - CX, p.y - CY)

describe('slotAngle', () => {
  it('with a GFS: households share the 290° arc from 64°, leaving the GFS wedge', () => {
    expect(slotAngle(0, 1, true)).toBe(64 + 145)
    expect(slotAngle(0, 2, true)).toBeCloseTo(136.5)
    expect(slotAngle(1, 2, true)).toBeCloseTo(281.5)
    for (let n = 1; n <= 6; n++) {
      for (let i = 0; i < n; i++) {
        const a = slotAngle(i, n, true)
        // The GFS tile sits around 34° — never under a household.
        expect(a > 64 && a < 354).toBe(true)
      }
    }
  })

  it('without a GFS: a full circle from -150°', () => {
    expect(slotAngle(0, 3, false)).toBe(-150)
    expect(slotAngle(1, 3, false)).toBe(-30)
    expect(slotAngle(2, 3, false)).toBe(90)
  })
})

describe('layoutConstellation', () => {
  it('places every node on r = 72 in the given order', () => {
    const { nodes, more } = layoutConstellation(['a', 'b', 'c'], true)
    expect(more).toBeNull()
    expect(nodes.map((n) => n.item)).toEqual(['a', 'b', 'c'])
    for (const n of nodes) expect(dist(n)).toBeCloseTo(RADIUS, 0)
  })

  it('caps at 6 slots: 5 households + "+N" where N = total − 5', () => {
    const items = Array.from({ length: 9 }, (_, i) => i)
    const { nodes, more } = layoutConstellation(items, true)
    expect(nodes.map((n) => n.item)).toEqual([0, 1, 2, 3, 4])
    expect(more!.count).toBe(4)
    expect(dist(more!)).toBeCloseTo(RADIUS, 0)
  })

  it('exactly 6 households all fit without a "+N"', () => {
    const { nodes, more } = layoutConstellation([1, 2, 3, 4, 5, 6], false)
    expect(nodes).toHaveLength(6)
    expect(more).toBeNull()
  })
})

describe('emptySlots / linkPath', () => {
  it('three first-run slots at -150°, -30°, 90°', () => {
    const slots = emptySlots()
    expect(slots).toHaveLength(3)
    expect(slots[2]).toEqual({ x: CX, y: CY + RADIUS })
  })

  it('a link sags 8 below the midpoint by default', () => {
    expect(linkPath(124, 164)).toBe('M124,92 Q124,136 124,164')
    expect(linkPath(210, 150, -6)).toBe('M124,92 Q167,115 210,150')
  })
})
