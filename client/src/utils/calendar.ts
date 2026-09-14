/**
 * Calendar formatting / grouping helpers shared by the household
 * calendar (`features/calendar/CalendarPage.tsx`) and the per-space
 * calendar tab (`features/spaces/SpaceFeedPage.tsx`). Keeping the
 * grouping rule in one place means the two surfaces always render the
 * same day buckets — no drift between the household and a space.
 */
import { t } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import { utcIsoToLocalParts } from './timezone'

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

/** Calendar view modes — mirrored on the household and per-space
 *  calendar surfaces so date math + range labels can come from one
 *  helper module. */
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

/** Inverse of :func:`keyOrdinal`. */
function keyFromOrdinal(ordinal: number): string {
  const d = new Date(ordinal * 86_400_000)
  const y = String(d.getUTCFullYear()).padStart(4, '0')
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${dd}`
}

/** Runaway guard for :func:`groupEventsByDay`. A bad ICS import (or a
 *  typo'd year in the composer) can produce a decade-long "event";
 *  without a cap the day-expansion would mint thousands of cards. The
 *  entry still reports the TRUE ``dayCount`` so the UI can say
 *  "day 1 of 4000" — we just stop emitting cards. */
const MAX_SPAN_DAYS = 366

/** One day-card's worth of an event. A multi-day event yields one
 *  entry per covered day, all pointing at the SAME row. */
export interface DayEventEntry {
  /** The original event row — never a clone with a rewritten
   *  ``start`` / ``end``. Downstream code keys the RSVP
   *  ``occurrence_at`` off ``event.start``, so a per-day clone with a
   *  shifted start would silently RSVP the wrong occurrence. */
  event: CalendarEvent
  /** 1-based position of this day within the event's full span. */
  dayIndex: number
  /** Total days the event covers — 1 for an ordinary same-day event.
   *  Always the TRUE span, even when the visible-range clamp or
   *  :data:`MAX_SPAN_DAYS` suppressed some of the day cards. */
  dayCount: number
  /** ``dayIndex === 1`` — the day the event actually starts. */
  isFirst: boolean
  /** ``dayIndex === dayCount`` — the day the event ends. */
  isLast: boolean
}

/** Group events into ``{ "YYYY-MM-DD" → day entries }`` buckets. The
 *  key is built from date components rather than
 *  ``toLocaleDateString()`` because the previous shape (``5/14/2026`` /
 *  ``14.5.2026``) couldn't be round-tripped through ``new Date()`` in
 *  non-en-* locales, so the agenda sort silently fell back to insertion
 *  order. With ``YYYY-MM-DD`` the sort is locale-independent
 *  (lexicographic == chronological).
 *
 *  A **multi-day** event lands in every day it covers, not just its
 *  start day — the agenda is a list of per-day cards, so a Fri–Sun trip
 *  filed only under Friday is invisible when you look at Saturday. Each
 *  entry carries its ``dayIndex`` / ``dayCount`` so the card can render
 *  "Day 2 of 3" and drop the start time on continuation days.
 *
 *  The span is walked as **calendar arithmetic over the two
 *  ``YYYY-MM-DD`` keys** (`keyOrdinal` ± whole days), never by
 *  cursoring a ``Date``: a date string carries no clock, so a 23- or
 *  25-hour DST day can neither skip nor duplicate a key.
 *
 *  Conventions, each matching what already ships elsewhere:
 *
 *  * **All-day → the event's own tz, timed → the viewer's.** All-day
 *    rows are stored as ``00:00`` / ``23:59`` in ``event.tz`` (see
 *    ``components/CalendarEventDialog.tsx``), so their day keys are
 *    read back in that same zone — ``'UTC'`` when the row carries no
 *    ``tz``, which is the server default and the zone legacy rows were
 *    computed in. Timed rows are wall-clock events for whoever is
 *    looking, so they bucket in the viewer's zone.
 *  * **A midnight end is exclusive.** ``22:00 → 00:00`` is one evening,
 *    not two days: an end landing exactly on midnight (in the same zone
 *    its key was read in) belongs to the day before it. This also
 *    handles ICS-style exclusive ``00:00`` end bounds.
 *  * **Degenerate rows fail soft.** A missing / unparseable / backwards
 *    ``end`` degrades to a single-day event on the start day. An
 *    unparseable ``start`` is skipped entirely rather than minting a
 *    ``NaN-NaN-NaN`` bucket.
 *
 *  ``visibleRange`` (as produced by :func:`dateRangeForMode`) clamps the
 *  emitted keys to the visible period — the server's range query is
 *  overlap-based, so an event starting before the range comes back and
 *  would otherwise file a stray out-of-range day card (an April card in
 *  a May view). The clamp is measured in **viewer calendar days** for
 *  both conventions: the keyspace is calendar-day strings and
 *  :func:`formatDayLabel` renders each key as a viewer calendar day, so
 *  "which days are on screen" is a viewer-zone question even for a key
 *  the event's own zone produced. The walk *starts* inside the range
 *  rather than skipping its way in, so a span longer than :data:`MAX_SPAN_DAYS` whose
 *  visible days sit beyond day 366 still files cards inside the range.
 *  The clamp is **fail-soft**: if the clamped range is genuinely empty,
 *  the event still lands on its own start day. The visible range and the
 *  rows held in the caller's ``events`` signal can legitimately disagree
 *  — WS frames accrete rows for other periods into the same cache, and a
 *  fetch can land after the user has navigated — and a hard clamp would
 *  silently swallow those. Clamping never changes ``dayIndex`` /
 *  ``dayCount``, so a clipped span still reads "day 4 of 6".
 *
 *  Within a bucket, continuation entries come first — a running
 *  multi-day event is standing context for the day — then the events
 *  that actually start that day, sorted by start time so a
 *  multi-calendar overlay (the household page does one fetch per
 *  calendar then ``.flat()``s the results) doesn't surface in
 *  calendar-arrival order. */
export function groupEventsByDay(
  evts: CalendarEvent[],
  visibleRange?: { start: string; end: string },
): Record<string, DayEventEntry[]> {
  const groups: Record<string, DayEventEntry[]> = {}
  // The visible window is a span of VIEWER calendar days, for BOTH
  // conventions — see the clamp below — so derive its bounds once.
  const viewerLo = visibleRange ? viewerParts(visibleRange.start)?.key : undefined
  const viewerHi = visibleRange ? viewerParts(visibleRange.end)?.key : undefined
  for (const e of evts) {
    const allDay = e.all_day === true
    const tz = allDay ? safeTimeZone(e.tz) : ''
    const partsOf = (iso: string): DayParts | null =>
      allDay ? zonedParts(iso, tz) : viewerParts(iso)

    const startParts = partsOf(e.start)
    if (!startParts) continue
    const startKey = startParts.key

    // Last covered day. Anything degenerate (absent, unparseable, or
    // not after the start) collapses to the start day.
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

    const startOrdinal = keyOrdinal(startKey)
    const endOrdinal = keyOrdinal(endKey)
    const dayCount = endOrdinal - startOrdinal + 1

    // Clamp to the visible range in the VIEWER's calendar days —
    // whichever convention produced the key. The bucket keyspace is
    // calendar-day STRINGS and ``formatDayLabel`` renders each key as
    // a viewer calendar day, so "is this day inside the visible
    // period" is a question about viewer days. The event's own zone
    // decides WHICH day an all-day event falls on; the viewer's period
    // decides WHICH days are on screen. (Re-reading the range bounds
    // in ``event.tz`` landed a day off for any viewer west of the
    // event's zone — a Zurich all-day 30 May – 2 Jun leaked a 1 June
    // card into a May view at plain UTC.) ``dateRangeForMode`` builds
    // its bounds from LOCAL components before ``toISOString()``, so
    // reading them back in the viewer's zone recovers exactly the
    // first / last local day of the period.
    //
    // The walk also STARTS inside the range — beginning at the event's
    // own start day would burn the MAX_SPAN_DAYS budget on invisible
    // days and emit nothing for a genuinely-overlapping decade-long
    // row.
    const from = viewerLo
      ? Math.max(startOrdinal, keyOrdinal(viewerLo)) : startOrdinal
    const to = viewerHi
      ? Math.min(endOrdinal, keyOrdinal(viewerHi)) : endOrdinal

    let emittedAny = false
    for (let o = from; o <= to && o - from < MAX_SPAN_DAYS; o++) {
      const dayIndex = o - startOrdinal + 1
      const entry: DayEventEntry = {
        event: e,
        dayIndex,
        dayCount,
        isFirst: dayIndex === 1,
        isLast: dayIndex === dayCount,
      }
      const key = keyFromOrdinal(o)
      if (!groups[key]) groups[key] = []
      groups[key].push(entry)
      emittedAny = true
    }
    // Fail-soft: a zero-overlap clamp must not make the event vanish.
    if (!emittedAny) {
      if (!groups[startKey]) groups[startKey] = []
      groups[startKey].push({
        event: e,
        dayIndex: 1,
        dayCount,
        isFirst: true,
        isLast: dayCount === 1,
      })
    }
  }
  for (const key of Object.keys(groups)) {
    groups[key].sort((a, b) => {
      // Continuations first, then starters by start time.
      if (a.isFirst !== b.isFirst) return a.isFirst ? 1 : -1
      return new Date(a.event.start).getTime()
        - new Date(b.event.start).getTime()
    })
  }
  return groups
}

/** Row labels for one day-card of an event — see
 *  :func:`formatDayPortion`. */
export interface DayPortionLabels {
  /** Row label. Multi-day: "1 – 3 May · from 16:00". Ordinary timed
   *  event: "16:00". Single-day all-day event: "" (the existing
   *  "All day" badge already says it). */
  when: string
  /** Span badge — "Starts" / "Day 2 of 3" / "Ends". ``null`` for a
   *  single-day event, which needs no badge. */
  badge: string | null
  /** Screen-reader summary of the row's place in the span. ``null``
   *  for a single-day event. */
  aria: string | null
}

/** ``{ hour: '2-digit', minute: '2-digit' }`` clock for ``iso`` in the
 *  VIEWER's zone — no ``timeZone`` option at all.
 *
 *  Only timed rows ever get a clock: :func:`formatDayPortion` calls
 *  this exclusively on its ``!allDay`` paths, because an all-day row
 *  has no wall clock to quote (its ``00:00`` / ``23:59`` are storage
 *  artefacts, not something the host typed). And a timed row IS a
 *  wall-clock event for whoever is looking — the same convention
 *  :func:`groupEventsByDay` buckets timed rows with, so the printed
 *  clock and the day card it sits under can't disagree. */
function clockIn(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  })
}

/** Per-day labels for one :interface:`DayEventEntry` — the row that
 *  renders under an agenda day heading (which already states the
 *  date), e.g. ``"1 – 3 May · from 16:00"`` + a ``"Starts"`` badge.
 *
 *  A **single-day** event gets no badge and no aria summary: an
 *  ordinary timed event keeps exactly the plain start time the row has
 *  always shown, and a single-day all-day event gets an EMPTY label
 *  (its "All day" badge already says everything a ``00:00`` clock
 *  would pretend to).
 *
 *  A **multi-day** event gets ``"<range> · <portion>"``: the compact
 *  localized range via ``Intl.DateTimeFormat.formatRange`` (which
 *  collapses the shared month/year per locale — ``"1 – 3 May"`` on
 *  en-GB, ``"May 1 – 3"`` on en-US, ``"28. Apr. – 3. Mai"`` on de-DE;
 *  hand-rolling it from two ``toLocaleDateString`` calls and a dash is
 *  what makes those read wrong), plus the day-specific clock hint —
 *  "from HH:MM" on the first day, "until HH:MM" on the last, "all day"
 *  for every day of an all-day span and for the middle days of a timed
 *  one (the event does cover those whole days). */
export function formatDayPortion(entry: DayEventEntry): DayPortionLabels {
  const { event, dayIndex, dayCount, isFirst, isLast } = entry
  const allDay = event.all_day === true
  // ``undefined`` means "the viewer's zone" to Intl — the convention
  // for a timed row. An all-day row is anchored on its own tz.
  const zone = allDay ? safeTimeZone(event.tz) : undefined
  const startDate = new Date(event.start)

  if (dayCount <= 1) {
    return {
      when: allDay ? '' : clockIn(event.start),
      badge: null,
      aria: null,
    }
  }

  // The exclusive-end rule (:func:`lastInclusiveMoment`) — so a
  // ``22:00 → next-day 00:00`` span can't print a range one day longer
  // than the midnight-exclusive ``dayCount`` the badge claims.
  const effectiveEnd = lastInclusiveMoment(event.end as string)
  const range = new Intl.DateTimeFormat(undefined, {
    day: 'numeric', month: 'short', timeZone: zone,
  }).formatRange(startDate, effectiveEnd)

  // ``isFirst`` wins a degenerate ``isFirst && isLast`` entry (which
  // can't occur for ``dayCount > 1``, but must not crash if it does).
  const portion = allDay
    ? t('event.span.all_day')
    : isFirst
      ? t('event.span.from', { time: clockIn(event.start) })
      : isLast
        ? t('event.span.until', {
            time: clockIn(event.end as string),
          })
        : t('event.span.all_day')

  const badge = isFirst
    ? t('event.span.starts')
    : isLast
      ? t('event.span.ends')
      : t('event.span.day_of', {
          n: String(dayIndex), total: String(dayCount),
        })

  return {
    when: `${range} · ${portion}`,
    badge,
    aria: t('event.span.aria', {
      range, n: String(dayIndex), total: String(dayCount),
    }),
  }
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
    const starts = startDate.toLocaleString()
    const endDate = event.end ? new Date(event.end) : null
    const ends = endDate && !Number.isNaN(endDate.getTime())
      ? endDate.toLocaleString()
      : starts
    return { starts, ends }
  }
  const zone = safeTimeZone(event.tz)
  const day = (d: Date) =>
    d.toLocaleDateString(undefined, { dateStyle: 'full', timeZone: zone })
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

/** Friendly day-group label — "Today · Friday 8 May" / "Tomorrow ·
 *  Saturday 9 May" / "Mon 12 May" rather than the spreadsheet-y
 *  ``5/8/2026`` the toLocaleDateString default emits. ``dayKey`` is a
 *  ``YYYY-MM-DD`` bucket key (the shape :func:`groupEventsByDay`
 *  emits), parsed as a LOCAL date below. */
export interface FriendlyDayLabel {
  /** Long form for visual rendering: "Friday 8 May" */
  long: string
  /** Relative descriptor for the kicker: "Today" / "Tomorrow" / null */
  relative: string | null
  /** True when the bucket is the local current day. */
  isToday: boolean
}
export function formatDayLabel(dayKey: string): FriendlyDayLabel {
  // Parse ``YYYY-MM-DD`` (the shape :func:`groupEventsByDay` emits) as
  // a LOCAL date — ``new Date('2026-05-14')`` would treat it as UTC
  // midnight and bump the rendered day back by one for viewers west of
  // UTC. Any other shape falls through to the generic ``new Date()``
  // path so legacy callers / tests that still pass
  // ``toLocaleDateString()`` output keep working.
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dayKey)
  const date = iso
    ? new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]))
    : new Date(dayKey)
  if (Number.isNaN(date.getTime())) {
    return { long: dayKey, relative: null, isToday: false }
  }
  const today = new Date()
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate()
  const tomorrow = new Date(today)
  tomorrow.setDate(today.getDate() + 1)
  const isToday = sameDay(date, today)
  const isTomorrow = sameDay(date, tomorrow)
  const long = date.toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })
  const relative = isToday
    ? 'Today'
    : isTomorrow
      ? 'Tomorrow'
      : null
  return { long, relative, isToday }
}

/** Heading for a month-view month strip — "April 2026", localised. */
export function formatMonthHeading(date: Date): string {
  return date.toLocaleDateString(undefined, {
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

/** ISO bounds covering the active period for ``mode`` anchored at
 *  ``date``. Month → calendar month; week → Sun-Sat; day → 00:00 to
 *  23:59:59. Used by both the household calendar and the per-space
 *  calendar's view-mode switcher. */
export function dateRangeForMode(
  date: Date,
  mode: CalendarViewMode,
): { start: string; end: string } {
  if (mode === 'month') return monthRange(date)
  const d = new Date(date)
  if (mode === 'week') {
    const dayOfWeek = d.getDay()
    const start = new Date(d)
    start.setDate(d.getDate() - dayOfWeek)
    start.setHours(0, 0, 0, 0)
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    end.setHours(23, 59, 59, 0)
    return { start: start.toISOString(), end: end.toISOString() }
  }
  // day
  const start = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59)
  return { start: start.toISOString(), end: end.toISOString() }
}

/** Heading shown in the controls strip for the active period. */
export function formatRangeHeading(
  date: Date,
  mode: CalendarViewMode,
): string {
  if (mode === 'month') return formatMonthHeading(date)
  if (mode === 'week') {
    const start = new Date(date)
    start.setDate(date.getDate() - date.getDay())
    const end = new Date(start)
    end.setDate(start.getDate() + 6)
    return `${start.toLocaleDateString(undefined, {
      month: 'short', day: 'numeric',
    })} – ${end.toLocaleDateString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
    })}`
  }
  return date.toLocaleDateString(undefined, {
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
