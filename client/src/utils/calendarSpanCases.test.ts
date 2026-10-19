/**
 * Every calendar in the app places and spells an event by the same rules:
 * the SPA views (here) and the household wall dashboard
 * (``tests/domain/test_calendar_span_cases.py``) are tested against ONE
 * set of cases, ``tests/fixtures/calendar_span_cases.json``. The vitest
 * run is in UTC, the viewer zone every case assumes.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { t } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import { eventDaySpan } from './calendar'
import { spanDays } from './calendarViews'

interface SpanCase {
  name: string
  start: string
  end: string
  all_day: boolean
  tz: string | null
  first: string
  last: string
  day_count: number
  label_en: string | null
}

// jsdom gives import.meta.url an http: scheme; vitest still injects __dirname.
const { cases } = JSON.parse(readFileSync(
  join(__dirname, '..', '..', '..', 'tests', 'fixtures', 'calendar_span_cases.json'), 'utf8',
)) as { cases: SpanCase[] }

describe('calendar span cases shared with the wall dashboard', () => {
  it('runs in UTC (the viewer zone of every case)', () => {
    expect(new Date('2026-05-01T00:00:00Z').getTimezoneOffset()).toBe(0)
  })

  for (const c of cases) {
    it(c.name, () => {
      const event = {
        id: 'e', calendar_id: 'c', summary: c.name, description: null,
        start: c.start, end: c.end, all_day: c.all_day, tz: c.tz ?? undefined, created_by: 'u',
      } as unknown as CalendarEvent
      const span = eventDaySpan(event)!
      expect(span).toEqual({ startKey: c.first, endKey: c.last, dayCount: c.day_count })
      const label = span.dayCount > 1
        ? `${spanDays(span.startKey, span.endKey)} · ${t('calendar.span_days', { count: String(span.dayCount) })}`
        : null
      expect(label).toBe(c.label_en)
    })
  }
})
