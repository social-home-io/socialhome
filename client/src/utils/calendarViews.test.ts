/**
 * Calendar view layout: Upcoming groups each event ONCE under the week
 * it is next on, the grids cover the right days, and a multi-day event
 * is one bar per week row (clipped, flagged where it runs on).
 */
import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import {
  UPCOMING_WEEKS, entriesOnDay, gridRange, gridWeeks, groupUpcomingByWeek,
  layoutWeekBars, shortDay, spanDays, spanEntries, upcomingRange, upcomingWeekTitle,
  viewOnScreen, weekRangeLabel,
} from './calendarViews'

/** A timed event on local wall-clock times. */
function timed(id: string, y: number, mo: number, d: number, h: number, hours = 1): CalendarEvent {
  const start = new Date(y, mo - 1, d, h, 0)
  const end = new Date(start.getTime() + hours * 3600_000)
  return {
    id, calendar_id: 'c', summary: id, description: null,
    start: start.toISOString(), end: end.toISOString(), all_day: false, created_by: 'u',
  }
}

/** An all-day event over whole days (inclusive), stored the composer's
 *  way: 00:00 → 23:59 in its own tz (UTC here). */
function allDay(id: string, from: string, to = from): CalendarEvent {
  return {
    id, calendar_id: 'c', summary: id, description: null,
    start: `${from}T00:00:00Z`, end: `${to}T23:59:00Z`, all_day: true, tz: 'UTC', created_by: 'u',
  }
}

// Monday 19 October 2026, mid-morning.
const NOW = new Date(2026, 9, 19, 9, 0)

afterEach(async () => { await setLocale('en') })

describe('spanEntries', () => {
  it('a six-day holiday is one entry covering six days', () => {
    const [e] = spanEntries([allDay('hols', '2026-10-26', '2026-10-31')])
    expect(e).toMatchObject({ startKey: '2026-10-26', endKey: '2026-10-31', dayCount: 6 })
  })

  it('orders by first day, longer spans first, all-day before timed', () => {
    const rows = spanEntries([
      timed('t', 2026, 10, 26, 9),
      allDay('one', '2026-10-26'),
      allDay('long', '2026-10-26', '2026-10-28'),
      timed('early', 2026, 10, 20, 9),
    ])
    expect(rows.map((r) => r.event.id)).toEqual(['early', 'long', 'one', 't'])
  })
})

describe('groupUpcomingByWeek (Monday weeks)', () => {
  const weeks = (evts: CalendarEvent[]) => groupUpcomingByWeek(evts, NOW, 0)

  it('files each event once under its week: this week, next week, later', () => {
    const w = weeks([
      timed('dentist', 2026, 10, 19, 8),
      allDay('hols', '2026-10-26', '2026-10-31'),
      allDay('bday', '2026-10-29'),
      timed('later', 2026, 11, 4, 18),
    ])
    expect(w.map((x) => [x.offset, x.startKey, x.endKey, x.entries.map((e) => e.event.id)])).toEqual([
      [0, '2026-10-19', '2026-10-25', ['dentist']],
      [1, '2026-10-26', '2026-11-01', ['hols', 'bday']],
      [2, '2026-11-02', '2026-11-08', ['later']],
    ])
  })

  it('an event already running sits in this week', () => {
    const w = weeks([allDay('camp', '2026-10-15', '2026-10-21')])
    expect(w).toHaveLength(1)
    expect(w[0].offset).toBe(0)
    expect(w[0].entries[0].dayCount).toBe(7)
  })

  it(`leaves out what ended before today and what starts after ${UPCOMING_WEEKS} weeks`, () => {
    const w = weeks([
      allDay('past', '2026-10-12', '2026-10-18'),
      allDay('last-day', '2026-11-15'),
      allDay('too-far', '2026-11-16'),
    ])
    expect(w.flatMap((x) => x.entries.map((e) => e.event.id))).toEqual(['last-day'])
  })

  it('a Sunday week start moves the boundaries', () => {
    const w = groupUpcomingByWeek([allDay('sun', '2026-10-25')], NOW, 6)
    expect(w[0]).toMatchObject({ offset: 1, startKey: '2026-10-25', endKey: '2026-10-31' })
  })

  it('titles: This week, Next week, then none (the dates alone)', () => {
    const w = weeks([
      timed('a', 2026, 10, 20, 9), timed('b', 2026, 10, 27, 9), timed('c', 2026, 11, 3, 9),
    ])
    expect(w.map(upcomingWeekTitle)).toEqual(['This week', 'Next week', null])
  })
})

describe('upcomingRange', () => {
  it(`runs from today's midnight through ${UPCOMING_WEEKS} weeks`, () => {
    const { start, end } = upcomingRange(NOW)
    expect(new Date(start)).toEqual(new Date(2026, 9, 19))
    expect(new Date(end)).toEqual(new Date(2026, 10, 15, 23, 59, 59))
  })
})

describe('gridWeeks / gridRange', () => {
  it('Month covers whole weeks around the month (Monday start)', () => {
    const rows = gridWeeks(NOW, 'month', 0)
    expect(rows).toHaveLength(5)
    expect(rows[0][0]).toBe('2026-09-28')
    expect(rows[4][6]).toBe('2026-11-01')
    expect(rows.every((r) => r.length === 7)).toBe(true)
  })

  it('Month with a Sunday start', () => {
    const rows = gridWeeks(NOW, 'month', 6)
    expect(rows[0][0]).toBe('2026-09-27')
    expect(rows[rows.length - 1][6]).toBe('2026-10-31')
  })

  it('Week is the one week holding the cursor', () => {
    expect(gridWeeks(NOW, 'week', 0)).toEqual([[
      '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24', '2026-10-25',
    ]])
  })

  it('the fetch range spans the first to the last grid day', () => {
    const { start, end } = gridRange(gridWeeks(NOW, 'month', 0))
    expect(new Date(start)).toEqual(new Date(2026, 8, 28))
    expect(new Date(end)).toEqual(new Date(2026, 10, 1, 23, 59, 59))
  })

  it('Week follows the week start: a Sunday is the last day (Monday) or the first (Sunday)', () => {
    const sunday = new Date(2026, 9, 4, 15, 30)
    expect(gridWeeks(sunday, 'week', 0)[0][0]).toBe('2026-09-28')
    expect(gridWeeks(sunday, 'week', 6)[0][0]).toBe('2026-10-04')
    // A Saturday closes the Sunday-start week.
    expect(gridWeeks(new Date(2026, 9, 3, 9), 'week', 6)[0]).toContain('2026-09-27')
  })

  it('a week spanning a DST change still ends at local 23:59:59', () => {
    // US DST ends Sun 1 Nov 2026, inside the Mon 26 Oct – Sun 1 Nov week.
    const prevTz = process.env.TZ
    process.env.TZ = 'America/New_York'
    try {
      expect(new Date(2026, 9, 26).getTimezoneOffset())
        .not.toBe(new Date(2026, 10, 1, 12).getTimezoneOffset())
      const r = gridRange(gridWeeks(new Date(2026, 9, 27, 12), 'week', 0))
      expect(r.start).toBe(new Date(2026, 9, 26, 0, 0, 0).toISOString())
      expect(r.end).toBe(new Date(2026, 10, 1, 23, 59, 59).toISOString())
    } finally {
      process.env.TZ = prevTz
    }
  })
})

describe('viewOnScreen', () => {
  it('Week falls back to Month on a narrow screen; the rest stay', () => {
    expect(viewOnScreen('week', false)).toBe('month')
    expect(viewOnScreen('week', true)).toBe('week')
    expect(viewOnScreen('upcoming', false)).toBe('upcoming')
  })
})

describe('layoutWeekBars', () => {
  const week = gridWeeks(NOW, 'week', 0)[0]

  it('a multi-day event is ONE bar across its days', () => {
    const bars = layoutWeekBars(week, spanEntries([allDay('trip', '2026-10-20', '2026-10-23')]))
    expect(bars).toHaveLength(1)
    expect(bars[0]).toMatchObject({ col: 1, span: 4, lane: 0, continuesBefore: false, continuesAfter: false })
  })

  it('clips to the week and says where it runs on', () => {
    const bars = layoutWeekBars(week, spanEntries([allDay('hols', '2026-10-15', '2026-10-28')]))
    expect(bars[0]).toMatchObject({ col: 0, span: 7, continuesBefore: true, continuesAfter: true })
  })

  it('stacks overlapping events into lanes, reusing a free one', () => {
    const bars = layoutWeekBars(week, spanEntries([
      allDay('long', '2026-10-19', '2026-10-21'),
      timed('mon', 2026, 10, 19, 9),
      timed('thu', 2026, 10, 22, 9),
    ]))
    const lane = Object.fromEntries(bars.map((b) => [b.entry.event.id, b.lane]))
    expect(lane).toEqual({ long: 0, mon: 1, thu: 0 })
  })

  it('skips events outside the week', () => {
    expect(layoutWeekBars(week, spanEntries([allDay('x', '2026-11-02')]))).toEqual([])
  })
})

describe('entriesOnDay', () => {
  it('includes the days inside a span', () => {
    const entries = spanEntries([allDay('hols', '2026-10-26', '2026-10-31'), allDay('b', '2026-10-29')])
    expect(entriesOnDay(entries, '2026-10-29').map((e) => e.event.id)).toEqual(['hols', 'b'])
    expect(entriesOnDay(entries, '2026-11-01')).toEqual([])
  })
})

describe('labels', () => {
  it('shortDay / spanDays — months named only when the span crosses one', () => {
    expect(shortDay('2026-10-26')).toBe('Mon 26')
    expect(spanDays('2026-10-26', '2026-10-31')).toBe('Mon 26 – Sat 31')
    expect(spanDays('2026-10-30', '2026-11-02')).toBe('Fri 30 Oct – Mon 2 Nov')
  })

  it('weekRangeLabel collapses the shared month', () => {
    expect(weekRangeLabel('2026-10-19', '2026-10-25')).toMatch(/Oct 19\s*–\s*25/)
  })

  it('German', async () => {
    await setLocale('de')
    expect(shortDay('2026-10-26')).toBe('Mo 26')
  })
})
