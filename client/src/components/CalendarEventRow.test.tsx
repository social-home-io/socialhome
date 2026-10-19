/**
 * CalendarEventRow: one anatomy for every event — a multi-day event is
 * one row with its span and day count in the date column.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/preact'
import type { CalendarEvent } from '@/types'
import { spanEntries } from '@/utils/calendarViews'
import { CalendarEventRow, rowMeta, rowWhen } from './CalendarEventRow'

afterEach(cleanup)

const ev = (over: Partial<CalendarEvent>): CalendarEvent => ({
  id: 'e', calendar_id: 'c', summary: 'Event', description: null,
  start: '2026-10-26T00:00:00Z', end: '2026-10-26T23:59:00Z', all_day: true, tz: 'UTC',
  created_by: 'u', ...over,
})
const entry = (over: Partial<CalendarEvent>) => spanEntries([ev(over)])[0]

describe('rowWhen', () => {
  it('multi-day: "Mon 26 –" / "Sat 31" / "6 days"', () => {
    expect(rowWhen(entry({ end: '2026-10-31T23:59:00Z' }))).toEqual(['Mon 26 –', 'Sat 31', '6 days'])
  })

  it('single all-day: the day over "All day"', () => {
    expect(rowWhen(entry({ start: '2026-10-29T00:00:00Z', end: '2026-10-29T23:59:00Z' })))
      .toEqual(['Thu 29', 'All day'])
  })

  it('timed: the day over the start time', () => {
    const start = new Date(2026, 9, 19, 8, 30)
    const [day, time] = rowWhen(entry({
      all_day: false, start: start.toISOString(), end: new Date(2026, 9, 19, 9, 0).toISOString(),
    }))
    expect(day).toBe('Mon 19')
    expect(time).toMatch(/08:30|8:30/)
  })
})

describe('rowMeta', () => {
  it('timed: until when, then where', () => {
    const m = rowMeta(entry({
      all_day: false, location: 'Town hall',
      start: new Date(2026, 9, 22, 19, 0).toISOString(), end: new Date(2026, 9, 22, 21, 0).toISOString(),
    }))
    expect(m).toMatch(/^until (21:00|0?9:00 PM) · Town hall$/)
  })

  it('all-day: the place, else the first line of the description', () => {
    expect(rowMeta(entry({ location: 'Lake' }))).toBe('Lake')
    expect(rowMeta(entry({ description: 'Ruth turns 80\nCake at 3' }))).toBe('Ruth turns 80')
    expect(rowMeta(entry({}))).toBe('')
  })
})

describe('CalendarEventRow', () => {
  it('is a disclosure: the detail renders only while open', () => {
    const onToggle = vi.fn()
    const e = entry({ summary: 'Autumn holidays', end: '2026-10-31T23:59:00Z' })
    const r = render(
      <CalendarEventRow entry={e} open={false} onToggle={onToggle} detailId="d1">
        <p>detail</p>
      </CalendarEventRow>,
    )
    const head = r.getByRole('button', { name: /Autumn holidays/ })
    expect(head.getAttribute('aria-expanded')).toBe('false')
    expect(head.getAttribute('aria-controls')).toBe('d1')
    expect(head.textContent).toContain('6 days')
    expect(r.container.querySelector('.sh-cal-row--multi')).not.toBeNull()
    expect(r.queryByText('detail')).toBeNull()
    fireEvent.click(head)
    expect(onToggle).toHaveBeenCalled()
    r.rerender(
      <CalendarEventRow entry={e} open onToggle={onToggle} detailId="d1"><p>detail</p></CalendarEventRow>,
    )
    expect(r.getByText('detail').parentElement?.id).toBe('d1')
  })
})

describe('CalendarEventRow — decor', () => {
  it('paints the rail in a calendar hue and shows labels after the title', () => {
    const r = render(
      <CalendarEventRow entry={entry({ summary: 'Dinner' })} open={false} onToggle={() => {}} detailId="d2"
                        decor={{ hue: '#123456', badges: <span class="chip">You · Bob</span> }} />,
    )
    const row = r.container.querySelector<HTMLElement>('.sh-cal-row')!
    expect(row.classList.contains('sh-cal-row--hued')).toBe(true)
    expect(row.style.getPropertyValue('--sh-cal-hue')).toBe('#123456')
    expect(r.getByRole('button', { name: /Dinner/ }).textContent).toContain('You · Bob')
  })

  it('no decor: the kind colours the rail', () => {
    const r = render(
      <CalendarEventRow entry={entry({})} open={false} onToggle={() => {}} detailId="d3" />,
    )
    expect(r.container.querySelector('.sh-cal-row--hued')).toBeNull()
  })
})
