import { describe, it, expect } from 'vitest'
import { groupEventsByDay, formatMonthHeading, monthRange } from './calendar'
import type { CalendarEvent } from '@/types'

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

describe('calendar utils', () => {
  it('groups events by their local-date key, preserving order within a day', () => {
    const a = evt('a', '2026-04-30T08:00:00')
    const b = evt('b', '2026-04-30T15:00:00')
    const c = evt('c', '2026-05-01T09:00:00')
    const groups = groupEventsByDay([a, b, c])
    const keys = Object.keys(groups)
    expect(keys.length).toBe(2)
    expect(groups[keys[0]].map(e => e.id)).toEqual(['a', 'b'])
    expect(groups[keys[1]].map(e => e.id)).toEqual(['c'])
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
