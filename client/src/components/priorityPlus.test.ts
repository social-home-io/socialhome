/**
 * Priority+ fitting: as many items as fit, in order, the More button
 * always kept; never reordered.
 */
import { describe, it, expect } from 'vitest'
import { fitCount } from './priorityPlus'

// Feed · Chat 4 · Calendar · Tasks · Pages · Gallery · Stickies
const EN = [56, 84, 92, 66, 70, 76, 80]
// Feed · Chat 4 · Kalender · Aufgaben · Seiten · Galerie · Notizzettel
const DE = [56, 84, 98, 96, 72, 76, 112]
const MORE = 84
const GAP = 4

describe('fitCount', () => {
  it('a phone row (≈ 300 px beside the gear) fits Feed · Chat · Calendar … and More', () => {
    // 84 more + 56 + 84 + 92 + 3 gaps = 328 → 3 fit in 340, Tasks would need 398.
    expect(fitCount(EN, 340, MORE, GAP)).toBe(3)
    expect(fitCount(EN, 400, MORE, GAP)).toBe(4)
  })

  it('German labels are wider: one fewer section fits in the same row', () => {
    expect(fitCount(EN, 400, MORE, GAP)).toBe(4)
    expect(fitCount(DE, 400, MORE, GAP)).toBe(3)
  })

  it('a desktop row fits every section', () => {
    expect(fitCount(EN, 1000, MORE, GAP)).toBe(EN.length)
  })

  it('never shows fewer than one (the feed anchors the row)', () => {
    expect(fitCount(EN, 10, MORE, GAP)).toBe(1)
    expect(fitCount([], 10, MORE, GAP)).toBe(0)
  })

  it('a badge that widens an early item pushes the cut forward, never reorders', () => {
    const withBadge = [...EN]
    withBadge[1] += 30
    expect(fitCount(withBadge, 400, MORE, GAP)).toBe(3)
  })

  it('exact fit counts', () => {
    // 84 + (4+56) + (4+84) = 232
    expect(fitCount(EN, 232, MORE, GAP)).toBe(2)
    expect(fitCount(EN, 231, MORE, GAP)).toBe(1)
  })
})
