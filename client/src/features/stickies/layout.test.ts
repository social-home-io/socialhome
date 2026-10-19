import { describe, expect, it } from 'vitest'
import { BOARD_H, BOARD_W, NOTE_H, NOTE_W, displayPositions, freeSpot, type Spot } from './layout'

const overlaps = (a: Spot, b: Spot) =>
  Math.abs(a.x - b.x) < NOTE_W && Math.abs(a.y - b.y) < NOTE_H
const inside = (p: Spot) =>
  p.x >= 0 && p.y >= 0 && p.x <= BOARD_W - NOTE_W && p.y <= BOARD_H - NOTE_H

describe('freeSpot', () => {
  it('fills the board without overlap while there is room', () => {
    const taken: Spot[] = []
    for (let i = 0; i < 12; i++) {
      const s = freeSpot(taken)
      expect(inside(s)).toBe(true)
      expect(taken.some(t => overlaps(s, t))).toBe(false)
      taken.push(s)
    }
  })

  it('skips a cell a hand-placed note covers', () => {
    const first = freeSpot([])
    const nudged = { x: first.x + 30, y: first.y + 10 }
    expect(overlaps(freeSpot([nudged]), nudged)).toBe(false)
  })

  it('on a full board still stays inside and fans out', () => {
    const taken: Spot[] = []
    for (let x = 0; x < 1000; x += 50) for (let y = 0; y < 700; y += 50) taken.push({ x, y })
    const a = freeSpot(taken)
    const b = freeSpot([...taken, a])
    expect(inside(a) && inside(b)).toBe(true)
    expect(a).not.toEqual(b)
  })
})

const note = (id: string, x: number, y: number, created_at = `2026-10-19T08:00:0${id}`) =>
  ({ id, position_x: x, position_y: y, created_at })

describe('displayPositions', () => {
  it('keeps notes that do not stack where they are stored', () => {
    const d = displayPositions([note('1', 50, 56), note('2', 400, 70), note('3', 200, 350)])
    expect(d.get('1')).toEqual({ x: 50, y: 56 })
    expect(d.get('2')).toEqual({ x: 400, y: 70 })
    expect(d.get('3')).toEqual({ x: 200, y: 350 })
  })

  // Three notes stored at (≈0, ≈0) used to draw as one: "3 notes" over a
  // board showing a single sticky.
  it('fans out notes stored on the same spot so each one shows', () => {
    const d = displayPositions([note('1', 0.05, 0.08), note('2', 0.4, 0.1), note('3', 0.2, 0.5)])
    const spots = ['1', '2', '3'].map(id => d.get(id)!)
    expect(spots[0]).toEqual({ x: 0.05, y: 0.08 })   // the oldest keeps its spot
    for (let i = 0; i < spots.length; i++) {
      expect(inside(spots[i])).toBe(true)
      for (let j = 0; j < i; j++) {
        expect(Math.abs(spots[i].x - spots[j].x) >= 24 || Math.abs(spots[i].y - spots[j].y) >= 24).toBe(true)
      }
    }
  })

  it('the oldest note keeps its spot whatever order the rows arrive in', () => {
    const d = displayPositions([note('2', 300, 300), note('1', 300, 300)])
    expect(d.get('1')).toEqual({ x: 300, y: 300 })
    expect(d.get('2')).not.toEqual({ x: 300, y: 300 })
  })

  it('a hidden note is drawn clear of every other note, younger ones included', () => {
    // b is hidden under a; c (younger) sits on the first grid cell.
    const d = displayPositions([note('1', 300, 300), note('2', 300, 300), note('3', 20, 20)])
    const b = d.get('2')!
    for (const other of [d.get('1')!, d.get('3')!]) expect(overlaps(b, other)).toBe(false)
    expect(d.get('3')).toEqual({ x: 20, y: 20 })
  })

  it('a stacked note at the bottom-right corner wraps inside the board', () => {
    const d = displayPositions([note('1', 1000, 700), note('2', 1000, 700)])
    expect(inside(d.get('2')!)).toBe(true)
  })
})
