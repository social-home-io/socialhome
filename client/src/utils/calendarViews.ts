/**
 * The calendar's views — Upcoming (a rolling agenda), Month and Week
 * (day grids with spanning bars) — as pure layout over event rows.
 *
 * Every view works in ``YYYY-MM-DD`` day keys from
 * :func:`eventDaySpan`, so an event lands on the same days in every view
 * (and in the household agenda, which buckets by the same rule). A
 * multi-day event is ONE entry everywhere: one row in Upcoming, one bar
 * per week row in a grid — never a card per day.
 */
import { formatLocale, t } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import {
  addDaysToKey, dayKeyOf, daysBetweenKeys, eventDaySpan, formatDayKey,
} from './calendar'
import { startOfWeek, type WeekStart } from './week'

/** The views a space calendar offers. Week is for wide screens. */
export type CalendarView = 'upcoming' | 'month' | 'week'

/** Week needs room for seven readable columns: narrower screens get
 *  Upcoming and Month only (a space's Calendar and the household page). */
export const CALENDAR_WIDE_QUERY = '(min-width: 769px)'

/** The view on screen: Week falls back to Month on a narrow screen. */
export function viewOnScreen(v: CalendarView, wide: boolean): CalendarView {
  return v === 'week' && !wide ? 'month' : v
}

/** How far Upcoming looks ahead, in weeks. */
export const UPCOMING_WEEKS = 4

/** One event with the days it covers. */
export interface SpanEntry {
  event: CalendarEvent
  /** First covered day. */
  startKey: string
  /** Last covered day (inclusive). */
  endKey: string
  /** Days covered — 1 for a same-day event. */
  dayCount: number
}

/** ``events`` with their spans, in start order (all-day before timed on
 *  the same day, then by start time). Unparseable rows are dropped. */
export function spanEntries(events: CalendarEvent[]): SpanEntry[] {
  const out: SpanEntry[] = []
  for (const event of events) {
    const span = eventDaySpan(event)
    if (span) out.push({ event, ...span })
  }
  return out.sort(compareEntries)
}

function compareEntries(a: SpanEntry, b: SpanEntry): number {
  if (a.startKey !== b.startKey) return a.startKey < b.startKey ? -1 : 1
  // Longer spans first: they are the day's standing context.
  if (a.dayCount !== b.dayCount) return b.dayCount - a.dayCount
  if (a.event.all_day !== b.event.all_day) return a.event.all_day ? -1 : 1
  return new Date(a.event.start).getTime() - new Date(b.event.start).getTime()
}

/** ISO bounds of Upcoming: today's local midnight up to the end of the
 *  last day of the look-ahead. */
export function upcomingRange(now: Date): { start: string; end: string } {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const end = new Date(start)
  end.setDate(start.getDate() + UPCOMING_WEEKS * 7 - 1)
  end.setHours(23, 59, 59, 0)
  return { start: start.toISOString(), end: end.toISOString() }
}

/** One week of Upcoming. */
export interface UpcomingWeek {
  /** First day of the week (the viewer's week start). */
  startKey: string
  /** Last day of the week. */
  endKey: string
  /** 0 = the week holding today, 1 = next week, … */
  offset: number
  entries: SpanEntry[]
}

/** Upcoming: the events from today through the look-ahead, each ONCE,
 *  filed under the week it is next on — an event already running
 *  (started before today) sits in this week. Weeks without events are
 *  left out. */
export function groupUpcomingByWeek(
  events: CalendarEvent[], now: Date, weekStart: WeekStart,
): UpcomingWeek[] {
  const todayKey = dayKeyOf(now)
  const lastKey = addDaysToKey(todayKey, UPCOMING_WEEKS * 7 - 1)
  const firstWeek = dayKeyOf(startOfWeek(now, weekStart))
  const weeks = new Map<number, UpcomingWeek>()
  for (const entry of spanEntries(events)) {
    if (entry.endKey < todayKey || entry.startKey > lastKey) continue
    const anchor = entry.startKey < todayKey ? todayKey : entry.startKey
    const offset = Math.floor(daysBetweenKeys(firstWeek, anchor) / 7)
    let week = weeks.get(offset)
    if (!week) {
      const startKey = addDaysToKey(firstWeek, offset * 7)
      week = { startKey, endKey: addDaysToKey(startKey, 6), offset, entries: [] }
      weeks.set(offset, week)
    }
    week.entries.push(entry)
  }
  return [...weeks.values()].sort((a, b) => a.offset - b.offset)
}

/** The day keys of a grid: the weeks covering the month of ``cursor``
 *  (``month``) or the one week holding it (``week``). Each row is 7
 *  keys starting on ``weekStart``. */
export function gridWeeks(
  cursor: Date, view: 'month' | 'week', weekStart: WeekStart,
): string[][] {
  const first = view === 'month'
    ? new Date(cursor.getFullYear(), cursor.getMonth(), 1)
    : cursor
  const firstKey = dayKeyOf(startOfWeek(first, weekStart))
  const lastKey = view === 'month'
    ? dayKeyOf(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0))
    : addDaysToKey(firstKey, 6)
  const rows: string[][] = []
  for (let k = firstKey; k <= lastKey; k = addDaysToKey(k, 7)) {
    rows.push(Array.from({ length: 7 }, (_, i) => addDaysToKey(k, i)))
  }
  return rows
}

/** ISO bounds covering every day of ``weeks`` (local midnight of the
 *  first through the end of the last) — the grid fetches leading and
 *  trailing days too, so they aren't blank. */
export function gridRange(weeks: string[][]): { start: string; end: string } {
  const [y1, m1, d1] = weeks[0][0].split('-').map(Number)
  const last = weeks[weeks.length - 1][6]
  const [y2, m2, d2] = last.split('-').map(Number)
  return {
    start: new Date(y1, m1 - 1, d1).toISOString(),
    end: new Date(y2, m2 - 1, d2, 23, 59, 59).toISOString(),
  }
}

/** One event's bar in one week row of a grid. */
export interface GridBar {
  entry: SpanEntry
  /** Column the bar starts in (0 = the week's first day). */
  col: number
  /** Columns it spans (1–7). */
  span: number
  /** Stacking row inside the week (0 = top). */
  lane: number
  /** The event began in an earlier week / ends in a later one — the bar
   *  is drawn open on that side. */
  continuesBefore: boolean
  continuesAfter: boolean
}

/** Bars for one week row: every entry overlapping ``weekKeys``, clipped
 *  to the week and packed into the first lane free across its columns
 *  (longer spans claim lanes first, so they stay on top). */
export function layoutWeekBars(weekKeys: string[], entries: SpanEntry[]): GridBar[] {
  const first = weekKeys[0]
  const last = weekKeys[6]
  const lanes: boolean[][] = []
  const bars: GridBar[] = []
  for (const entry of entries) {
    if (entry.endKey < first || entry.startKey > last) continue
    const from = entry.startKey < first ? first : entry.startKey
    const to = entry.endKey > last ? last : entry.endKey
    const col = daysBetweenKeys(first, from)
    const span = daysBetweenKeys(from, to) + 1
    let lane = 0
    while (lanes[lane]?.slice(col, col + span).some(Boolean)) lane++
    lanes[lane] ??= Array(7).fill(false)
    for (let c = col; c < col + span; c++) lanes[lane][c] = true
    bars.push({
      entry, col, span, lane,
      continuesBefore: entry.startKey < first,
      continuesAfter: entry.endKey > last,
    })
  }
  return bars
}

/** The entries on day ``key`` (in start order). */
export function entriesOnDay(entries: SpanEntry[], key: string): SpanEntry[] {
  return entries.filter((e) => e.startKey <= key && e.endKey >= key)
}

/** "Mon 19" — weekday and day of month, in the UI locale. */
export function shortDay(key: string): string {
  return `${formatDayKey(key, { weekday: 'short' })} ${formatDayKey(key, { day: 'numeric' })}`
}

/** The two ends of a span: "Mon 26", "Sat 31"; the months are named
 *  when the span crosses one ("Fri 30 Oct", "Mon 2 Nov"). */
export function spanEnds(startKey: string, endKey: string): [string, string] {
  if (startKey.slice(0, 7) === endKey.slice(0, 7)) return [shortDay(startKey), shortDay(endKey)]
  const withMonth = (key: string) =>
    `${shortDay(key)} ${formatDayKey(key, { month: 'short' })}`
  return [withMonth(startKey), withMonth(endKey)]
}

/** "Mon 26 – Sat 31" (see :func:`spanEnds`). */
export function spanDays(startKey: string, endKey: string): string {
  const [from, to] = spanEnds(startKey, endKey)
  return `${from} – ${to}`
}

/** "Oct 19 – 25" for a week heading (the locale collapses the shared
 *  month). Day keys are dates, so they are formatted pinned to UTC. */
export function weekRangeLabel(startKey: string, endKey: string): string {
  const at = (key: string) => {
    const [y, m, d] = key.split('-').map(Number)
    return new Date(Date.UTC(y, m - 1, d))
  }
  return new Intl.DateTimeFormat(formatLocale(), {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  }).formatRange(at(startKey), at(endKey))
}

/** The heading of an Upcoming week: "This week", "Next week", then the
 *  dates alone. */
export function upcomingWeekTitle(week: UpcomingWeek): string | null {
  if (week.offset === 0) return t('calendar.upcoming.this_week')
  if (week.offset === 1) return t('calendar.upcoming.next_week')
  return null
}
