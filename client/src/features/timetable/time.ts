/**
 * Pure time / weekday helpers for the timetable UI.
 *
 * Times are the wire's ``"HH:MM"`` wall-clock strings; weekdays are
 * ISO-style 0 = Mon … 6 = Sun (``utils/week.ts``). Weekday names come
 * from ``Intl`` in the UI locale so every language gets its own short
 * names ("Mon" / "Mo" / "lun.").
 */
import { locale, t } from '@/i18n/i18n'
import { weekdayOrder, type WeekStart } from '@/utils/week'

const LAST_MINUTE = 23 * 60 + 59

/** ``"08:05"`` → 485. */
export function toMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number)
  return h * 60 + m
}

/** 485 → ``"08:05"``, clamped to the day (there is no 24:00). */
export function fromMinutes(minutes: number): string {
  const m = Math.max(0, Math.min(LAST_MINUTE, Math.round(minutes)))
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

/** ``"08:00–08:45"``. */
export function formatRange(start: string, end: string): string {
  return `${start}–${end}`
}

/** Round to the nearest 5 minutes. */
export function snapTo5(minutes: number): number {
  return Math.round(minutes / 5) * 5
}

/** ``days`` sorted into the timetable's week-start order. */
export function orderedDays(days: readonly number[], weekStart: WeekStart): number[] {
  const set = new Set(days)
  return weekdayOrder(weekStart).filter(d => set.has(d))
}

/** Localised weekday name for ISO weekday ``wd`` (0 = Mon). */
const formatters = new Map<string, Intl.DateTimeFormat>()

function weekdayFormatter(lang: string, style: 'short' | 'long'): Intl.DateTimeFormat {
  const key = `${lang}|${style}`
  let f = formatters.get(key)
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(lang, { weekday: style, timeZone: 'UTC' })
    } catch {
      f = new Intl.DateTimeFormat('en', { weekday: style, timeZone: 'UTC' })
    }
    formatters.set(key, f)
  }
  return f
}

export function weekdayName(
  wd: number,
  style: 'short' | 'long',
  lang: string = locale.value,
): string {
  // 2024-01-01 was a Monday; pin the zone so the offset can't shift it.
  return weekdayFormatter(lang, style).format(new Date(Date.UTC(2024, 0, 1 + wd)))
}

/** "Mon–Fri", "Mon, Wed, Fri", "Every day" — runs of ≥ 3 contiguous
 *  days (in week-start order) collapse into a range. */
export function daysSummary(days: readonly number[], weekStart: WeekStart): string {
  const ordered = orderedDays(days, weekStart)
  if (ordered.length === 7) return t('timetable.days.every_day')
  const order = weekdayOrder(weekStart)
  const runs: number[][] = []
  for (const d of ordered) {
    const last = runs[runs.length - 1]
    if (last && order.indexOf(d) === order.indexOf(last[last.length - 1]) + 1) {
      last.push(d)
    } else {
      runs.push([d])
    }
  }
  return runs.map(run => run.length >= 3
    ? `${weekdayName(run[0], 'short')}–${weekdayName(run[run.length - 1], 'short')}`
    : run.map(d => weekdayName(d, 'short')).join(', '),
  ).join(', ')
}

/** "3rd" (en) / "3." (de) — from the locale's ordinal plural rules. */
export function ordinal(n: number): string {
  let rule = 'other'
  try {
    rule = new Intl.PluralRules(locale.value, { type: 'ordinal' }).select(n)
  } catch { /* unknown locale → "other" */ }
  const key = `timetable.ordinal.${rule}`
  const text = t(key, { n: String(n) })
  return text === key ? t('timetable.ordinal.other', { n: String(n) }) : text
}

/** ``from`` if it is in ``days``, else the next weekday that is
 *  (wrapping past Sunday). ``days`` must be non-empty. */
export function nextDayFrom(from: number, days: readonly number[]): number {
  for (let i = 0; i < 7; i++) {
    const d = (from + i) % 7
    if (days.includes(d)) return d
  }
  return days[0]
}
