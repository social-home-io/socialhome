import { describe, it, expect } from 'vitest'
import {
  toMinutes, fromMinutes, formatRange, snapTo5, orderedDays,
  weekdayName, daysSummary, ordinal, nextDayFrom,
} from './time'

describe('timetable time helpers', () => {
  it('converts HH:MM to minutes and back', () => {
    expect(toMinutes('00:00')).toBe(0)
    expect(toMinutes('08:05')).toBe(485)
    expect(toMinutes('23:59')).toBe(1439)
    expect(fromMinutes(485)).toBe('08:05')
    expect(fromMinutes(0)).toBe('00:00')
  })

  it('clamps out-of-day minutes to 00:00–23:59', () => {
    expect(fromMinutes(-10)).toBe('00:00')
    expect(fromMinutes(1500)).toBe('23:59')
  })

  it('formats a time range with an en dash', () => {
    expect(formatRange('08:00', '08:45')).toBe('08:00–08:45')
  })

  it('snaps minutes to the nearest 5', () => {
    expect(snapTo5(482)).toBe(480)
    expect(snapTo5(483)).toBe(485)
    expect(snapTo5(480)).toBe(480)
  })

  it('orders days by the week start', () => {
    expect(orderedDays([4, 0, 2], 0)).toEqual([0, 2, 4])
    expect(orderedDays([0, 1, 2, 3, 4], 6)).toEqual([0, 1, 2, 3, 4])
    expect(orderedDays([6, 0, 1], 6)).toEqual([6, 0, 1])
    expect(orderedDays([6, 0, 1], 0)).toEqual([0, 1, 6])
  })

  it('names weekdays in the UI locale', () => {
    expect(weekdayName(0, 'short')).toBe('Mon')
    expect(weekdayName(6, 'long')).toBe('Sunday')
    expect(weekdayName(2, 'short', 'de')).toBe('Mi')
  })

  it('caches formatters per (lang, style) without mixing them up', () => {
    expect(weekdayName(0, 'long', 'de')).toBe('Montag')
    expect(weekdayName(0, 'long', 'en')).toBe('Monday')
    expect(weekdayName(0, 'short', 'de')).toBe('Mo')
    expect(weekdayName(0, 'long', 'de')).toBe('Montag')
    expect(weekdayName(3, 'long', 'xx-invalid-!!')).toBe('Thursday')
  })

  it('summarises days as ranges where contiguous, in week-start order', () => {
    expect(daysSummary([0, 1, 2, 3, 4], 0)).toBe('Mon–Fri')
    expect(daysSummary([0, 1, 2, 3, 4, 5], 0)).toBe('Mon–Sat')
    expect(daysSummary([0, 2, 4], 0)).toBe('Mon, Wed, Fri')
    expect(daysSummary([5, 6], 0)).toBe('Sat, Sun')
    expect(daysSummary([6, 0, 1, 2], 6)).toBe('Sun–Wed')
    // Monday-start order splits Sun off the end of the run.
    expect(daysSummary([6, 0, 1, 2], 0)).toBe('Mon–Wed, Sun')
    expect(daysSummary([0, 1, 2, 3, 4, 5, 6], 0)).toBe('Every day')
  })

  it('builds English ordinals', () => {
    expect(ordinal(1)).toBe('1st')
    expect(ordinal(2)).toBe('2nd')
    expect(ordinal(3)).toBe('3rd')
    expect(ordinal(4)).toBe('4th')
    expect(ordinal(11)).toBe('11th')
    expect(ordinal(22)).toBe('22nd')
  })

  it('finds the next day in a set, wrapping around the week', () => {
    expect(nextDayFrom(2, [0, 1, 2, 3, 4])).toBe(2)
    expect(nextDayFrom(5, [0, 1, 2, 3, 4])).toBe(0)
    expect(nextDayFrom(6, [1, 3])).toBe(1)
    expect(nextDayFrom(2, [1, 3])).toBe(3)
  })
})
