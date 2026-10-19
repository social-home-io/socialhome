/**
 * Sticky-board placement in the normalised board space (1000 × 700).
 *
 * - ``freeSpot``: where a new note goes — the first note-sized grid cell
 *   no existing note overlaps, else a diagonal cascade. Mirrors
 *   ``free_sticky_spot`` in ``socialhome/domain/sticky.py`` (the server
 *   uses it for a note created without a position).
 * - ``displayPositions``: where each note is DRAWN. A note stored on
 *   (almost) the same spot as an older one would be hidden under it, so
 *   it is drawn on the first free spot instead. Stored positions are
 *   never rewritten — the note keeps its drawn spot until someone moves
 *   it, and a move stores wherever it is dropped.
 */

export const BOARD_W = 1000
export const BOARD_H = 700
/** A note's nominal size in board units (18 % × 20 %). */
export const NOTE_W = BOARD_W * 0.18
export const NOTE_H = BOARD_H * 0.2

const MARGIN = 20
const GAP = 20
/** Diagonal step: enough to show a covered note's grip and first line. */
const CASCADE = 30
/** Notes whose top-left corners are this close (both axes) are stacked:
 *  the later one would cover the older one's grip and text. */
const STACK_EPS = 24

export interface Spot { x: number; y: number }

const MAX_X = BOARD_W - NOTE_W
const MAX_Y = BOARD_H - NOTE_H

export function freeSpot(taken: readonly Spot[]): Spot {
  const free = (x: number, y: number) =>
    taken.every(n => Math.abs(x - n.x) >= NOTE_W || Math.abs(y - n.y) >= NOTE_H)
  for (let y = MARGIN; y <= MAX_Y; y += NOTE_H + GAP) {
    for (let x = MARGIN; x <= MAX_X; x += NOTE_W + GAP) {
      if (free(x, y)) return { x, y }
    }
  }
  const step = taken.length * CASCADE
  return { x: MARGIN + (step % (MAX_X - MARGIN)), y: MARGIN + (step % (MAX_Y - MARGIN)) }
}

/** ``created_at`` may be missing on an optimistic row not saved yet. */
interface Placeable { id: string; position_x: number; position_y: number; created_at?: string }

/** Drawn position per note id. Every note keeps its stored spot unless
 *  an older note sits (almost) exactly there; such a hidden note is drawn
 *  on the first spot free of every other note (``freeSpot``), or steps
 *  down-right on a full board. */
export function displayPositions(notes: readonly Placeable[]): Map<string, Spot> {
  const order = [...notes].sort((a, b) =>
    (a.created_at ?? '').localeCompare(b.created_at ?? '') || a.id.localeCompare(b.id))
  const drawn: Spot[] = []
  const out = new Map<string, Spot>()
  const stacked = (p: Spot) =>
    drawn.some(d => Math.abs(d.x - p.x) < STACK_EPS && Math.abs(d.y - p.y) < STACK_EPS)
  // Pass 1: notes that show where they are stored stay there. Compare
  // where notes are DRAWN: CSS pulls a note stored past the edge inwards,
  // so (1000, 700) and (820, 560) sit on the same spot.
  const hidden: { n: Placeable; base: Spot }[] = []
  for (const n of order) {
    const base = { x: Math.min(n.position_x, MAX_X), y: Math.min(n.position_y, MAX_Y) }
    if (stacked(base)) {
      hidden.push({ n, base })
    } else {
      drawn.push(base)
      out.set(n.id, { x: n.position_x, y: n.position_y })
    }
  }
  // Pass 2: each hidden note goes to a spot clear of all of them.
  for (const { n, base } of hidden) {
    let p = freeSpot(drawn)
    for (let k = 1; stacked(p) && k <= 40; k++) {
      p = {
        x: (base.x + k * CASCADE) % (MAX_X + 1),
        y: (base.y + k * CASCADE) % (MAX_Y + 1),
      }
    }
    drawn.push(p)
    out.set(n.id, p)
  }
  return out
}
