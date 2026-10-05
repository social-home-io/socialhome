/**
 * Relative-time helpers for feed-style surfaces.
 *
 * Five surfaces (DM inbox, Pages index, Pages viewer, Notifications,
 * follow-on candidates) had each inlined a lightly-tweaked relative
 * formatter. The shapes split into two families that the SPA actually
 * needs:
 *
 * 1. **Compact ("chat" shape)** — for dense lists like the DM inbox or
 *    the Momentum feed where a one-or-two-character pill is what the
 *    user is scanning. ``now`` / ``5m`` / ``3h`` / ``Yesterday`` /
 *    ``Mon`` / ``Apr 23``.
 * 2. **Verbose ("docs" shape)** — for surfaces where the row has space
 *    for a friendly phrase like a Pages byline or a notification row.
 *    ``just now`` / ``5 min ago`` / ``3h ago`` / ``yesterday`` /
 *    ``5 days ago`` / ``Apr 23``.
 *
 * Both fall back to the raw input on parse failure (rare — server
 * timestamps are always RFC 3339). The surfaces still pair the result
 * with a ``time[title]`` carrying the precise locale string so a hover
 * or screen-reader still gets the full stamp.
 */

import { isOne, formatLocale, t } from '@/i18n/i18n'

const MS_PER_MIN = 60_000
const MS_PER_DAY = 86_400_000

interface ParsedDelta {
  t: number
  now: number
  diff: number
  min: number
  hr: number
  sameDay: boolean
  yesterday: boolean
}

/** Normalise the raw server timestamp before handing it to ``Date.parse``.
 *
 *  Several backend paths still store ``viewed_at`` / ``last_seen_at`` /
 *  similar via SQLite's ``datetime('now')``, which produces the naive
 *  shape ``"2026-05-13 18:34:56"`` (UTC value, but no ``T``, no ``Z``,
 *  no offset). JS's ``Date.parse`` interprets that local-format string
 *  as the **viewer's local time**, so a fresh "just now" stamp can
 *  read as "2h ago" for anyone in a positive UTC offset (e.g. CEST).
 *
 *  We detect the naive shape with a tight regex (date + space + time,
 *  no trailing zone) and synthesise a proper UTC ISO 8601 string by
 *  swapping the space for ``T`` and appending ``Z``. Strings that
 *  already carry a zone designator (``Z`` / ``±HH:MM``) or the ISO
 *  ``T`` separator are passed through untouched, so backend rows that
 *  do the right thing keep working.
 */
const NAIVE_SQLITE_TS = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/
/**
 * Make a wall-clock-vs-UTC-safe ISO string out of whatever the backend
 * sent. The two shapes the SH backend produces are both UTC by the
 * codebase's invariants (see CLAUDE.md "Database timestamps") — this
 * helper just makes ``Date.parse`` agree by tagging the naive shape
 * with a ``Z`` and the ``T`` separator. Re-exported because anything
 * that reads a SH backend timestamp into JS Date math wants the same
 * normalisation (otherwise local-time interpretation creeps in for
 * users not in UTC).
 */
export function normaliseTimestamp(iso: string): string {
  if (NAIVE_SQLITE_TS.test(iso)) return iso.replace(' ', 'T') + 'Z'
  return iso
}

function parseDelta(iso: string): ParsedDelta | null {
  const t = Date.parse(normaliseTimestamp(iso))
  if (Number.isNaN(t)) return null
  const now = Date.now()
  const diff = now - t
  const dayThen = new Date(t).toDateString()
  const dayNow = new Date(now).toDateString()
  const yesterdayDate = new Date(now)
  yesterdayDate.setDate(yesterdayDate.getDate() - 1)
  return {
    t,
    now,
    diff,
    min: Math.floor(diff / MS_PER_MIN),
    hr: Math.floor(diff / MS_PER_MIN / 60),
    sameDay: dayThen === dayNow,
    yesterday: dayThen === yesterdayDate.toDateString(),
  }
}

/**
 * Compact "chat" shape. Use for tight scan-by lists (DM inbox, momentum
 * feed). One or two characters wherever possible.
 */
export function relativeChatTime(iso: string): string {
  const d = parseDelta(iso)
  if (!d) return iso
  if (d.min < 1) return 'now'
  if (d.min < 60) return `${d.min}m`
  if (d.sameDay) return `${d.hr}h`
  if (d.yesterday) return 'Yesterday'
  if (d.diff < 6 * MS_PER_DAY) {
    return new Date(d.t).toLocaleDateString(undefined, { weekday: 'short' })
  }
  return new Date(d.t).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  })
}

/**
 * Verbose "docs" shape. Use for surfaces that have room for a friendly
 * phrase (Pages byline, Notifications row). In the UI language.
 */
export function relativeDocsTime(iso: string): string {
  const d = parseDelta(iso)
  if (!d) return iso
  if (d.min < 1) return t('time.just_now')
  if (d.min < 60) return t('time.minutes_ago', { n: String(d.min) })
  if (d.sameDay) return t('time.hours_ago_short', { n: String(d.hr) })
  if (d.yesterday) return t('time.yesterday')
  if (d.diff < 7 * MS_PER_DAY) {
    const days = Math.floor(d.diff / MS_PER_DAY)
    return t(isOne(days) ? 'time.days_ago_long_one' : 'time.days_ago_long', { n: String(days) })
  }
  const sameYear = new Date(d.t).getFullYear() === new Date(d.now).getFullYear()
  return new Date(d.t).toLocaleDateString(formatLocale(), {
    month: 'short',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
  })
}

/**
 * Forward-looking shape — "in 6 days" / "in 3 hours" / "in 5 minutes" —
 * for a timestamp that is expected to sit in the future (an invite
 * link's ``expires_at``, say), in the UI language.
 *
 * ``relativeDocsTime`` is past-tense only: every future stamp collapses
 * to "just now" there, which reads as *already expired* on exactly the
 * surface where the remaining life is the point. Anything at or past
 * the moment of the call returns the translated "expired".
 */
export function relativeFutureTime(iso: string): string {
  const at = Date.parse(normaliseTimestamp(iso))
  if (Number.isNaN(at)) return iso
  const ms = at - Date.now()
  if (ms <= 0) return t('time.expired')
  const min = Math.floor(ms / MS_PER_MIN)
  // Round, don't floor, from the hour up: a link minted seconds ago with
  // a 7-day life is 6.9999 days out, and flooring made the confirmation
  // sentence contradict the picker the user had just used ("Stops
  // working after 7 days" → "in 6 days"). Same for the 1-day option,
  // which read as "in 23 hours".
  const hr = ms / (MS_PER_MIN * 60)
  const days = Math.round(ms / MS_PER_DAY)
  if (days >= 30) {
    return t('time.on_date', {
      date: new Date(at).toLocaleDateString(formatLocale(), {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      }),
    })
  }
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] = min < 60
    ? [Math.max(1, min), 'minute']
    : hr < 23.5
      ? [Math.round(hr), 'hour']
      : [days, 'day']
  try {
    return new Intl.RelativeTimeFormat(formatLocale(), { numeric: 'always' })
      .format(value, unit)
  } catch {
    return `in ${value} ${unit}${value === 1 ? '' : 's'}`
  }
}
