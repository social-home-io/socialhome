/**
 * Pure helpers of the week validity picker: the window of months it
 * shows (the school year, or 12 months), the weeks in it (by their
 * anchor — first day — in the timetable's week start) grouped by the
 * month of that anchor day, and the chip labels.
 */
import { t } from '@/i18n/i18n'
import { isoWeekNumber, type WeekStart } from '@/utils/week'
import { addDays, dateRange, parseIsoDate, shortDate, weekAnchor } from './dates'

/** Does the week starting ``anchor`` overlap [from, until]? */
export function weekInRange(anchor: string, from: string | null, until: string | null): boolean {
  const last = addDays(anchor, 6)
  return (from === null || last >= from) && (until === null || anchor <= until)
}

/** "W41" (Monday start — ISO week) / "Oct 4" (Sunday start — the date). */
export function chipLabel(anchor: string, ws: WeekStart): string {
  return ws === 0
    ? t('timetable.weeks.kw', { n: String(isoWeekNumber(parseIsoDate(anchor))) })
    : shortDate(anchor)
}

export type ChipState = 'school' | 'holiday' | 'outside'

/** "Week 41, Oct 5 – 11, 2026, school week". */
export function chipAria(anchor: string, ws: WeekStart, state: ChipState): string {
  const range = dateRange(anchor, addDays(anchor, 6), true)
  const what = t(`timetable.weeks.state_${state}`)
  return ws === 0
    ? t('timetable.weeks.chip_aria', { n: String(isoWeekNumber(parseIsoDate(anchor))), range, state: what })
    : t('timetable.weeks.chip_aria_date', { range, state: what })
}

// ─── The window of months the dialog shows ───────────────────────────

/** ``"YYYY-MM"`` */
export type Month = string

export function addMonths(m: Month, n: number): Month {
  const [y, mo] = m.split('-').map(Number)
  const total = y * 12 + (mo - 1) + n
  return `${Math.floor(total / 12)}-${String((total % 12) + 1).padStart(2, '0')}`
}

/** The longest window shown at once. */
export const MAX_WINDOW_MONTHS = 18

export interface WeekWindow {
  start: Month
  end: Month
  /** The school year itself (both ends set, not shifted): months with
   *  no week in the valid range are hidden. */
  school: boolean
}

/** The months shown: the school year (the month of ``from``'s week
 *  through the month of ``until``'s week) when both ends are set, else
 *  12 months from ``from`` (when only it is set) or the current month.
 *  ``offset`` shifts by 12 months per step. */
export function weekWindow(
  from: string | null, until: string | null, today: string, ws: WeekStart, offset: number,
): WeekWindow {
  const month = (d: string) => weekAnchor(d, ws).slice(0, 7)
  let start: Month
  let end: Month
  if (from && until && from <= until) {
    start = month(from)
    // A multi-year range shows its first 18 months; ‹ › reach the rest.
    end = month(until) < addMonths(start, MAX_WINDOW_MONTHS - 1) ? month(until)
      : addMonths(start, MAX_WINDOW_MONTHS - 1)
  } else {
    start = from && !until ? month(from) : today.slice(0, 7)
    end = addMonths(start, 11)
  }
  if (offset === 0) return { start, end, school: !!(from && until && from <= until) }
  const s = addMonths(start, 12 * offset)
  return { start: s, end: addMonths(s, 11), school: false }
}

/** Anchors of the weeks whose anchor day falls in ``start``…``end``. */
export function anchorsOfWindow(w: Pick<WeekWindow, 'start' | 'end'>, ws: WeekStart): string[] {
  let a = weekAnchor(`${w.start}-01`, ws)
  if (a < `${w.start}-01`) a = addDays(a, 7)
  const out: string[] = []
  while (a.slice(0, 7) <= w.end) {
    out.push(a)
    a = addDays(a, 7)
  }
  return out
}

/** ``anchors`` grouped by the month of their anchor day, in order. */
export function monthRows(anchors: readonly string[]): { month: Month; anchors: string[] }[] {
  const rows: { month: Month; anchors: string[] }[] = []
  for (const a of anchors) {
    const m = a.slice(0, 7)
    const last = rows[rows.length - 1]
    if (last && last.month === m) last.anchors.push(a)
    else rows.push({ month: m, anchors: [a] })
  }
  return rows
}
