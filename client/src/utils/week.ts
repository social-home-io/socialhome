/**
 * First-day-of-the-week helpers.
 *
 * Weekdays are indexed ISO-style — Monday = 0 … Sunday = 6 — which
 * matches the backend's Python ``date.weekday()``. A :type:`WeekStart`
 * is therefore just the ISO weekday index of the day a week opens on.
 *
 * The user picks ``'mon'`` / ``'sun'`` / ``'auto'`` (the ``week_start``
 * preference); ``'auto'`` follows the browser locale's week info.
 * All date math is local-calendar (``setDate``) so a week spanning a
 * DST switch never drifts off local midnight.
 */
import { getPreferences } from './preferences'

/** ISO weekday index of the first day of the week: 0 = Mon, 6 = Sun. */
export type WeekStart = 0 | 6

/** Stored preference; ``'auto'`` = follow the locale. */
export type WeekStartPref = 'auto' | 'mon' | 'sun'

interface WeekInfo { firstDay?: number }
interface LocaleWithWeekInfo {
  getWeekInfo?: () => WeekInfo
  weekInfo?: WeekInfo
}

/** Week start the locale uses. ``Intl.Locale.getWeekInfo()`` (or the
 *  older ``weekInfo`` getter) reports ``firstDay`` as 1 = Mon … 7 = Sun.
 *  ``locale`` defaults to the browser's (``navigator.language``), not
 *  the app UI language — the UI locale carries no region (``en`` vs
 *  en-US / en-GB), and the region is what decides the week start.
 *  Only Sunday and Monday are offered, so any non-Sunday answer (incl.
 *  Saturday-first locales) maps to Monday, as does an engine without
 *  week info or a locale tag ``Intl`` rejects. */
export function detectLocaleWeekStart(locale?: string): WeekStart {
  try {
    const loc = new Intl.Locale(locale ?? navigator.language) as unknown as LocaleWithWeekInfo
    const info = loc.getWeekInfo?.() ?? loc.weekInfo
    return info?.firstDay === 7 ? 6 : 0
  } catch {
    return 0
  }
}

/** The user's ``week_start`` preference; unknown / absent → ``'auto'``. */
export function getWeekStartPref(): WeekStartPref {
  const v = getPreferences().week_start
  return v === 'mon' || v === 'sun' ? v : 'auto'
}

/** Resolve a preference to a concrete week start. */
export function resolveWeekStart(pref: WeekStartPref = 'auto'): WeekStart {
  if (pref === 'mon') return 0
  if (pref === 'sun') return 6
  return detectLocaleWeekStart()
}

/** The week start in effect right now — the stored preference or the
 *  locale default. Read per call so a changed preference applies at
 *  once. */
export function currentWeekStart(): WeekStart {
  return resolveWeekStart(getWeekStartPref())
}

/** ISO weekday of ``d`` in local time: Mon = 0 … Sun = 6. */
export function isoWeekday(d: Date): number {
  return (d.getDay() + 6) % 7
}

/** Local midnight of the first day of the week containing ``d``. */
export function startOfWeek(d: Date, ws: WeekStart): Date {
  const offset = (isoWeekday(d) - ws + 7) % 7
  const start = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  start.setDate(start.getDate() - offset)
  return start
}

/** ISO weekday indices in display order for ``ws``. */
export function weekdayOrder(ws: WeekStart): number[] {
  return Array.from({ length: 7 }, (_, i) => (ws + i) % 7)
}

/** ISO 8601 week number (1–53) of ``d``'s local calendar day: weeks
 *  run Monday–Sunday and week 1 holds the year's first Thursday. Math
 *  on UTC midnights so a DST switch can't shift the day count. */
export function isoWeekNumber(d: Date): number {
  const day = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())
  // The Thursday of d's week decides the ISO year.
  const thursday = new Date(day + (3 - isoWeekday(d)) * 86_400_000)
  const jan1 = Date.UTC(thursday.getUTCFullYear(), 0, 1)
  return Math.floor((thursday.getTime() - jan1) / (7 * 86_400_000)) + 1
}
