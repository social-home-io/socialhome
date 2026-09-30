/**
 * Day builder maths — the pure half of ``DayBuilder``.
 *
 * A day is a start time plus an ordered list of rows (lesson / break,
 * a length in minutes, an optional title). Times are derived, never
 * stored: each row starts where the previous one ended, plus
 * ``gap`` minutes between two lessons (a break replaces the gap, as in
 * the backend's school template). So changing one length ripples
 * through every row after it.
 *
 * Lessons are numbered 1., 2., … skipping breaks and the optional
 * early "0." lesson (added with "+ Add before"). Saving keeps the
 * subject (title / icon / colour / room / teacher / note) of the entry
 * a row came from, or — for a new row — of an old entry with the same
 * start time and kind, so fixing times never wipes the subjects.
 */
import type { SlotInput } from '@/store/timetables'
import type { Timetable, TimetableEntry, TimetableEntryKind } from '@/types'
import { normalizeSubject } from './colors'
import { suggestIcon } from './icons'
import { dayEntries } from './layout'
import { fromMinutes, toMinutes } from './time'

export interface BuilderRow {
  key: string
  kind: TimetableEntryKind
  minutes: number
  title: string
  /** The "0." lesson before the first one. */
  early: boolean
  /** The entry this row was built from (its subject is kept). */
  origin: TimetableEntry | null
}

export interface TimedRow extends BuilderRow {
  start: number
  end: number
  /** "1." / "0." for lessons, ``null`` for breaks. */
  label: string | null
  error: 'midnight' | 'short' | 'overlap' | null
}

export const DEFAULT_BREAK_MINUTES = 20
const LAST_MINUTE = 23 * 60 + 59

let seq = 0
export const rowKey = () => `r${++seq}`

export function newRow(
  kind: TimetableEntryKind, minutes: number, title = '', early = false,
): BuilderRow {
  return { key: rowKey(), kind, minutes, title, early, origin: null }
}

/** Derive start / end / label / error for every row. */
export function timeRows(rows: readonly BuilderRow[], startMin: number, gap: number): TimedRow[] {
  const out: TimedRow[] = []
  let t = startMin
  let n = 0
  let prevEnd = -Infinity
  rows.forEach((row, i) => {
    if (i > 0 && row.kind === 'lesson' && rows[i - 1].kind === 'lesson') t += gap
    const start = t
    const end = t + row.minutes
    t = end
    const label = row.kind !== 'lesson' ? null : row.early ? '0.' : `${++n}.`
    const error = !Number.isFinite(row.minutes) || row.minutes < 5 ? 'short'
      : start < 0 || end > LAST_MINUTE ? 'midnight'
        : start < prevEnd ? 'overlap'
          : null
    prevEnd = Math.max(prevEnd, end)
    out.push({ ...row, start, end, label, error })
  })
  return out
}

/** The six-lesson school day (breaks after the 2nd and 4th). */
export function defaultRows(tt: Pick<Timetable, 'defaults'>, breakTitle: string): BuilderRow[] {
  const len = tt.defaults.lesson_minutes
  return [
    newRow('lesson', len), newRow('lesson', len),
    newRow('break', 20, breakTitle),
    newRow('lesson', len), newRow('lesson', len),
    newRow('break', 15, breakTitle),
    newRow('lesson', len), newRow('lesson', len),
  ]
}

export interface BuilderState {
  start: string
  lessonMinutes: number
  gap: number
  rows: BuilderRow[]
}

/** Rows (and start / gap) from the day's entries; the school default
 *  for an empty day. The gap is the one the day already uses between
 *  lessons when that is uniform, so opening and saving changes
 *  nothing. */
export function initialState(tt: Timetable, weekday: number, breakTitle: string): BuilderState {
  const entries = dayEntries(tt, weekday)
  if (entries.length === 0) {
    return {
      start: tt.defaults.day_start,
      lessonMinutes: tt.defaults.lesson_minutes,
      gap: tt.defaults.gap_minutes,
      rows: defaultRows(tt, breakTitle),
    }
  }
  const gaps = new Set<number>()
  entries.forEach((e, i) => {
    const prev = entries[i - 1]
    if (prev && prev.kind === 'lesson' && e.kind === 'lesson') {
      gaps.add(toMinutes(e.start) - toMinutes(prev.end))
    }
  })
  const gap = gaps.size === 1 ? Math.max(0, [...gaps][0]) : tt.defaults.gap_minutes
  const lessons = entries.filter(e => e.kind === 'lesson')
  const lengths = new Set(lessons.map(e => toMinutes(e.end) - toMinutes(e.start)))
  return {
    start: entries[0].start,
    lessonMinutes: lengths.size === 1 ? [...lengths][0] : tt.defaults.lesson_minutes,
    gap,
    rows: entries.map((e, i) => ({
      key: rowKey(),
      kind: e.kind,
      minutes: toMinutes(e.end) - toMinutes(e.start),
      title: e.title ?? '',
      early: i === 0 && e.kind === 'lesson' && e.label === '0.',
      origin: e,
    })),
  }
}

/** "+ Lesson" / "+ Break": appended after the last row. */
export function appendRow(rows: BuilderRow[], row: BuilderRow): BuilderRow[] {
  return [...rows, row]
}

/** "+ Add before": an early "0." lesson ending ``gap`` before the
 *  first slot — the start time moves back, every other row keeps its
 *  time. ``null`` when there already is one. */
export function addBefore(state: BuilderState): BuilderState | null {
  if (state.rows[0]?.early) return null
  const first = state.rows[0]
  const len = state.lessonMinutes
  const shift = len + (first?.kind === 'lesson' ? state.gap : 0)
  return {
    ...state,
    start: fromMinutes(toMinutes(state.start) - shift),
    rows: [newRow('lesson', len, '', true), ...state.rows],
  }
}

export function moveRow(rows: BuilderRow[], index: number, dir: -1 | 1): BuilderRow[] {
  const to = index + dir
  if (to < 0 || to >= rows.length) return rows
  const next = [...rows]
  ;[next[index], next[to]] = [next[to], next[index]]
  // Only the first row can be the early "0." lesson.
  return next.map((r, i) => (r.early && i !== 0 ? { ...r, early: false } : r))
}

const subjectOf = (e: TimetableEntry | null | undefined) => e
  ? { icon: e.icon, color: e.color, room: e.room, teacher: e.teacher, note: e.note }
  : { icon: null, color: null, room: null, teacher: null, note: null }

/** The slots ``POST …/generate`` gets. A row keeps its origin entry's
 *  subject; a new row takes the subject of an old entry with the same
 *  start and kind. A retitled lesson drops the old icon / colour (the
 *  icon follows the new title, as in the EntryDialog). */
export function toSlots(timed: readonly TimedRow[], old: readonly TimetableEntry[]): SlotInput[] {
  const used = new Set(timed.map(r => r.origin?.id).filter(Boolean))
  return timed.map(r => {
    let src = r.origin && r.origin.kind === r.kind ? r.origin : null
    if (!src) {
      src = old.find(e => !used.has(e.id) && e.kind === r.kind
        && toMinutes(e.start) === r.start) ?? null
      if (src) used.add(src.id)
    }
    const title = r.title.trim() || (r.origin ? '' : src?.title?.trim() ?? '')
    let subject = subjectOf(src)
    if (src && normalizeSubject(title) !== normalizeSubject(src.title ?? '')) {
      subject = { ...subject, icon: suggestIcon(title), color: null }
    }
    if (!src) subject = { ...subject, icon: suggestIcon(title) }
    return {
      start: fromMinutes(r.start),
      end: fromMinutes(r.end),
      kind: r.kind,
      label: r.label,
      title: title || null,
      ...subject,
    }
  })
}
