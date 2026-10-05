/**
 * Timezone helpers for the calendar surface.
 *
 * Every calendar event carries an IANA timezone (``event.tz``) — the
 * wall-clock anchor the host had in mind when they created the event.
 * The SPA does two boundary conversions:
 *
 *   1. **Create / edit form** — the user types "19:00 on Tuesday" in
 *      their local mind, in the household's (or the space's) timezone.
 *      `localPartsToUtcIso` translates that ``(date, time, tz)`` triple
 *      into a UTC ISO string for the wire — the backend stores UTC.
 *      This replaces the legacy `${date}T${time}:00Z` shape, which
 *      lied about the tz suffix and got every event off by the
 *      creator's UTC offset.
 *
 *   2. **Display** — the event comes back with ``start`` (UTC) and
 *      ``tz`` (the originating IANA zone). `formatEventTime` renders
 *      the wall clock in ``event.tz`` as the primary line; when the
 *      viewer's browser timezone differs, a secondary "≈ HH:MM your
 *      time" line is included so cross-household members see both the
 *      shared anchor and their personal equivalent.
 *
 * DST is handled by `Intl.DateTimeFormat` under the hood — IANA names
 * carry the full transition rule set, so the same `(UTC, tz)` pair
 * resolves to the correct wall clock on either side of a DST shift.
 */

import { formatLocale, t } from '@/i18n/i18n'

/** The viewer's current browser timezone (IANA name). Cached at
 *  module load — `Intl` returns a stable resolution per process. */
export function detectBrowserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** Internal: extract the UTC offset (in minutes) of ``tz`` at the
 *  instant ``utcDate``. Uses Intl's ``formatToParts`` with a hidden
 *  longOffset token. Returns 0 (UTC) on unknown zones rather than
 *  throwing — the caller falls back to UTC, matching the "events stay
 *  reachable even with a malformed tz" safety net the backend keeps. */
function offsetMinutesAt(utcDate: Date, tz: string): number {
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      timeZoneName: 'longOffset',
      hour: '2-digit',
    })
    const parts = fmt.formatToParts(utcDate)
    const off = parts.find((p) => p.type === 'timeZoneName')?.value
    if (!off) return 0
    // ``"GMT+01:00"`` / ``"GMT−05:30"`` / bare ``"GMT"``.
    const m = /GMT([+−-])(\d{1,2})(?::?(\d{2}))?/.exec(off)
    if (!m) return 0
    const sign = m[1] === '-' || m[1] === '−' ? -1 : 1
    const hh = parseInt(m[2], 10)
    const mm = m[3] ? parseInt(m[3], 10) : 0
    return sign * (hh * 60 + mm)
  } catch {
    return 0
  }
}

/** Convert a local-time form input ``(date, time, tz)`` to UTC ISO.
 *
 *  ``date`` is ``YYYY-MM-DD``, ``time`` is ``HH:MM``, both in the
 *  zone ``tz``. The returned string is a tz-aware UTC ISO 8601 the
 *  backend can parse directly with `datetime.fromisoformat`.
 *
 *  Implementation: build a UTC datetime from the literal parts, then
 *  measure the actual ``tz`` offset at that instant via Intl and
 *  subtract it. This works for both standard and DST offsets because
 *  the IANA tz database knows when the transition happens.
 */
export function localPartsToUtcIso(
  date: string,
  time: string,
  tz: string,
): string {
  const [yyyy, mm, dd] = date.split('-').map((s) => parseInt(s, 10))
  const [hh, mi] = time.split(':').map((s) => parseInt(s, 10))
  // The wall-clock instant in ``tz`` is the same numbers; build a UTC
  // datetime from them and shift by ``tz``'s offset to recover the
  // actual UTC instant.
  const naiveUtc = Date.UTC(yyyy, (mm || 1) - 1, dd || 1, hh || 0, mi || 0, 0)
  const offsetMin = offsetMinutesAt(new Date(naiveUtc), tz)
  const utcMs = naiveUtc - offsetMin * 60 * 1000
  return new Date(utcMs).toISOString()
}

/** Render a UTC ISO start (the storage shape) into its wall clock in
 *  ``tz``. ``YYYY-MM-DD`` and ``HH:MM`` form, suitable for prefilling
 *  the edit dialog's date / time ``<input>``s without lying about the
 *  zone the way the legacy `toTimeString()` did. */
export function utcIsoToLocalParts(
  utcIso: string,
  tz: string,
): { date: string; time: string } {
  const d = new Date(utcIso)
  // ``en-CA`` gives ISO-style ``YYYY-MM-DD`` from `toLocaleDateString`
  // — every other locale juggles the order. Hour / minute is locale-
  // agnostic via ``hour12: false``.
  const date = d.toLocaleDateString('en-CA', { timeZone: tz })
  const time = d.toLocaleTimeString('en-GB', {
    timeZone: tz,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  return { date, time }
}

export interface FormattedEventTime {
  /** Wall-clock string in the event's tz (e.g. ``"7:00 PM"``). */
  primary: string
  /** The event's tz, shown next to ``primary`` when it differs from
   *  the viewer's browser tz (e.g. ``"Europe/Berlin"``). */
  primaryTz: string
  /** Optional "≈ HH:MM your time" line — non-null only when the
   *  event's tz differs from the viewer's, so the common
   *  same-household case stays uncluttered. */
  secondary: string | null
}

/** Render a calendar event time honouring the event's wall-clock
 *  anchor (``eventTz``). When the viewer's browser timezone differs,
 *  attach a "≈ HH:MM your time" hint so cross-household members can
 *  read both the shared anchor and their personal equivalent without
 *  doing math.
 */
export function formatEventTime(
  utcIso: string,
  eventTz: string,
  viewerTz: string = detectBrowserTz(),
): FormattedEventTime {
  const d = new Date(utcIso)
  const primary = d.toLocaleTimeString(formatLocale(), {
    timeZone: eventTz,
    hour: 'numeric',
    minute: '2-digit',
  })
  if (eventTz === viewerTz || !eventTz) {
    return { primary, primaryTz: eventTz, secondary: null }
  }
  const viewerLocal = d.toLocaleTimeString(formatLocale(), {
    timeZone: viewerTz,
    hour: 'numeric',
    minute: '2-digit',
  })
  return {
    primary,
    primaryTz: eventTz,
    secondary: t('time.your_time', { time: viewerLocal }),
  }
}
