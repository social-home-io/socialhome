import { describe, it, expect, beforeEach } from 'vitest'
import { locale } from '@/i18n/i18n'
import { dueLabel, parseDueDate } from './dueLabel'

const NOW = new Date(2026, 9, 1, 15, 30) // 1 Oct 2026, local

beforeEach(() => { locale.value = 'en' })

describe('parseDueDate', () => {
  it('reads YYYY-MM-DD as a LOCAL date (no UTC shift to the day before)', () => {
    const d = parseDueDate('2026-10-03')!
    expect([d.getFullYear(), d.getMonth(), d.getDate()]).toEqual([2026, 9, 3])
  })
  it('accepts an ISO timestamp (its local calendar day) and rejects junk', () => {
    expect(parseDueDate('2026-10-03T00:00:00')!.getDate()).toBe(3)
    expect(parseDueDate('soon')).toBeNull()
  })
})

describe('dueLabel', () => {
  it('past due: one danger chip "Overdue · <date>"', () => {
    expect(dueLabel('2026-09-28', NOW)).toEqual({
      text: 'Overdue · Sep 28', short: 'Sep 28', tone: 'overdue', title: expect.any(String),
    })
  })
  it('due today: an amber "Today"', () => {
    expect(dueLabel('2026-10-01', NOW)).toMatchObject({ text: 'Today', tone: 'today' })
  })
  it('later: just the date, no tone; another year adds the year', () => {
    expect(dueLabel('2026-10-03', NOW)).toMatchObject({ text: 'Oct 3', tone: null })
    expect(dueLabel('2027-01-05', NOW).text).toBe('Jan 5, 2027')
  })
  it('follows the UI language', () => {
    locale.value = 'de'
    expect(dueLabel('2026-10-03', NOW).text).toBe('3. Okt.')
  })
  it('an unreadable value is shown as-is', () => {
    expect(dueLabel('someday', NOW)).toEqual({ text: 'someday', short: 'someday', tone: null, title: 'someday' })
  })
})
