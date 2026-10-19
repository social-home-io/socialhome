import { describe, it, expect, afterEach } from 'vitest'
import { currentUser } from '@/store/auth'
import { setLocale } from '@/i18n/i18n'
import {
  eventDaySpan,
  formatRangeHeading,
  formatEventBounds,
  formatMonthHeading,
  groupSharedEvents,
  lastInclusiveMoment,
  monthRange,
  formatDayKey,
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
  it('day keys are locale-independent YYYY-MM-DD that sort chronologically', () => {
    // Regression for the agenda landing out of order on non-en-*
    // locales: the old key was ``toLocaleDateString()`` and could not be
    // round-tripped (``14.5.2026`` → NaN). Day keys sort as strings.
    const keys = ['2026-05-21', '2026-05-14', '2026-05-19']
      .map(d => eventDaySpan(evt(d, `${d}T10:00:00`))!.startKey)
    expect([...keys].sort()).toEqual(['2026-05-14', '2026-05-19', '2026-05-21'])
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

describe('formatDayKey', () => {
  it.each(['America/Los_Angeles', 'UTC', 'Pacific/Kiritimati'])(
    'renders the key\'s own day and month in %s',
    (tz) => {
      const prevTz = process.env.TZ
      process.env.TZ = tz
      try {
        expect(formatDayKey('2026-05-01', {
          weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
        })).toBe('Friday, May 1, 2026')
        expect(formatDayKey('2026-05-01', { month: 'long', year: 'numeric' }))
          .toBe('May 2026')
      } finally {
        process.env.TZ = prevTz
      }
    },
  )

  it('ignores a caller-supplied timeZone', () => {
    expect(formatDayKey('2026-01-01', { year: 'numeric', timeZone: 'Pacific/Pago_Pago' }))
      .toBe('2026')
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

const span = (e: CalendarEvent) => eventDaySpan(e)

describe('eventDaySpan — the day rules every calendar view shares', () => {
  it('a timed 3-day span covers its three days', () => {
    // Local wall-clock strings (no ``Z``): timed rows are read in the
    // viewer's own zone, so a UTC instant would make the test zone-bound.
    expect(span(spanEvt('trip', '2026-05-01T14:00:00', '2026-05-03T10:00:00')))
      .toEqual({ startKey: '2026-05-01', endKey: '2026-05-03', dayCount: 3 })
  })

  it('anchors an all-day span in UTC when the row has no tz', () => {
    expect(span(spanEvt('h', '2026-05-01T00:00:00Z', '2026-05-03T23:59:00Z', { all_day: true })))
      .toEqual({ startKey: '2026-05-01', endKey: '2026-05-03', dayCount: 3 })
  })

  it('treats an exact-midnight end as exclusive (22:00 → 00:00 = one day)', () => {
    expect(span(spanEvt('late', '2026-05-01T22:00:00', '2026-05-02T00:00:00'))!.dayCount).toBe(1)
  })

  it('a same-day event is one day', () => {
    expect(span(spanEvt('lunch', '2026-05-14T11:00:00', '2026-05-14T12:00:00')))
      .toEqual({ startKey: '2026-05-14', endKey: '2026-05-14', dayCount: 1 })
  })

  it('counts calendar days across a spring-forward weekend', () => {
    // EU spring-forward night (29 March 2026): key arithmetic, never a
    // ``Date`` cursor, so the 23-hour day neither skips nor repeats.
    expect(span(spanEvt('w', '2026-03-28T20:00:00', '2026-03-30T09:00:00')))
      .toEqual({ startKey: '2026-03-28', endKey: '2026-03-30', dayCount: 3 })
  })

  it('falls back to the start day when end precedes start, is missing or unparseable', () => {
    expect(span(spanEvt('b', '2026-05-10T10:00:00', '2026-05-08T10:00:00'))!.dayCount).toBe(1)
    expect(span(spanEvt('n', '2026-05-10T10:00:00', undefined))!.dayCount).toBe(1)
    expect(span(spanEvt('j', '2026-05-11T10:00:00', 'not-a-date'))!.endKey).toBe('2026-05-11')
  })

  it('has no span for an unparseable start', () => {
    expect(span(spanEvt('junk', 'not-a-date', '2026-05-02T10:00:00'))).toBeNull()
  })
})

describe('eventDaySpan — all-day events anchor on the event tz', () => {
  it('keeps a Zurich single-day all-day event on its one day', () => {
    // Stored shape (``CalendarEventDialog.tsx`` writes 00:00 / 23:59 in
    // the event's own tz): "1 May 2026" in Europe/Zurich is on the wire
    // as 2026-04-30T22:00:00Z → 2026-05-01T21:59:00Z.
    expect(span(spanEvt('m', '2026-04-30T22:00:00Z', '2026-05-01T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' })))
      .toEqual({ startKey: '2026-05-01', endKey: '2026-05-01', dayCount: 1 })
  })

  it('a Zurich all-day 3-day span covers its own three days', () => {
    expect(span(spanEvt('t', '2026-04-30T22:00:00Z', '2026-05-03T21:59:00Z',
      { all_day: true, tz: 'Europe/Zurich' }))!.dayCount).toBe(3)
  })

  it('anchors a west-of-UTC all-day event on its own zone', () => {
    expect(span(spanEvt('la', '2026-05-01T07:00:00Z', '2026-05-02T06:59:00Z',
      { all_day: true, tz: 'America/Los_Angeles' })))
      .toEqual({ startKey: '2026-05-01', endKey: '2026-05-01', dayCount: 1 })
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

  it('still places an all-day row carrying an unknown zone (as UTC)', () => {
    expect(span(bad())).toEqual({ startKey: '2026-05-01', endKey: '2026-05-03', dayCount: 3 })
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
    expect(span(timed)).toEqual({ startKey: '2026-05-01', endKey: '2026-05-02', dayCount: 2 })
  })
})

describe('formatRangeHeading — week start', () => {
  const setWeekPref = (week_start: string | null) => {
    ;(currentUser as { value: unknown }).value = week_start === null
      ? null
      : { user_id: 'u', preferences_json: JSON.stringify({ week_start }) }
  }
  afterEach(() => setWeekPref(null))

  // Sun 4 Oct 2026, mid-afternoon local time.
  const sunday = new Date(2026, 9, 4, 15, 30)

  it('formatRangeHeading follows the week start', () => {
    const fmt = (d: Date, withYear = false) => d.toLocaleDateString(undefined, withYear
      ? { month: 'short', day: 'numeric', year: 'numeric' }
      : { month: 'short', day: 'numeric' })
    expect(formatRangeHeading(sunday, 'week', 0))
      .toBe(`${fmt(new Date(2026, 8, 28))} – ${fmt(new Date(2026, 9, 4), true)}`)
    expect(formatRangeHeading(sunday, 'week', 6))
      .toBe(`${fmt(new Date(2026, 9, 4))} – ${fmt(new Date(2026, 9, 10), true)}`)
  })

  it('formatRangeHeading default reads the preference per call', () => {
    setWeekPref('sun')
    const sun = formatRangeHeading(sunday, 'week')
    setWeekPref('mon')
    const mon = formatRangeHeading(sunday, 'week')
    expect(sun).toBe(formatRangeHeading(sunday, 'week', 6))
    expect(mon).toBe(formatRangeHeading(sunday, 'week', 0))
    expect(sun).not.toBe(mon)
  })
})

describe('calendar headings in the UI language', () => {
  it('formats the month and day headings in German', async () => {
    await setLocale('de')
    try {
      const d = new Date(2026, 9, 4, 12, 0, 0)
      expect(formatRangeHeading(d, 'month')).toBe('Oktober 2026')
      expect(formatRangeHeading(d, 'day')).toBe('Sonntag, 4. Oktober 2026')
    } finally {
      await setLocale('en')
    }
  })
})
