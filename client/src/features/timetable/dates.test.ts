import { describe, it, expect } from 'vitest'
import {
  addDays, dateRange, daysBetween, editableFrom, isIsoDate, isoDate, overridesInWeek,
  parseIsoDate, todayIn, weekAnchor, weekdayOf, weekLabel,
} from './dates'
import { timetable } from './testUtils'
import type { TimetableOverride } from '@/types'

describe('timetable dates', () => {
  it('round-trips local calendar days', () => {
    expect(isoDate(new Date(2026, 9, 5, 23, 59))).toBe('2026-10-05')
    expect(isoDate(parseIsoDate('2026-03-29'))).toBe('2026-03-29')
    expect(isIsoDate('2026-02-30')).toBe(false)
    expect(isIsoDate('2026-02-28')).toBe(true)
    expect(isIsoDate('')).toBe(false)
  })

  it('adds days across month, year and DST boundaries', () => {
    expect(addDays('2026-03-28', 2)).toBe('2026-03-30')
    expect(addDays('2026-12-30', 3)).toBe('2027-01-02')
    expect(addDays('2026-10-05', -7)).toBe('2026-09-28')
    expect(daysBetween('2026-03-28', '2026-04-04')).toBe(7)
  })

  it('anchors a week on its Monday or Sunday', () => {
    expect(weekAnchor('2026-10-08', 0)).toBe('2026-10-05')
    expect(weekAnchor('2026-10-08', 6)).toBe('2026-10-04')
    expect(weekAnchor('2026-10-04', 0)).toBe('2026-09-28')
    expect(weekdayOf('2026-10-05')).toBe(0)
    expect(weekdayOf('2026-10-04')).toBe(6)
  })

  it('reads today in the timetable zone', () => {
    const late = new Date(Date.UTC(2026, 9, 5, 23, 30))
    expect(todayIn('UTC', late)).toBe('2026-10-05')
    expect(todayIn('Europe/Berlin', late)).toBe('2026-10-06')
    expect(todayIn('Not/AZone', late)).toBe(isoDate(late))
    expect(editableFrom({ tz: 'UTC' }, late)).toBe('2026-09-21')
  })

  it('labels a week by ISO number for Monday starts, by dates for Sunday starts', () => {
    // Intl pads the range dash with thin spaces.
    const plain = (s: string) => s.replace(/\s/g, ' ')
    expect(plain(weekLabel('2026-10-05', 0))).toBe('W41 · Oct 5 – 11')
    expect(plain(weekLabel('2026-10-04', 6))).toBe('Oct 4 – 10')
    expect(plain(dateRange('2026-09-28', '2026-10-04'))).toBe('Sep 28 – Oct 4')
  })

  it('picks the overrides of one week', () => {
    const ov = (date: string) => ({ id: date, date, kind: 'cancel' }) as TimetableOverride
    const tt = timetable({ overrides: [ov('2026-10-04'), ov('2026-10-05'), ov('2026-10-11'), ov('2026-10-12')] })
    expect(overridesInWeek(tt, '2026-10-05').map(o => o.id)).toEqual(['2026-10-05', '2026-10-11'])
  })
})
