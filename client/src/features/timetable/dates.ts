/**
 * Calendar-date helpers for the timetable's week mode and the week
 * validity picker.
 *
 * Dates are the wire's ``"YYYY-MM-DD"`` strings, handled as local
 * calendar days (``new Date(y, m, d)`` + ``setDate``) so a DST switch
 * never shifts a day. A week's *anchor* is its first day — a Monday or
 * a Sunday, per the timetable's ``week_start`` — exactly what the
 * backend stores in ``excluded_weeks`` and answers as ``week.anchor``.
 */
import { locale, t } from '@/i18n/i18n'
import type { Timetable, TimetableOverride, TimetableValidity } from '@/types'
import { isoWeekNumber, startOfWeek, type WeekStart } from '@/utils/week'

/** The backend refuses overrides dated more than this many days ago. */
export const OVERRIDE_RETENTION_DAYS = 14

const pad = (n: number) => String(n).padStart(2, '0')

/** Local calendar day of ``d`` as ``"YYYY-MM-DD"``. */
export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** ``"YYYY-MM-DD"`` → local midnight. */
export function parseIsoDate(s: string): Date {
  const [y, m, d] = s.split('-').map(Number)
  return new Date(y, m - 1, d)
}

export function isIsoDate(s: string | null | undefined): s is string {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false
  return isoDate(parseIsoDate(s)) === s
}

export function addDays(s: string, n: number): string {
  const d = parseIsoDate(s)
  d.setDate(d.getDate() + n)
  return isoDate(d)
}

/** Whole days from ``a`` to ``b`` (negative when ``b`` is earlier). */
export function daysBetween(a: string, b: string): number {
  const utc = (s: string) => {
    const [y, m, d] = s.split('-').map(Number)
    return Date.UTC(y, m - 1, d)
  }
  return Math.round((utc(b) - utc(a)) / 86_400_000)
}

/** Anchor (first day) of the week containing ``s``. */
export function weekAnchor(s: string, ws: WeekStart): string {
  return isoDate(startOfWeek(parseIsoDate(s), ws))
}

/** ISO weekday (0 = Mon) of a date string. */
export function weekdayOf(s: string): number {
  return (parseIsoDate(s).getDay() + 6) % 7
}

/** Today in ``tz`` (the timetable's zone); the browser's today when the
 *  zone is unknown to ``Intl``. */
export function todayIn(tz: string, now: Date = new Date()): string {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(now)
    const get = (type: string) => parts.find(p => p.type === type)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    return isoDate(now)
  }
}

/** Overrides dated before this are refused by the backend (422). */
export function editableFrom(tt: Pick<Timetable, 'tz'>, now: Date = new Date()): string {
  return addDays(todayIn(tt.tz, now), -OVERRIDE_RETENTION_DAYS)
}

const fmtCache = new Map<string, Intl.DateTimeFormat>()
function fmt(opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale.value}|${JSON.stringify(opts)}`
  let f = fmtCache.get(key)
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(locale.value, opts)
    } catch {
      f = new Intl.DateTimeFormat('en', opts)
    }
    fmtCache.set(key, f)
  }
  return f
}

/** "Oct 5" */
export function shortDate(s: string): string {
  return fmt({ month: 'short', day: 'numeric' }).format(parseIsoDate(s))
}

/** "Oct 5, 2026" */
export function longDate(s: string): string {
  return fmt({ year: 'numeric', month: 'short', day: 'numeric' }).format(parseIsoDate(s))
}

/** "Mon, Oct 5" */
export function weekdayDate(s: string): string {
  return fmt({ weekday: 'short', month: 'short', day: 'numeric' }).format(parseIsoDate(s))
}

/** "Monday, October 5, 2026" — what a screen reader hears for a day. */
export function fullDate(s: string): string {
  return fmt({ weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' }).format(parseIsoDate(s))
}

/** Day-of-month number for a day heading ("5"). */
export function dayOfMonth(s: string): string {
  return String(parseIsoDate(s).getDate())
}

/** "Oct 5 – 11" / "Sep 28 – Oct 4" (``withYear`` adds the year). */
export function dateRange(from: string, to: string, withYear = false): string {
  const f = fmt(withYear
    ? { year: 'numeric', month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric' })
  const a = parseIsoDate(from)
  const b = parseIsoDate(to)
  const range = (f as Intl.DateTimeFormat & {
    formatRange?: (x: Date, y: Date) => string
  }).formatRange
  return range ? range.call(f, a, b) : `${f.format(a)} – ${f.format(b)}`
}

/** The navigator label of the week starting ``anchor``: "W41 · Oct 5 – 11"
 *  for a Monday-start timetable, "Oct 4 – 10" for a Sunday-start one
 *  (Sunday weeks have no ISO number). */
export function weekLabel(anchor: string, ws: WeekStart): string {
  const range = dateRange(anchor, addDays(anchor, 6))
  return ws === 0
    ? `${t('timetable.weeks.kw', { n: String(isoWeekNumber(parseIsoDate(anchor))) })} · ${range}`
    : range
}

/** Overrides dated inside the week starting ``anchor``. */
export function overridesInWeek(
  tt: Pick<Timetable, 'overrides'>, anchor: string,
): TimetableOverride[] {
  const end = addDays(anchor, 7)
  return tt.overrides.filter(o => o.date >= anchor && o.date < end)
}

/** "Sep 1, 2026 – Jul 30, 2027 · 6 holiday weeks" — the chip text. */
export function validitySummary(v: TimetableValidity): string {
  const range = v.valid_from && v.valid_until
    ? dateRange(v.valid_from, v.valid_until, true)
    : v.valid_from ? t('timetable.validity.from', { date: longDate(v.valid_from) })
      : v.valid_until ? t('timetable.validity.until', { date: longDate(v.valid_until) })
        : t('timetable.validity.always')
  const n = v.excluded_weeks.length
  if (n === 0) return range
  return `${range} · ${t(n === 1 ? 'timetable.validity.holidays_one' : 'timetable.validity.holidays',
    { n: String(n) })}`
}
