/**
 * CalendarUpcoming: week groups, each event once (a six-day holiday is
 * one row, not six cards), the empty state and the end line.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/preact'
import type { CalendarEvent } from '@/types'
import { CalendarUpcoming } from './CalendarUpcoming'

afterEach(cleanup)

const NOW = new Date(2026, 9, 19, 9, 0)
const allDay = (id: string, from: string, to = from): CalendarEvent => ({
  id, calendar_id: 'c', summary: id, description: null,
  start: `${from}T00:00:00Z`, end: `${to}T23:59:00Z`, all_day: true, tz: 'UTC', created_by: 'u',
})

function mount(events: CalendarEvent[]) {
  return render(
    <CalendarUpcoming
      events={events} now={NOW} weekStart={0}
      renderDetail={(e) => <p>detail of {e.summary}</p>}
      empty={<p>nothing</p>}
      footer={<span>end line</span>}
    />,
  )
}

describe('CalendarUpcoming', () => {
  it('groups by week with "This week" / "Next week" headings and the dates', () => {
    const r = mount([allDay('Swim', '2026-10-21'), allDay('Autumn holidays', '2026-10-26', '2026-10-31')])
    const headings = r.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headings[0]).toMatch(/^This week\s*Oct 19\s*–\s*25$/)
    expect(headings[1]).toMatch(/^Next week\s*Oct 26\s*–\s*Nov 1$/)
  })

  it('a multi-day event is ONE row with its span', () => {
    const r = mount([allDay('Autumn holidays', '2026-10-26', '2026-10-31')])
    const rows = r.getAllByRole('button', { name: /Autumn holidays/ })
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('Mon 26 – Sat 31 6 days')
  })

  it('opens one row at a time', () => {
    const r = mount([allDay('Alpha', '2026-10-21'), allDay('Bravo', '2026-10-22')])
    fireEvent.click(r.getByRole('button', { name: /Alpha/ }))
    expect(r.getByText('detail of Alpha')).toBeTruthy()
    fireEvent.click(r.getByRole('button', { name: /Bravo/ }))
    expect(r.queryByText('detail of Alpha')).toBeNull()
    expect(r.getByText('detail of Bravo')).toBeTruthy()
  })

  it('shows the empty state when nothing is coming up, and the end line', () => {
    const r = mount([allDay('Past', '2026-10-01')])
    expect(r.getByText('nothing')).toBeTruthy()
    expect(r.getByText('end line')).toBeTruthy()
  })
})
