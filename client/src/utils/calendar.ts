/**
 * Calendar date helpers shared by the household calendar
 * (`features/calendar/CalendarPage.tsx`) and a space's Calendar
 * (`features/spaces/SpaceCalendarSection.tsx`): the day rules every view
 * places events by (:func:`eventDaySpan`), the expanded event's bounds,
 * the shared-event merge and the grid headings. The views themselves are
 * ``utils/calendarViews.ts`` — one calendar UI, no drift between the
 * household and a space.
 */
import { formatLocale } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import { utcIsoToLocalParts } from './timezone'
import { currentWeekStart, startOfWeek, type WeekStart } from './week'

/** Zones already checked by :func:`safeTimeZone` — ``Intl`` rejects an
 *  unknown zone by throwing, and the agenda asks the same question
 *  once per event per render. */
const _ZONE_OK = new Map<string, string>()

/** Resolve ``tz`` to a zone every ``Intl`` surface in this module will
 *  actually accept, falling back to ``'UTC'``.
 *
 *  ``event.tz`` is whatever the authoring household (or an ICS import,
 *  or a federated peer) put on the row, and ``Intl`` answers an unknown
 *  zone with ``RangeError: Invalid time zone specified`` — so a single
 *  malformed row would otherwise take out the whole household /
 *  space calendar (the App-level ``ErrorBoundary`` catches it into an
 *  error screen). ``'UTC'`` is the same fail-soft fallback
 *  ``offsetMinutesAt`` in ``utils/timezone`` already uses, and the zone
 *  a row with no ``tz`` is read in anyway: a bad zone costs one row its
 *  day precision, never the page. */
export function safeTimeZone(tz: string | null | undefined): string {
  if (!tz) return 'UTC'
  const cached = _ZONE_OK.get(tz)
  if (cached) return cached
  let resolved = 'UTC'
  try {
    // Constructing the formatter is what validates the zone.
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    resolved = tz
  } catch {
    resolved = 'UTC'
  }
  _ZONE_OK.set(tz, resolved)
  return resolved
}

/** The last inclusive MOMENT of an event whose ``end`` is an exclusive
 *  bound — one millisecond before it.
 *
 *  Every end bound in this module is exclusive at the boundary: an
 *  all-day row stores ``23:59`` in its own tz (so ``-1 ms`` stays on
 *  the same day), an ICS import stores the following ``00:00``, and a
 *  timed ``22:00 → 00:00`` span is one evening rather than two days.
 *  Subtracting the millisecond makes all three name the right day at
 *  once, which is why the rule — not any one formatter — is what the
 *  three call sites share (:func:`formatDayPortion`,
 *  :func:`formatEventBounds`, and ``EventPostCard``'s card headline;
 *  their output contracts differ deliberately).
 *
 *  Fail-soft: an unparseable ``endIso`` yields an invalid ``Date``
 *  (which every ``toLocale*`` renders as "Invalid Date") rather than
 *  throwing — callers already guard for that. */
export function lastInclusiveMoment(endIso: string): Date {
  const ms = new Date(endIso).getTime()
  return Number.isNaN(ms) ? new Date(NaN) : new Date(ms - 1)
}

/** A day key plus the wall clock it was read at, both in the SAME zone
 *  — see :func:`viewerParts` / :func:`zonedParts`. */
interface DayParts {
  /** ``YYYY-MM-DD`` bucket key. */
  key: string
  /** ``HH:MM`` wall clock in the key's zone, for the
   *  midnight-exclusive end rule. */
  time: string
}

/** Period units for ``advanceDate`` / ``formatRangeHeading`` (the
 *  calendar grids use month / week; ``day`` heads a single day). */
export type CalendarViewMode = 'month' | 'week' | 'day'

/** Build a ``YYYY-MM-DD`` key from the local date components of ``d``.
 *  Used as the bucket key in :func:`groupEventsByDay` — lexicographic
 *  sort over this shape is chronological, so callers don't have to
 *  round-trip through ``new Date()`` (which doesn't reliably parse
 *  locale-formatted strings like ``14.5.2026`` / ``14/05/2026``). */
function localDateKey(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${dd}`
}

/** Wall clock of ``d`` in the viewer's zone, ``HH:MM``. Paired with
 *  :func:`localDateKey` by :func:`groupEventsByDay` so the
 *  midnight-exclusive rule can be applied in the same zone the day key
 *  was derived in. */
function localTimeKey(d: Date): string {
  const h = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${mi}`
}

/** ``{ key: "YYYY-MM-DD", time: "HH:MM" }`` for ``iso`` read in the
 *  viewer's own zone — the convention for **timed** rows, which are
 *  wall-clock events for whoever is looking ("when does this happen
 *  for me"). ``null`` when ``iso`` is unparseable. */
function viewerParts(iso: string): DayParts | null {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return { key: localDateKey(d), time: localTimeKey(d) }
}

/** Same shape as :func:`viewerParts`, but read in the IANA zone ``tz``
 *  — the convention for **all-day** rows.
 *
 *  All-day events are NOT stored at UTC midnight: the composer writes
 *  them as ``00:00`` / ``23:59`` **in the event's own tz** (see
 *  ``components/CalendarEventDialog.tsx``, which calls
 *  ``localPartsToUtcIso(date, '00:00', tz)`` / ``'23:59'``) and the
 *  backend stamps that zone onto the row as ``event.tz``. So an
 *  all-day "1 May 2026" authored in ``Europe/Zurich`` is on the wire as
 *  ``2026-04-30T22:00:00Z → 2026-05-01T21:59:00Z``; bucketing it by UTC
 *  components would turn every non-UTC household's ordinary single-day
 *  holiday into a spurious two-day span (30 April + 1 May). Reading the
 *  components back in ``e.tz`` recovers exactly ``2026-05-01`` for
 *  every viewer in every zone.
 *
 *  ``utcIsoToLocalParts`` owns the Intl offset math (``utils/timezone``)
 *  — no offset arithmetic is duplicated here. */
function zonedParts(iso: string, tz: string): DayParts | null {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const { date, time } = utcIsoToLocalParts(iso, tz)
  // ``en-CA`` already yields ``YYYY-MM-DD``; pad the year so keys are
  // 4-2-2 even for the (pathological) sub-1000 years, keeping
  // lexicographic == chronological.
  const [y, m, dd] = date.split('-')
  return { key: `${y.padStart(4, '0')}-${m}-${dd}`, time }
}

/** Whole-day ordinal of a ``YYYY-MM-DD`` key. A date string carries no
 *  clock, so this arithmetic is DST-proof by construction — no offset
 *  can shift a day boundary that was never expressed as an instant. */
function keyOrdinal(key: string): number {
  const [y, m, d] = key.split('-').map(Number)
  return Math.round(Date.UTC(y, m - 1, d) / 86_400_000)
}

/** Format a ``YYYY-MM-DD`` day key as that calendar date in the UI's
 *  locale (``formatLocale()``), e.g. ``{ weekday: 'long', … }``.
 *
 *  A day key is a date, not an instant. The ordinal's midnight is read
 *  back pinned to ``timeZone: 'UTC'`` so the rendered day is the key's
 *  own day for every viewer — formatting it in the viewer's zone would
 *  show the previous day (and, on the 1st, the previous month) west of
 *  UTC. Any ``timeZone`` in ``options`` is overridden for that reason. */
export function formatDayKey(
  dayKey: string, options: Intl.DateTimeFormatOptions,
): string {
  return new Date(keyOrdinal(dayKey) * 86_400_000).toLocaleDateString(
    formatLocale(), { ...options, timeZone: 'UTC' },
  )
}

/** Inverse of :func:`keyOrdinal`. */
function keyFromOrdinal(ordinal: number): string {
  const d = new Date(ordinal * 86_400_000)
  const y = String(d.getUTCFullYear()).padStart(4, '0')
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${dd}`
}

/** The calendar days an event covers: its first and last day key and
 *  how many days that is. ``null`` when ``start`` is unparseable. Every
 *  calendar view (Upcoming, Month, Week — household and space) places an
 *  event by these rules:
 *
 *  * **Day keys, not ``Date`` cursors.** ``YYYY-MM-DD`` keys sort
 *    lexicographically == chronologically in every locale, and calendar
 *    arithmetic over them (``keyOrdinal``) never skips or repeats a day
 *    across a 23- / 25-hour DST day.
 *  * **All-day → the event's own tz, timed → the viewer's.** All-day
 *    rows are stored as ``00:00`` / ``23:59`` in ``event.tz`` (see
 *    ``components/CalendarEventDialog.tsx``), so their days are read in
 *    that zone — ``'UTC'`` when the row carries none (the server default
 *    and the zone legacy rows were computed in). Timed rows are
 *    wall-clock events for whoever is looking.
 *  * **A midnight end is exclusive** — ``22:00 → 00:00`` is one evening,
 *    not two days (also ICS-style exclusive ``00:00`` end bounds).
 *  * **Degenerate ends fail soft** — a missing, unparseable or backwards
 *    ``end`` makes it a single-day event on the start day. */
export function eventDaySpan(
  e: CalendarEvent,
): { startKey: string; endKey: string; dayCount: number } | null {
  const allDay = e.all_day === true
  const tz = allDay ? safeTimeZone(e.tz) : ''
  const partsOf = (iso: string): DayParts | null =>
    allDay ? zonedParts(iso, tz) : viewerParts(iso)

  const startParts = partsOf(e.start)
  if (!startParts) return null
  const startKey = startParts.key

  // Last covered day. Anything degenerate (absent, unparseable, or not
  // after the start) collapses to the start day.
  let endKey = startKey
  const endParts = e.end ? partsOf(e.end) : null
  if (endParts
    && new Date(e.end as string).getTime() > new Date(e.start).getTime()) {
    // A midnight end belongs to the previous day (exclusive bound).
    const candidate = endParts.time === '00:00'
      ? keyFromOrdinal(keyOrdinal(endParts.key) - 1)
      : endParts.key
    if (candidate > endKey) endKey = candidate
  }
  return { startKey, endKey, dayCount: keyOrdinal(endKey) - keyOrdinal(startKey) + 1 }
}

/** ``key`` moved by ``days`` whole calendar days (``YYYY-MM-DD``). */
export function addDaysToKey(key: string, days: number): string {
  return keyFromOrdinal(keyOrdinal(key) + days)
}

/** Whole days from ``a`` to ``b`` (both ``YYYY-MM-DD``). */
export function daysBetweenKeys(a: string, b: string): number {
  return keyOrdinal(b) - keyOrdinal(a)
}

/** ``YYYY-MM-DD`` of ``d``'s local calendar day. */
export function dayKeyOf(d: Date): string {
  return localDateKey(d)
}

/** The two bound labels of the expanded event detail — see
 *  :func:`formatEventBounds`. */
export interface EventBounds {
  /** Rendering of the event's start bound. */
  starts: string
  /** Rendering of the event's end bound. Falls back to
   *  :attr:`starts` when the row has no parseable ``end``. */
  ends: string
}

/** "Starts" / "Ends" values for the expanded event card on the
 *  household calendar and the per-space calendar tab.
 *
 *  An **all-day** row renders as a full DATE ONLY, read in the event's
 *  own ``tz`` — the same convention :func:`groupEventsByDay` /
 *  :func:`formatDayPortion` document: the composer stores all-day rows
 *  as ``00:00`` / ``23:59`` in ``event.tz``, so reading their
 *  components in any other zone shifts the day (a 10–12 September
 *  Zurich span reads as starting the 9th in UTC). And an all-day event
 *  has no wall clock to quote, so none is printed.
 *
 *  A **timed** row keeps the plain ``toLocaleString()`` rendering in
 *  the viewer's zone — correct by construction for a wall-clock event.
 *
 *  Degenerate rows fail soft, matching the rest of this module: a
 *  missing / unparseable ``end`` reuses the start rendering rather than
 *  printing "Invalid Date", and an unparseable ``start`` is echoed raw.
 */
export function formatEventBounds(event: CalendarEvent): EventBounds {
  const startDate = new Date(event.start)
  if (Number.isNaN(startDate.getTime())) {
    return { starts: event.start, ends: event.start }
  }
  if (event.all_day !== true) {
    const starts = startDate.toLocaleString(formatLocale())
    const endDate = event.end ? new Date(event.end) : null
    const ends = endDate && !Number.isNaN(endDate.getTime())
      ? endDate.toLocaleString(formatLocale())
      : starts
    return { starts, ends }
  }
  const zone = safeTimeZone(event.tz)
  const day = (d: Date) =>
    d.toLocaleDateString(formatLocale(), { dateStyle: 'full', timeZone: zone })
  const starts = day(startDate)
  const rawEnd = event.end ? new Date(event.end) : null
  // The exclusive-end rule again (:func:`lastInclusiveMoment`): an
  // ICS-imported all-day row carries an exclusive ``00:00`` end, which
  // would otherwise name the following day.
  const ends = rawEnd && !Number.isNaN(rawEnd.getTime())
    ? day(lastInclusiveMoment(event.end as string))
    : starts
  return { starts, ends }
}

/** Merge same-event rows that landed on multiple calendars.
 *
 *  The composer fans out a multi-attendee event as one ``POST`` per
 *  picked calendar — so a "Family dinner" assigned to both Pascal and
 *  Maria lands as **two separate rows** in the DB, with different
 *  ``id`` and ``calendar_id`` but identical content. With both
 *  calendars visible the agenda would otherwise render two near-
 *  identical cards stacked.
 *
 *  Two grouping strategies, applied in order:
 *
 *   1. **client_event_uuid (intent-driven, issue #327)** — the
 *      composer mints one v4 UUID before the fan-out + stamps it on
 *      every ``POST`` in the batch. Rows that share a uuid merge
 *      unconditionally — edits that diverge title / description /
 *      location on one row still group correctly.
 *   2. **Content-key fallback** — for legacy / externally-imported
 *      rows without a uuid: ``(summary, start, end, created_by,
 *      description, location, cover_url)``. The extra fields beyond
 *      ``(summary, start, end)`` keep genuinely-different same-minute
 *      twins (two parallel "tennis lessons" with different teachers)
 *      separate.
 *
 *  Returned rows are clones of the primary row (the first one in the
 *  group whose ``calendar_id`` is owned by ``created_by``, or just the
 *  first row when no calendar matches). ``_grouped_calendar_ids`` /
 *  ``_grouped_event_ids`` carry the underlying ids for the chip
 *  render and click-routing.
 *
 *  Those two arrays are a RENDER artifact of the rows passed in (i.e.
 *  the visible calendars only). The server-built ``copies`` field
 *  rides along on the cloned primary untouched and supersedes them
 *  for anything that drives writes — see ``CalendarEvent.copies``.
 */
export function groupSharedEvents(
  evts: CalendarEvent[],
  calendars: { id: string; owner_username: string }[] = [],
): CalendarEvent[] {
  if (evts.length === 0) return evts
  const calOwner = new Map<string, string>()
  for (const c of calendars) calOwner.set(c.id, c.owner_username)
  const groups = new Map<string, CalendarEvent[]>()
  const order: string[] = []
  for (const e of evts) {
    // Prefer the client-stamped uuid when present — that's the
    // intent-driven signal. Fall back to the content key for legacy
    // / sub-version-peer rows that don't carry it.
    const key = e.client_event_uuid
      ? `uuid:${e.client_event_uuid}`
      : [
          'content',
          e.summary,
          e.start,
          e.end,
          e.created_by,
          e.description ?? '',
          e.location ?? '',
          e.cover_url ?? '',
        ].join('\x1f')
    const bucket = groups.get(key)
    if (bucket) {
      bucket.push(e)
    } else {
      groups.set(key, [e])
      order.push(key)
    }
  }
  const out: CalendarEvent[] = []
  for (const key of order) {
    const bucket = groups.get(key)!
    if (bucket.length === 1) {
      out.push(bucket[0])
      continue
    }
    // Primary = the row whose calendar belongs to the creator. Falls
    // back to the first row when no calendar matches (e.g. the
    // creator's row was filtered out of the visible set).
    const primary =
      bucket.find(e => calOwner.get(e.calendar_id) === e.created_by) ?? bucket[0]
    out.push({
      ...primary,
      _grouped_calendar_ids: bucket.map(e => e.calendar_id),
      _grouped_event_ids: bucket.map(e => e.id),
    })
  }
  return out
}

/** Heading for a month-view month strip — "April 2026", localised. */
export function formatMonthHeading(date: Date): string {
  return date.toLocaleDateString(formatLocale(), {
    month: 'long',
    year: 'numeric',
  })
}

/** ISO bounds for the calendar month containing ``date``. */
export function monthRange(date: Date): { start: string; end: string } {
  const start = new Date(date.getFullYear(), date.getMonth(), 1)
  const end = new Date(date.getFullYear(), date.getMonth() + 1, 0, 23, 59, 59)
  return { start: start.toISOString(), end: end.toISOString() }
}

/** Heading shown in the controls strip for the active period. */
export function formatRangeHeading(
  date: Date,
  mode: CalendarViewMode,
  weekStart: WeekStart = currentWeekStart(),
): string {
  if (mode === 'month') return formatMonthHeading(date)
  if (mode === 'week') {
    const start = startOfWeek(date, weekStart)
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    return `${start.toLocaleDateString(formatLocale(), {
      month: 'short', day: 'numeric',
    })} – ${end.toLocaleDateString(formatLocale(), {
      month: 'short', day: 'numeric', year: 'numeric',
    })}`
  }
  return date.toLocaleDateString(formatLocale(), {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  })
}

/** ``date`` advanced by ``direction`` units of ``mode`` (-1 = back). */
export function advanceDate(
  date: Date,
  direction: number,
  mode: CalendarViewMode,
): Date {
  const next = new Date(date)
  if (mode === 'month') next.setMonth(next.getMonth() + direction)
  else if (mode === 'week') next.setDate(next.getDate() + 7 * direction)
  else next.setDate(next.getDate() + direction)
  return next
}

/** Deterministic colour per calendar id — picks one of 16 hand-tuned
 *  earth-tone hues so two members rarely collide visually. The same id
 *  always lands on the same colour across reloads / sessions. 16 is
 *  enough for any realistic household; if a household ever has 17+
 *  calendars the chip names still disambiguate. */
const _CAL_HUES = [
  'var(--sh-primary)',  // terracotta
  'var(--sh-success)',  // moss
  'var(--sh-warning)',  // honey
  'var(--sh-danger)',   // brick
  '#7B5BA8',            // plum
  '#3F7B8C',            // dusty teal
  '#A89344',            // ochre
  '#5C7B5A',            // sage
  '#9B5B3F',            // cinnamon
  '#34688D',            // navy
  '#7C9D5F',            // olive
  '#B57E47',            // amber
  '#5B8E8E',            // slate teal
  '#8C5777',            // rose-plum
  '#46735A',            // pine
  '#BC6C68',            // brick rose
] as const

export function calendarHue(calId: string): string {
  // Tiny string-hash → pick a hue. djb2-flavoured.
  let h = 5381
  for (let i = 0; i < calId.length; i++) {
    h = ((h << 5) + h + calId.charCodeAt(i)) | 0
  }
  return _CAL_HUES[Math.abs(h) % _CAL_HUES.length]
}

/** Resolve the chip dot colour. The DB column wins when the owner has
 *  picked one. Both legacy "default-blue" sentinels (``#4A90E2`` from the
 *  schema, ``#2196F3`` from an earlier service default) are treated as
 *  "unset" so the warm hash-derived palette takes over — leaving every
 *  fresh calendar a cold-blue chip read as generic and stale against
 *  the hearth surface. */
const _UNSET_CAL_COLORS = new Set(['#4a90e2', '#2196f3'])
export function resolveCalendarColor(
  c: { id: string; color?: string | null },
): string {
  if (c.color && !_UNSET_CAL_COLORS.has(c.color.toLowerCase())) return c.color
  return calendarHue(c.id)
}
