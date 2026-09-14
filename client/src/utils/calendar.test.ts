import { describe, it, expect } from 'vitest'
import {
  formatDayLabel,
  formatDayPortion,
  formatEventBounds,
  formatMonthHeading,
  groupEventsByDay,
  groupSharedEvents,
  lastInclusiveMoment,
  monthRange,
  type DayEventEntry,
} from './calendar'
import type { CalendarEvent } from '@/types'

interface SharedEvtOpts {
  calendar_id?: string
  summary?: string
  start?: string
  end?: string
  created_by?: string
  description?: string | null
  location?: string | null
  cover_url?: string | null
}
function sharedEvt(id: string, options: SharedEvtOpts = {}): CalendarEvent {
  return {
    id,
    calendar_id: 'cal-a',
    summary: 's',
    description: null,
    start: '2026-05-15T18:00:00Z',
    end: '2026-05-15T19:00:00Z',
    all_day: false,
    rrule: null,
    capacity: null,
    created_by: 'u-alice',
    location: null,
    cover_url: null,
    ...options,
  } as unknown as CalendarEvent
}

/** Event ids of a day bucket, in bucket order. */
const ids = (rows: DayEventEntry[] | undefined) =>
  (rows ?? []).map(r => r.event.id)

function evt(id: string, startISO: string): CalendarEvent {
  return {
    id,
    calendar_id: 'cal-1',
    summary: id,
    description: null,
    start: startISO,
    end: startISO,
    all_day: false,
    rrule: null,
    capacity: null,
    created_by: 'u-1',
  } as unknown as CalendarEvent
}

interface SpanEvtOpts {
  all_day?: boolean
  tz?: string
}
/** Multi-day fixture — ``evt`` pins ``end === start`` so it can't
 *  express a span. */
function spanEvt(
  id: string,
  startISO: string,
  endISO: string | undefined,
  options: SpanEvtOpts = {},
): CalendarEvent {
  return {
    id,
    calendar_id: 'cal-1',
    summary: id,
    description: null,
    start: startISO,
    end: endISO,
    all_day: false,
    rrule: null,
    capacity: null,
    created_by: 'u-1',
    ...options,
  } as unknown as CalendarEvent
}

describe('calendar utils', () => {
  it('groups events by their local-date key, preserving order within a day', () => {
    const a = evt('a', '2026-04-30T08:00:00')
    const b = evt('b', '2026-04-30T15:00:00')
    const c = evt('c', '2026-05-01T09:00:00')
    const groups = groupEventsByDay([a, b, c])
    const keys = Object.keys(groups)
    expect(keys.length).toBe(2)
    expect(ids(groups[keys[0]])).toEqual(['a', 'b'])
    expect(ids(groups[keys[1]])).toEqual(['c'])
  })

  it('emits locale-independent YYYY-MM-DD keys that sort chronologically', () => {
    // Regression for the multi-day agenda landing out of order on
    // non-en-* locales (de-DE / en-GB / fr-FR / …). The old key
    // shape was ``toLocaleDateString()`` and the sort tried to
    // round-trip via ``new Date(key)``, which returns NaN for
    // ``14.5.2026`` / ``14/05/2026`` — V8's stable sort then left the
    // insertion order untouched. Locking in the YYYY-MM-DD shape so a
    // plain lexicographic sort is enough.
    const e21 = evt('e21', '2026-05-21T10:00:00')
    const e14 = evt('e14', '2026-05-14T10:00:00')
    const e19 = evt('e19', '2026-05-19T10:00:00')
    // Order chosen to match the bug report: keys land in
    // [14, 21, 19] order, which the broken sort previously left
    // untouched on non-en-US locales.
    const groups = groupEventsByDay([e14, e21, e19])
    const keys = Object.keys(groups).sort()
    expect(keys).toEqual(['2026-05-14', '2026-05-19', '2026-05-21'])
    // formatDayLabel must round-trip the new key shape as a LOCAL
    // date — ``new Date('2026-05-14')`` would otherwise be UTC
    // midnight and bump the rendered day back by one west of UTC.
    expect(formatDayLabel('2026-05-14').long).toContain('14')
  })

  it('sorts events within a day by start time even when inputs are scrambled', () => {
    // The household calendar fans one fetch per visible calendar and
    // then ``.flat()``s the results — when two visible calendars each
    // contribute events to the same day, the per-day order ends up
    // calendar-arrival order unless we sort inside the bucket.
    const afternoon = evt('afternoon', '2026-05-14T15:00:00')
    const morning = evt('morning', '2026-05-14T08:00:00')
    const noon = evt('noon', '2026-05-14T12:00:00')
    const groups = groupEventsByDay([afternoon, morning, noon])
    const keys = Object.keys(groups)
    expect(keys).toHaveLength(1)
    expect(ids(groups[keys[0]])).toEqual([
      'morning', 'noon', 'afternoon',
    ])
  })

  it('formats a month heading with month name + year', () => {
    const heading = formatMonthHeading(new Date('2026-04-15T00:00:00'))
    expect(heading.toLowerCase()).toContain('april')
    expect(heading).toContain('2026')
  })

  it('returns ISO bounds covering the whole calendar month', () => {
    const { start, end } = monthRange(new Date('2026-04-15T12:00:00'))
    expect(new Date(start).getDate()).toBe(1)
    expect(new Date(start).getMonth()).toBe(3) // April = 3 (0-indexed)
    // Last day of April is the 30th.
    expect(new Date(end).getDate()).toBe(30)
    expect(new Date(end).getMonth()).toBe(3)
  })
})

describe('formatDayLabel', () => {
  // Helper mirroring the production ``localDateKey`` shape — keeps
  // these tests independent of the runtime locale (the previous shape
  // ``toLocaleDateString()`` would round-trip via ``new Date()`` and
  // explode on non-en-* hosts).
  function dayKey(d: Date): string {
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    return `${y}-${m}-${dd}`
  }

  it('marks the current day with the "Today" relative kicker', () => {
    const today = new Date()
    const out = formatDayLabel(dayKey(today))
    expect(out.isToday).toBe(true)
    expect(out.relative).toBe('Today')
    // Long form contains the weekday — locale-agnostic existence check.
    expect(out.long.length).toBeGreaterThan(5)
  })

  it('marks the day after as "Tomorrow"', () => {
    const tomorrow = new Date()
    tomorrow.setDate(tomorrow.getDate() + 1)
    const out = formatDayLabel(dayKey(tomorrow))
    expect(out.isToday).toBe(false)
    expect(out.relative).toBe('Tomorrow')
  })

  it('returns null relative for far-future / past days', () => {
    const future = new Date()
    future.setDate(future.getDate() + 7)
    const out = formatDayLabel(dayKey(future))
    expect(out.isToday).toBe(false)
    expect(out.relative).toBe(null)
  })

  it('falls back to the original key on unparseable input', () => {
    const out = formatDayLabel('not-a-date')
    expect(out.long).toBe('not-a-date')
    expect(out.relative).toBe(null)
  })
})

describe('groupSharedEvents', () => {
  const cals = [
    { id: 'cal-a', owner_username: 'alice' },
    { id: 'cal-b', owner_username: 'bob' },
    { id: 'cal-c', owner_username: 'carol' },
  ]

  it('merges multi-calendar fan-out of one event into a single row', () => {
    const onAlice = sharedEvt('e-1', { calendar_id: 'cal-a' })
    const onBob = sharedEvt('e-2', { calendar_id: 'cal-b' })
    const out = groupSharedEvents([onAlice, onBob], cals)
    expect(out).toHaveLength(1)
    // Primary is the creator's row (alice owns cal-a, created_by=u-alice).
    expect(out[0].id).toBe('e-1')
    expect(out[0].calendar_id).toBe('cal-a')
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-a', 'cal-b'])
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('preserves single-row events unchanged (no group metadata added)', () => {
    const lonely = sharedEvt('lonely', { calendar_id: 'cal-a' })
    const out = groupSharedEvents([lonely], cals)
    expect(out).toHaveLength(1)
    expect(out[0]).toBe(lonely) // same reference — no clone for singletons
    expect(out[0]._grouped_calendar_ids).toBeUndefined()
  })

  it('keeps genuinely different same-minute twins separate', () => {
    // Two events at the same time / creator, different titles — NOT a
    // merge (e.g. parallel after-school activities).
    const a = sharedEvt('e-tennis', {
      calendar_id: 'cal-a', summary: 'Tennis with Pascal',
    })
    const b = sharedEvt('e-piano', {
      calendar_id: 'cal-b', summary: 'Piano with Maria',
    })
    const out = groupSharedEvents([a, b], cals)
    expect(out).toHaveLength(2)
  })

  it('keeps same-title twins with different locations separate', () => {
    const home = sharedEvt('e-home', {
      calendar_id: 'cal-a', location: 'Kitchen',
    })
    const out_a = sharedEvt('e-out', {
      calendar_id: 'cal-b', location: 'Café',
    })
    const out = groupSharedEvents([home, out_a], cals)
    expect(out).toHaveLength(2)
  })

  it('does not merge across creators even when title and time match', () => {
    const byAlice = sharedEvt('e-1', {
      calendar_id: 'cal-a', created_by: 'u-alice',
    })
    const byBob = sharedEvt('e-2', {
      calendar_id: 'cal-b', created_by: 'u-bob',
    })
    const out = groupSharedEvents([byAlice, byBob], cals)
    expect(out).toHaveLength(2)
  })

  it("falls back to the first row when the creator's calendar is hidden", () => {
    // Alice's row is missing from the input (the user filtered her
    // calendar out); only Bob's row is visible. Group still resolves
    // — Bob's row becomes the primary even though Alice created it.
    const onBob = sharedEvt('e-bob', {
      calendar_id: 'cal-b', created_by: 'u-alice',
    })
    const onCarol = sharedEvt('e-carol', {
      calendar_id: 'cal-c', created_by: 'u-alice',
    })
    const out = groupSharedEvents([onBob, onCarol], cals)
    expect(out).toHaveLength(1)
    expect(out[0].id).toBe('e-bob') // first row in input
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-b', 'cal-c'])
  })

  it('returns an empty array unchanged', () => {
    expect(groupSharedEvents([], cals)).toEqual([])
  })

  // ── client_event_uuid path (issue #327) ──────────────────────────────

  it('merges rows that share a client_event_uuid even when titles diverge', () => {
    // The intent-driven path: same uuid → same event, no matter what
    // the user did to the title / description / location on one row
    // post-creation. Without the uuid this case would split because
    // the content key disagrees.
    const aliceRow = sharedEvt('e-1', {
      calendar_id: 'cal-a',
      summary: 'Family dinner',
    } as SharedEvtOpts & { client_event_uuid?: string }) as CalendarEvent
    ;(aliceRow as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const bobRow = sharedEvt('e-2', {
      calendar_id: 'cal-b',
      summary: 'Family dinner (edited only on Bob\'s row)',
    } as SharedEvtOpts) as CalendarEvent
    ;(bobRow as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const out = groupSharedEvents([aliceRow, bobRow], cals)
    expect(out).toHaveLength(1)
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-a', 'cal-b'])
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('carries the server-authoritative `copies` through onto the merged row', () => {
    // ``copies`` is visibility-independent, so it can name calendars
    // that were never loaded (cal-c here). The grouper must pass it
    // through untouched — the edit dialog writes from it, while
    // ``_grouped_*`` only ever reflects the rows just rendered.
    const copies = [
      { event_id: 'e-1', calendar_id: 'cal-a', owner_username: 'alice' },
      { event_id: 'e-2', calendar_id: 'cal-b', owner_username: 'bob' },
      { event_id: 'e-3', calendar_id: 'cal-c', owner_username: 'carol' },
    ]
    const onAlice = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    const onBob = sharedEvt('e-2', { calendar_id: 'cal-b' }) as CalendarEvent
    onAlice.copies = copies
    onBob.copies = copies
    const out = groupSharedEvents([onAlice, onBob], cals)
    expect(out).toHaveLength(1)
    expect(out[0].copies).toEqual(copies)
    // The render artifact only knows about the two rows it merged.
    expect(out[0]._grouped_calendar_ids).toEqual(['cal-a', 'cal-b'])
  })

  it('does not merge two rows with different client_event_uuids', () => {
    // Two genuinely different events that happen to share a content
    // key — the uuid disambiguates them and keeps them separate.
    const a = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(a as { client_event_uuid?: string }).client_event_uuid
      = '11111111111111111111111111111111'
    const b = sharedEvt('e-2', { calendar_id: 'cal-b' }) as CalendarEvent
    ;(b as { client_event_uuid?: string }).client_event_uuid
      = '22222222222222222222222222222222'
    const out = groupSharedEvents([a, b], cals)
    expect(out).toHaveLength(2)
  })

  it('groups uuid-only rows together with no calendars table', () => {
    // Cross-household case: the recipient hasn't synced the host's
    // ``calendars`` row but DID receive the federation envelope.
    // ``calendars`` arg is empty; uuid alone still groups.
    const a = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(a as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const b = sharedEvt('e-2', { calendar_id: 'cal-x-remote' }) as CalendarEvent
    ;(b as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const out = groupSharedEvents([a, b], [])
    expect(out).toHaveLength(1)
    expect(out[0]._grouped_event_ids).toEqual(['e-1', 'e-2'])
  })

  it('keeps a uuid-bearing row separate from a uuid-less twin', () => {
    // Mixed case during rollout: the host stamped a uuid on the new
    // event but the recipient is on a sub-version peer that stripped
    // it. We don't try to bridge the two — content-key fallback
    // would, but the lack of uuid on one side means we can't trust
    // any cross-version merge.
    const stamped = sharedEvt('e-1', { calendar_id: 'cal-a' }) as CalendarEvent
    ;(stamped as { client_event_uuid?: string }).client_event_uuid
      = 'abcdef0123456789abcdef0123456789'
    const unstamped = sharedEvt('e-2', { calendar_id: 'cal-b' }) as CalendarEvent
    const out = groupSharedEvents([stamped, unstamped], cals)
    // One uuid-group + one content-group → two rows. The agenda
    // shows both with their own chip, which is honest about the
    // mixed-version state.
    expect(out).toHaveLength(2)
  })
})

describe('groupEventsByDay — multi-day spans', () => {
  it('expands a timed 3-day span into one entry per covered day', () => {
    // Local wall-clock strings (no ``Z``): timed rows bucket by the
    // viewer's own components, so a UTC instant would land on a
    // different local day at UTC±14 and make the test zone-dependent.
    const trip = spanEvt('trip', '2026-05-01T14:00:00', '2026-05-03T10:00:00')
    const groups = groupEventsByDay([trip])
    expect(Object.keys(groups).sort()).toEqual([
      '2026-05-01', '2026-05-02', '2026-05-03',
    ])
    const d1 = groups['2026-05-01'][0]
    const d2 = groups['2026-05-02'][0]
    const d3 = groups['2026-05-03'][0]
    expect([d1.dayIndex, d2.dayIndex, d3.dayIndex]).toEqual([1, 2, 3])
    expect([d1.dayCount, d2.dayCount, d3.dayCount]).toEqual([3, 3, 3])
    expect([d1.isFirst, d2.isFirst, d3.isFirst]).toEqual([true, false, false])
    expect([d1.isLast, d2.isLast, d3.isLast]).toEqual([false, false, true])
    // The original row is handed through untouched — RSVP
    // ``occurrence_at`` is keyed off ``event.start`` downstream.
    expect(d3.event).toBe(trip)
    expect(d3.event.start).toBe('2026-05-01T14:00:00')
  })

  it('anchors an all-day span in UTC so no off-by-one day appears', () => {
    // The fixture carries no ``tz``, so the documented ``'UTC'``
    // default applies — the same fallback legacy rows were computed
    // in. Zone-independent by construction.
    const holiday = spanEvt(
      'holiday', '2026-05-01T00:00:00Z', '2026-05-03T23:59:00Z',
      { all_day: true },
    )
    const groups = groupEventsByDay([holiday])
    expect(Object.keys(groups).sort()).toEqual([
      '2026-05-01', '2026-05-02', '2026-05-03',
    ])
    expect(groups['2026-05-03'][0].dayCount).toBe(3)
  })

  it('treats an exact-midnight end as exclusive (22:00 → 00:00 = one day)', () => {
    // Local wall clock (no ``Z``) — the midnight rule is applied in
    // the same zone the key was read in, which for a timed row is the
    // viewer's. A ``Z`` instant would be local 02:00 in Zurich and the
    // branch under test would never run.
    const late = spanEvt('late', '2026-05-01T22:00:00', '2026-05-02T00:00:00')
    const groups = groupEventsByDay([late])
    expect(Object.keys(groups)).toEqual(['2026-05-01'])
    expect(groups['2026-05-01'][0].dayCount).toBe(1)
    expect(groups['2026-05-01'][0].isLast).toBe(true)
  })

  it('clamps emitted keys to the visible window but keeps the true span', () => {
    // The server range query is overlap-based, so an event starting
    // before the visible month comes back — without the clamp it filed
    // a stray April card into a May view.
    const long = spanEvt('long', '2026-04-28T09:00:00', '2026-05-03T17:00:00')
    const visibleRange = monthRange(new Date(2026, 4, 15))
    const groups = groupEventsByDay([long], visibleRange)
    expect(Object.keys(groups).sort()).toEqual([
      '2026-05-01', '2026-05-02', '2026-05-03',
    ])
    const may1 = groups['2026-05-01'][0]
    expect(may1.dayIndex).toBe(4)
    expect(may1.dayCount).toBe(6)
    expect(may1.isFirst).toBe(false)
    expect(groups['2026-05-03'][0].dayIndex).toBe(6)
    expect(groups['2026-05-03'][0].isLast).toBe(true)
  })

  it('leaves ordinary same-day events as a single first-and-last entry', () => {
    const lunch = spanEvt('lunch', '2026-05-14T11:00:00', '2026-05-14T12:00:00')
    const groups = groupEventsByDay([lunch])
    expect(Object.keys(groups)).toEqual(['2026-05-14'])
    const row = groups['2026-05-14'][0]
    expect(row.dayCount).toBe(1)
    expect(row.dayIndex).toBe(1)
    expect(row.isFirst && row.isLast).toBe(true)
  })

  it('emits one key per calendar day across a spring-forward weekend', () => {
    // A TIMED span over the EU spring-forward night (29 March 2026),
    // expressed as local wall clock so both parsing and the day keys
    // use the viewer's components. The walk is calendar arithmetic on
    // the ``YYYY-MM-DD`` keys — a date string carries no clock, so the
    // 23-hour local day can neither skip nor duplicate a key. Holds in
    // every zone (zones without a transition simply see three ordinary
    // days).
    const weekend = spanEvt(
      'weekend', '2026-03-28T20:00:00', '2026-03-30T09:00:00',
    )
    const groups = groupEventsByDay([weekend])
    expect(Object.keys(groups).sort()).toEqual([
      '2026-03-28', '2026-03-29', '2026-03-30',
    ])
    expect(groups['2026-03-30'][0].dayCount).toBe(3)
  })

  it('files cards inside the range for a span longer than the 366-day cap', () => {
    // The walk STARTS at max(startKey, rangeLo): a runaway multi-year
    // row whose visible days sit beyond day 366 must still land in the
    // visible month, not fall through to the (out-of-range) start-day
    // fallback the cap used to force.
    const runaway = spanEvt('runaway', '2024-01-01T09:00:00', '2027-01-01T09:00:00')
    const may = monthRange(new Date(2026, 4, 15))
    const groups = groupEventsByDay([runaway], may)
    const keys = Object.keys(groups).sort()
    expect(keys[0]).toBe('2026-05-01')
    expect(keys[keys.length - 1]).toBe('2026-05-31')
    expect(keys).toHaveLength(31)
    // dayIndex / dayCount stay the TRUE span.
    expect(groups['2026-05-01'][0].dayIndex).toBe(852)
    expect(groups['2026-05-01'][0].dayCount).toBe(1097)
    expect(groups['2026-05-01'][0].isFirst).toBe(false)
  })

  it('never lets the window clamp swallow an event with no overlap', () => {
    // Fail-soft: the visible window and the rows held in the
    // ``events`` signal can legitimately disagree — WS frames accrete
    // rows for other periods into the same cache, and a fetch can land
    // after the user navigated away. A hard clamp would silently drop
    // them, so a zero-overlap event falls back to its own start day.
    const stray = spanEvt('stray', '2026-05-14T10:00:00', '2026-05-14T11:00:00')
    const september = monthRange(new Date(2026, 8, 15))
    const groups = groupEventsByDay([stray], september)
    expect(Object.keys(groups)).toEqual(['2026-05-14'])
    const row = groups['2026-05-14'][0]
    expect(row.dayIndex).toBe(1)
    expect(row.dayCount).toBe(1)
  })

  it('falls back to the start day for a multi-day event outside the window', () => {
    const away = spanEvt('away', '2026-05-14T10:00:00', '2026-05-16T11:00:00')
    const september = monthRange(new Date(2026, 8, 15))
    const groups = groupEventsByDay([away], september)
    expect(Object.keys(groups)).toEqual(['2026-05-14'])
    expect(groups['2026-05-14'][0].dayIndex).toBe(1)
    expect(groups['2026-05-14'][0].dayCount).toBe(3)
    expect(groups['2026-05-14'][0].isFirst).toBe(true)
  })

  it('renders continuation entries above the events that start that day', () => {
    // A running multi-day event is standing context for the day; the
    // things that actually start today come after it, still sorted by
    // start time.
    const running = spanEvt('running', '2026-05-01T09:00:00', '2026-05-03T09:00:00')
    const afternoon = evt('afternoon', '2026-05-02T15:00:00')
    const morning = evt('morning', '2026-05-02T08:00:00')
    const groups = groupEventsByDay([afternoon, morning, running])
    expect(ids(groups['2026-05-02'])).toEqual([
      'running', 'morning', 'afternoon',
    ])
    expect(ids(groups['2026-05-01'])).toEqual(['running'])
  })

  it('falls back to a single day when end precedes start', () => {
    const broken = spanEvt('broken', '2026-05-10T10:00:00', '2026-05-08T10:00:00')
    const groups = groupEventsByDay([broken])
    expect(Object.keys(groups)).toEqual(['2026-05-10'])
    expect(groups['2026-05-10'][0].dayCount).toBe(1)
  })

  it('falls back to a single day when end is missing or unparseable', () => {
    const noEnd = spanEvt('no-end', '2026-05-10T10:00:00', undefined)
    const junkEnd = spanEvt('junk-end', '2026-05-11T10:00:00', 'not-a-date')
    const groups = groupEventsByDay([noEnd, junkEnd])
    expect(Object.keys(groups).sort()).toEqual(['2026-05-10', '2026-05-11'])
    expect(groups['2026-05-10'][0].dayCount).toBe(1)
    expect(groups['2026-05-11'][0].dayCount).toBe(1)
  })

  it('skips an event with an unparseable start rather than bucketing NaN', () => {
    const junk = spanEvt('junk', 'not-a-date', '2026-05-02T10:00:00')
    const ok = evt('ok', '2026-05-02T10:00:00')
    const groups = groupEventsByDay([junk, ok])
    expect(Object.keys(groups)).toEqual(['2026-05-02'])
    expect(ids(groups['2026-05-02'])).toEqual(['ok'])
  })
})

describe('groupEventsByDay — all-day events anchor on the event tz', () => {
  it('keeps a Zurich single-day all-day event in exactly one bucket', () => {
    // Stored shape, verified against the composer
    // (``CalendarEventDialog.tsx`` writes 00:00 / 23:59 in the event's
    // own tz): an all-day "1 May 2026" authored in Europe/Zurich is on
    // the wire as 2026-04-30T22:00:00Z → 2026-05-01T21:59:00Z. Read by
    // UTC components that is [2026-04-30, 2026-05-01] — the "Day 1 of
    // 2" regression this round fixes.
    const holiday = spanEvt(
      'may-day', '2026-04-30T22:00:00Z', '2026-05-01T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const groups = groupEventsByDay([holiday])
    expect(Object.keys(groups)).toEqual(['2026-05-01'])
    expect(groups['2026-05-01'][0].dayCount).toBe(1)
  })

  it('expands a Zurich all-day 3-day span into its own three days', () => {
    const trip = spanEvt(
      'trip', '2026-04-30T22:00:00Z', '2026-05-03T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const groups = groupEventsByDay([trip])
    expect(Object.keys(groups).sort()).toEqual([
      '2026-05-01', '2026-05-02', '2026-05-03',
    ])
    expect(groups['2026-05-03'][0].dayCount).toBe(3)
  })

  it('anchors a west-of-UTC all-day event on its own zone', () => {
    const la = spanEvt(
      'la', '2026-05-01T07:00:00Z', '2026-05-02T06:59:00Z',
      { all_day: true, tz: 'America/Los_Angeles' },
    )
    const groups = groupEventsByDay([la])
    expect(Object.keys(groups)).toEqual(['2026-05-01'])
    expect(groups['2026-05-01'][0].dayCount).toBe(1)
  })

  it('clamps an all-day span to the VIEWER calendar days on screen', () => {
    // The bucket keyspace is calendar-day STRINGS and ``formatDayLabel``
    // renders each key as a viewer calendar day — so "is this day on
    // screen" is a question about viewer days no matter which zone
    // produced the key. Deriving the clamp bounds in the EVENT's zone
    // landed a day off for every viewer west of it: a Zurich-authored
    // all-day 30 May – 2 Jun leaked a ``2026-06-01`` card into a May
    // view at UTC and at America/Los_Angeles.
    const trip = spanEvt(
      'trip', '2026-05-29T22:00:00Z', '2026-06-02T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const may = monthRange(new Date(2026, 4, 15))
    const groups = groupEventsByDay([trip], may)
    expect(Object.keys(groups).sort()).toEqual(['2026-05-30', '2026-05-31'])
    // The clamp never rewrites the span itself.
    expect(groups['2026-05-30'][0].dayIndex).toBe(1)
    expect(groups['2026-05-30'][0].dayCount).toBe(4)
    expect(groups['2026-05-31'][0].dayIndex).toBe(2)
  })

  it('falls back to UTC for an all-day row with no tz', () => {
    const legacy = spanEvt(
      'legacy', '2026-05-01T00:00:00Z', '2026-05-01T23:59:00Z',
      { all_day: true },
    )
    const groups = groupEventsByDay([legacy])
    expect(Object.keys(groups)).toEqual(['2026-05-01'])
    expect(groups['2026-05-01'][0].dayCount).toBe(1)
  })
})

describe('formatDayPortion', () => {
  /** Same range shape the production helper builds — derived rather
   *  than hard-coded so the expectation follows the runtime default
   *  locale ("1 – 3 May" on en-GB, "May 1 – 3" on en-US). */
  function range(startISO: string, endISO: string, tz?: string): string {
    const fmt = new Intl.DateTimeFormat(undefined, {
      day: 'numeric', month: 'short', timeZone: tz,
    })
    // ``-1 ms`` = the last inclusive moment, mirroring the helper.
    return fmt.formatRange(
      new Date(startISO), new Date(new Date(endISO).getTime() - 1),
    )
  }
  const hhmm = (iso: string) =>
    new Date(iso).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit',
    })

  it('labels each day of a timed 3-day span with its own portion + badge', () => {
    const trip = spanEvt('trip', '2026-05-01T14:00:00', '2026-05-03T10:00:00')
    const groups = groupEventsByDay([trip])
    const d1 = formatDayPortion(groups['2026-05-01'][0])
    const d2 = formatDayPortion(groups['2026-05-02'][0])
    const d3 = formatDayPortion(groups['2026-05-03'][0])
    expect(d1.when).toContain('from ')
    expect(d1.when).toContain(hhmm('2026-05-01T14:00:00'))
    expect(d1.badge).toBe('Starts')
    expect(d2.when).toContain('all day')
    expect(d2.badge).toBe('Day 2 of 3')
    expect(d3.when).toContain('until ')
    expect(d3.when).toContain(hhmm('2026-05-03T10:00:00'))
    expect(d3.badge).toBe('Ends')
  })

  it('reports the same compact date range on every day of the span', () => {
    const trip = spanEvt('trip', '2026-05-01T14:00:00', '2026-05-03T10:00:00')
    const groups = groupEventsByDay([trip])
    const expected = range('2026-05-01T14:00:00', '2026-05-03T10:00:00')
    for (const key of ['2026-05-01', '2026-05-02', '2026-05-03']) {
      expect(formatDayPortion(groups[key][0]).when).toContain(expected)
    }
  })

  it('labels an ordinary same-day timed event with just its start time', () => {
    const lunch = spanEvt('lunch', '2026-05-14T11:00:00', '2026-05-14T12:00:00')
    const row = groupEventsByDay([lunch])['2026-05-14'][0]
    const out = formatDayPortion(row)
    expect(out.when).toBe(hhmm('2026-05-14T11:00:00'))
    expect(out.badge).toBe(null)
    expect(out.aria).toBe(null)
  })

  it('emits an empty label for a single-day all-day event', () => {
    // The row already carries an "All day" badge — a ``00:00`` clock
    // next to it is noise, so the page can drop the <time> element.
    const holiday = spanEvt(
      'holiday', '2026-04-30T22:00:00Z', '2026-05-01T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const row = groupEventsByDay([holiday])['2026-05-01'][0]
    const out = formatDayPortion(row)
    expect(out.when).toBe('')
    expect(out.badge).toBe(null)
  })

  it('anchors a multi-day all-day range on the event tz, not the viewer', () => {
    // Regression guard: authored as 1–3 May in Europe/Zurich, on the
    // wire as 2026-04-30T22:00Z → 2026-05-03T21:59Z. Rendering the
    // range in the viewer's zone (or UTC) would name 30 April.
    const trip = spanEvt(
      'trip', '2026-04-30T22:00:00Z', '2026-05-03T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const groups = groupEventsByDay([trip])
    const expected = range(
      '2026-04-30T22:00:00Z', '2026-05-03T21:59:00Z', 'Europe/Zurich',
    )
    for (const key of ['2026-05-01', '2026-05-02', '2026-05-03']) {
      const out = formatDayPortion(groups[key][0])
      expect(out.when).toContain('all day')
      expect(out.when).toContain(expected)
      // "30" is April's day number — must not appear anywhere.
      expect(out.when).not.toContain('30')
    }
  })

  it('keeps a midnight end off the following day in the range label', () => {
    // ``dayCount`` is 2 (midnight end is exclusive), so the range must
    // stop on the 2nd — the ``-1 ms`` effective end.
    const late = spanEvt('late', '2026-05-01T22:00:00', '2026-05-03T00:00:00')
    const groups = groupEventsByDay([late])
    const row = groups['2026-05-02'][0]
    expect(row.dayCount).toBe(2)
    expect(formatDayPortion(row).when).toContain(
      range('2026-05-01T22:00:00', '2026-05-03T00:00:00'),
    )
    expect(formatDayPortion(row).when).not.toContain('3 ')
  })

  it('describes the row position in the span for screen readers', () => {
    const trip = spanEvt('trip', '2026-05-01T14:00:00', '2026-05-03T10:00:00')
    const mid = groupEventsByDay([trip])['2026-05-02'][0]
    const out = formatDayPortion(mid)
    expect(out.aria).toContain('day 2 of 3')
  })
})

describe('formatEventBounds', () => {
  it('renders an all-day span as dates in the event tz, with no clock', () => {
    // Stored shape (``components/CalendarEventDialog.tsx``): an all-day
    // event is written as 00:00 / 23:59 in the event's OWN tz, so a
    // 10–12 September authored in Europe/Zurich is on the wire as
    // 2026-09-09T22:00Z → 2026-09-12T21:59Z. Reading those components
    // in the viewer's zone (or UTC) names the 9th and quotes a wall
    // clock the all-day event never had.
    const trip = spanEvt(
      'trip', '2026-09-09T22:00:00Z', '2026-09-12T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' },
    )
    const out = formatEventBounds(trip)
    expect(out.starts).toContain('10')
    expect(out.starts).not.toContain('9,')
    expect(out.ends).toContain('12')
    // Dates only — an all-day event has no wall clock to quote.
    expect(out.starts).not.toContain(':')
    expect(out.ends).not.toContain(':')
  })

  it('treats an exclusive 00:00 all-day end as the previous day', () => {
    // ICS-imported all-day events carry an exclusive midnight end
    // bound — same ``-1 ms`` rule ``formatDayPortion`` uses.
    const ics = spanEvt(
      'ics', '2026-09-10T00:00:00Z', '2026-09-13T00:00:00Z',
      { all_day: true, tz: 'UTC' },
    )
    const out = formatEventBounds(ics)
    expect(out.starts).toContain('10')
    expect(out.ends).toContain('12')
    expect(out.ends).not.toContain('13')
  })

  it('keeps a timed event on the viewer-zone toLocaleString output', () => {
    const meeting = spanEvt(
      'meeting', '2026-09-10T14:00:00Z', '2026-09-10T15:00:00Z',
    )
    const out = formatEventBounds(meeting)
    expect(out.starts).toBe(new Date('2026-09-10T14:00:00Z').toLocaleString())
    expect(out.ends).toBe(new Date('2026-09-10T15:00:00Z').toLocaleString())
  })

  it('falls back to the start bound when end is missing or unparseable', () => {
    const noEnd = spanEvt('no-end', '2026-09-10T14:00:00Z', undefined)
    const junk = spanEvt('junk-end', '2026-09-10T14:00:00Z', 'not-a-date')
    for (const e of [noEnd, junk]) {
      const out = formatEventBounds(e)
      expect(out.starts).toBe(new Date('2026-09-10T14:00:00Z').toLocaleString())
      expect(out.ends).toBe(out.starts)
      expect(out.ends).not.toContain('Invalid')
    }
  })

  it('returns the raw string for an unparseable start rather than throwing', () => {
    const out = formatEventBounds(spanEvt('junk', 'not-a-date', undefined))
    expect(out.starts).toBe('not-a-date')
    expect(out.ends).toBe('not-a-date')
  })
})

describe('lastInclusiveMoment', () => {
  it('is one millisecond before the exclusive end bound', () => {
    const d = lastInclusiveMoment('2026-09-13T00:00:00Z')
    expect(d.getTime()).toBe(new Date('2026-09-13T00:00:00Z').getTime() - 1)
  })

  it('fails soft on an unparseable end instead of throwing', () => {
    expect(() => lastInclusiveMoment('not-a-date')).not.toThrow()
    expect(Number.isNaN(lastInclusiveMoment('not-a-date').getTime())).toBe(true)
  })
})

describe('a malformed event.tz degrades one row, never the page', () => {
  // ``Intl`` throws ``RangeError: Invalid time zone specified`` on an
  // unknown zone. A peer (or a bad ICS import) can put anything in
  // ``event.tz``, and one such row used to take out the whole agenda
  // via the App-level ErrorBoundary. Every tz-consuming helper falls
  // back to UTC instead.
  const bad = () => spanEvt(
    'bad-tz', '2026-05-01T00:00:00Z', '2026-05-03T23:59:00Z',
    { all_day: true, tz: 'Foo/Bar' },
  )

  it('still buckets an all-day row carrying an unknown zone', () => {
    const groups = groupEventsByDay([bad()])
    expect(Object.keys(groups).sort()).toEqual([
      '2026-05-01', '2026-05-02', '2026-05-03',
    ])
    expect(groups['2026-05-02'][0].dayCount).toBe(3)
  })

  it('still labels the day portion of a row with an unknown zone', () => {
    const row = groupEventsByDay([bad()])['2026-05-02'][0]
    const out = formatDayPortion(row)
    expect(out.badge).toBe('Day 2 of 3')
    expect(out.when).toContain('all day')
    expect(out.when).not.toContain('Invalid')
  })

  it('still renders the expanded bounds of a row with an unknown zone', () => {
    const out = formatEventBounds(bad())
    expect(out.starts).toContain('1')
    expect(out.ends).toContain('3')
    expect(out.starts).not.toContain('Invalid')
    expect(out.ends).not.toContain('Invalid')
  })

  it('still buckets a TIMED row carrying an unknown zone', () => {
    // Timed rows bucket in the viewer's zone, so ``tz`` is only a
    // hazard if a helper reads it — this pins that it doesn't.
    const timed = spanEvt(
      'bad-tz-timed', '2026-05-01T09:00:00', '2026-05-02T09:00:00',
      { tz: 'Foo/Bar' },
    )
    const groups = groupEventsByDay([timed])
    expect(Object.keys(groups).sort()).toEqual(['2026-05-01', '2026-05-02'])
    expect(formatDayPortion(groups['2026-05-01'][0]).badge).toBe('Starts')
  })
})
