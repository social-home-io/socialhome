/**
 * Pure layout maths for the timetable grid.
 *
 * Two layouts:
 *
 *  * **Periods** — the classic school table. Chosen automatically when
 *    every visible day has the identical (start, end, kind) slot
 *    sequence; rows are the slots, a break every day shares becomes a
 *    thin spanning band, and consecutive identical lessons merge into
 *    one ``rowSpan`` cell (a double "Mathe" 1.+2.).
 *  * **Timeline** — the general case: blocks positioned on a shared
 *    minute axis so a 07:15 lesson on Tuesday visibly sits higher than
 *    Monday's 08:00. Long empty stretches (> 60 min on every day)
 *    compress into a 16 px "⋯" band.
 */
import type { Timetable, TimetableEntry, TimetableEntryKind } from '@/types'
import { fromMinutes, toMinutes } from './time'

/** A new entry's weekday / start / end, as the grid hands it to the
 *  EntryDialog. */
export interface EntryPrefill {
  weekday: number
  start: string
  end: string
}

/** Entries on ``weekday``, by start time. */
export function dayEntries(tt: Timetable, weekday: number): TimetableEntry[] {
  return tt.entries
    .filter(e => e.weekday === weekday)
    .sort((a, b) => toMinutes(a.start) - toMinutes(b.start))
}

/** 1-based position of ``entry`` among the lessons of its day — the
 *  "3rd lesson" of the aria-label; ``null`` for a break. */
const lessonNumbers = new WeakMap<Timetable, Map<string, number>>()

export function lessonNumber(tt: Timetable, entry: TimetableEntry): number | null {
  if (entry.kind !== 'lesson') return null
  let nums = lessonNumbers.get(tt)
  if (!nums) {
    // One sort per timetable version (a new version is a new object).
    const map = new Map<string, number>()
    for (const d of new Set(tt.entries.map(e => e.weekday))) {
      dayEntries(tt, d).filter(e => e.kind === 'lesson').forEach((e, i) => map.set(e.id, i + 1))
    }
    lessonNumbers.set(tt, map)
    nums = map
  }
  return nums.get(entry.id) ?? null
}

const slotKey = (e: { start: string; end: string; kind: TimetableEntryKind }) =>
  `${e.start}-${e.end}-${e.kind}`

/** True when every day in ``days`` is non-empty and has the same slot
 *  sequence — the automatic choice of the Periods layout. */
export function isPeriodsEligible(tt: Timetable, days: readonly number[]): boolean {
  if (days.length === 0) return false
  const seqs = days.map(d => dayEntries(tt, d).map(slotKey).join('|'))
  return seqs[0] !== '' && seqs.every(s => s === seqs[0])
}

export interface PeriodCell {
  entry: TimetableEntry
  /** The run's latest entry — what the next slot must continue. */
  last: TimetableEntry
  rowSpan: number
  /** End of the last merged slot (``entry.end`` when not merged). */
  end: string
}

export interface PeriodRow {
  key: string
  start: string
  end: string
  kind: TimetableEntryKind
  /** "1." — the entry's own label, else the running lesson number. */
  label: string
  /** A break every visible day shares: one thin spanning row. */
  band: boolean
  /** Band title (the first day's break title). */
  title: string | null
  /** Per weekday: a cell, ``null`` (nothing there — an empty cell) or
   *  ``'merged'`` (covered by a ``rowSpan`` from above). */
  cells: Record<number, PeriodCell | null | 'merged'>
}

/**
 * Can ``b`` continue the lesson ``a`` as one block (a double "Mathe")?
 * Both are lessons with the same (non-empty) title, room and icon, and
 * ``b`` starts back-to-back with ``a`` or after at most ``maxGap``
 * minutes (the timetable's ``gap_minutes``). The callers make sure no
 * break sits between them. Shared by the Periods table (``rowSpan``)
 * and the phone day view.
 */
export function canMerge(a: TimetableEntry, b: TimetableEntry, maxGap: number): boolean {
  const title = (s: string | null) => (s ?? '').trim().toLowerCase()
  const gap = toMinutes(b.start) - toMinutes(a.end)
  return a.kind === 'lesson' && b.kind === 'lesson'
    && title(a.title) !== '' && title(a.title) === title(b.title)
    && (a.room ?? '') === (b.room ?? '') && (a.icon ?? '') === (b.icon ?? '')
    && gap >= 0 && gap <= maxGap
}

/** A run of merged entries: the first one plus the span's end. */
export interface MergedRun {
  entry: TimetableEntry
  end: string
  count: number
}

/** A day's entries (in time order) with consecutive mergeable lessons
 *  collapsed into one run — the phone day view's rows. */
export function mergeRuns(entries: readonly TimetableEntry[], maxGap: number): MergedRun[] {
  const out: (MergedRun & { last: TimetableEntry })[] = []
  for (const e of entries) {
    const prev = out[out.length - 1]
    if (prev && canMerge(prev.last, e, maxGap)) {
      prev.end = e.end
      prev.last = e
      prev.count += 1
    } else {
      out.push({ entry: e, end: e.end, count: 1, last: e })
    }
  }
  return out.map(({ entry, end, count }) => ({ entry, end, count }))
}

/** Rows of the Periods table — the union of every visible day's
 *  slots, so a forced Periods view of uneven days still has a row for
 *  each (with empty cells where a day has nothing). */
export function periodRows(tt: Timetable, days: readonly number[]): PeriodRow[] {
  const bySlot = new Map<string, { start: string; end: string; kind: TimetableEntryKind;
    perDay: Map<number, TimetableEntry> }>()
  for (const d of days) {
    for (const e of dayEntries(tt, d)) {
      const k = slotKey(e)
      let slot = bySlot.get(k)
      if (!slot) {
        slot = { start: e.start, end: e.end, kind: e.kind, perDay: new Map() }
        bySlot.set(k, slot)
      }
      slot.perDay.set(d, e)
    }
  }
  const slots = [...bySlot.entries()].sort(([, a], [, b]) =>
    toMinutes(a.start) - toMinutes(b.start) || toMinutes(a.end) - toMinutes(b.end))

  const rows: PeriodRow[] = []
  const open = new Map<number, PeriodCell>()
  let lessonNo = 0
  for (const [key, slot] of slots) {
    const band = slot.kind === 'break' && days.every(d => slot.perDay.has(d))
    const first = days.map(d => slot.perDay.get(d)).find(Boolean)!
    if (slot.kind === 'lesson') lessonNo += 1
    const cells: PeriodRow['cells'] = {}
    for (const d of days) {
      const e = slot.perDay.get(d) ?? null
      const above = open.get(d)
      if (band || !e) {
        open.delete(d)
        cells[d] = band ? 'merged' : null
        continue
      }
      if (above && canMerge(above.last, e, tt.defaults.gap_minutes)) {
        above.rowSpan += 1
        above.end = e.end
        above.last = e
        cells[d] = 'merged'
        continue
      }
      const cell = { entry: e, last: e, rowSpan: 1, end: e.end }
      open.set(d, cell)
      cells[d] = cell
    }
    rows.push({
      key,
      start: slot.start,
      end: slot.end,
      kind: slot.kind,
      label: first.label ?? (slot.kind === 'lesson' ? `${lessonNo}.` : ''),
      band,
      title: band ? first.title : null,
      cells,
    })
  }
  return rows
}

// ─── Timeline geometry ───────────────────────────────────────────────

/** Pixels per minute at rest — 45 minutes ≈ 54 px. */
export const BASE_PPM = 1.2
/** Never scale beyond this to make short lessons tall. */
export const MAX_PPM = 2
/** Minimum height of a lesson block. */
export const MIN_BLOCK_PX = 36
/** Minimum height of a break band. */
export const MIN_BREAK_PX = 12
/** Axis padding before the first / after the last slot. */
export const AXIS_PAD_MIN = 15
/** Empty stretches longer than this (on every day) are compressed… */
export const COMPRESS_OVER_MIN = 60
/** …into a band this tall. */
export const COMPRESSED_PX = 16

export interface AxisSegment {
  from: number
  to: number
  compressed: boolean
}

export interface AxisTick {
  minute: number
  y: number
  major: boolean
}

export interface TimelineGeometry {
  /** Floor of a lesson block's height (taller in Picture view). */
  minBlock: number
  axisStart: number
  axisEnd: number
  ppm: number
  segments: AxisSegment[]
  height: number
  ticks: AxisTick[]
  /** Minute → y (px from the axis top). */
  y: (minute: number) => number
  /** y → minute (a click inside a compressed band maps to its start). */
  minuteAt: (y: number) => number
}

const floor30 = (m: number) => Math.floor(m / 30) * 30
const ceil30 = (m: number) => Math.ceil(m / 30) * 30

export function timelineGeometry(
  tt: Timetable,
  days: readonly number[],
  opts: { minBlock?: number } = {},
): TimelineGeometry {
  const minBlock = opts.minBlock ?? MIN_BLOCK_PX
  const visible = tt.entries.filter(e => days.includes(e.weekday))
  const spans = visible
    .map(e => ({ start: toMinutes(e.start), end: toMinutes(e.end), kind: e.kind }))
    .sort((a, b) => a.start - b.start)

  let axisStart: number
  let axisEnd: number
  if (spans.length === 0) {
    axisStart = floor30(toMinutes(tt.defaults.day_start) - AXIS_PAD_MIN)
    axisEnd = axisStart + 6 * 60
  } else {
    axisStart = floor30(Math.min(...spans.map(s => s.start)) - AXIS_PAD_MIN)
    axisEnd = ceil30(Math.max(...spans.map(s => s.end)) + AXIS_PAD_MIN)
  }
  axisStart = Math.max(0, axisStart)
  axisEnd = Math.min(24 * 60, axisEnd)

  const lessonLens = spans.filter(s => s.kind === 'lesson').map(s => s.end - s.start)
  const shortest = lessonLens.length ? Math.min(...lessonLens) : 45
  const ppm = Math.min(MAX_PPM, Math.max(BASE_PPM, minBlock / shortest))

  // Busy intervals across every visible day, merged.
  const busy: { start: number; end: number }[] = []
  for (const s of spans) {
    const last = busy[busy.length - 1]
    if (last && s.start <= last.end) last.end = Math.max(last.end, s.end)
    else busy.push({ start: s.start, end: s.end })
  }
  const segments: AxisSegment[] = []
  let cursor = axisStart
  for (let i = 0; i + 1 < busy.length; i++) {
    if (busy[i + 1].start - busy[i].end <= COMPRESS_OVER_MIN) continue
    const from = ceil30(busy[i].end + AXIS_PAD_MIN)
    const to = floor30(busy[i + 1].start - AXIS_PAD_MIN)
    if (to <= from) continue
    segments.push({ from: cursor, to: from, compressed: false })
    segments.push({ from, to, compressed: true })
    cursor = to
  }
  segments.push({ from: cursor, to: axisEnd, compressed: false })

  const segHeight = (s: AxisSegment) => s.compressed ? COMPRESSED_PX : (s.to - s.from) * ppm

  const y = (minute: number): number => {
    let acc = 0
    for (const s of segments) {
      if (minute <= s.to) {
        const within = Math.max(0, minute - s.from)
        return acc + (s.compressed ? (within / (s.to - s.from)) * COMPRESSED_PX : within * ppm)
      }
      acc += segHeight(s)
    }
    return acc
  }

  const minuteAt = (py: number): number => {
    let acc = 0
    for (const s of segments) {
      const h = segHeight(s)
      if (py <= acc + h) return s.compressed ? s.from : s.from + (py - acc) / ppm
      acc += h
    }
    return axisEnd
  }

  const ticks: AxisTick[] = []
  for (let m = ceil30(axisStart); m <= axisEnd; m += 30) {
    if (segments.some(s => s.compressed && m > s.from && m < s.to)) continue
    ticks.push({ minute: m, y: y(m), major: m % 60 === 0 })
  }

  return {
    minBlock, axisStart, axisEnd, ppm, segments,
    height: segments.reduce((a, s) => a + segHeight(s), 0),
    ticks, y, minuteAt,
  }
}

/** Top / height of an entry's block on the timeline. */
export function blockBox(
  geo: TimelineGeometry, entry: Pick<TimetableEntry, 'start' | 'end' | 'kind'>,
): { top: number; height: number } {
  const top = geo.y(toMinutes(entry.start))
  const raw = geo.y(toMinutes(entry.end)) - top
  const min = entry.kind === 'lesson' ? geo.minBlock : MIN_BREAK_PX
  return { top, height: Math.max(min, raw) }
}

/** Where "+ Add lesson" on a day starts: after the day's last entry
 *  plus ``gap_minutes``, or at ``day_start`` on an empty day. */
export function nextStartFor(tt: Timetable, weekday: number): string {
  const entries = dayEntries(tt, weekday)
  if (entries.length === 0) return tt.defaults.day_start
  const lastEnd = Math.max(...entries.map(e => toMinutes(e.end)))
  return fromMinutes(lastEnd + tt.defaults.gap_minutes)
}

/** A new entry's prefill: ``start`` plus ``defaults.lesson_minutes``
 *  (clamped so it never crosses midnight). */
export function prefillAt(tt: Timetable, weekday: number, startMin: number): EntryPrefill {
  const len = tt.defaults.lesson_minutes
  const start = Math.max(0, Math.min(startMin, 23 * 60 + 59 - len))
  return { weekday, start: fromMinutes(start), end: fromMinutes(start + len) }
}
