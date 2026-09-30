import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/api', () => ({ api: {}, ApiError: class extends Error {} }))
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { TimetablePrint } from './TimetablePrint'
import { entry, schoolWeek, timetable } from './testUtils'

const NOW = new Date(2026, 8, 30, 10, 0)
const plain = (s: string | null | undefined) => (s ?? '').replace(/\s/g, ' ')

describe('TimetablePrint', () => {
  it('prints a header with the name, the days and the validity, and a footer with the date', () => {
    const tt = timetable({
      entries: schoolWeek(),
      validity: { valid_from: '2026-09-07', valid_until: '2027-07-30', excluded_weeks: ['2026-10-26'] },
    })
    const { container } = render(<TimetablePrint tt={tt} days={[0, 1, 2, 3, 4]} now={NOW} />)
    const root = container.querySelector('.sh-timetable-print')!
    expect(root.getAttribute('aria-hidden')).toBe('true')
    expect(root.querySelector('h2')!.textContent).toBe("Emma's timetable")
    expect(plain(root.querySelector('.sh-timetable-print__meta')!.textContent))
      .toBe('Mon–Fri · Sep 7, 2026 – Jul 30, 2027 · 1 holiday week')
    expect(root.querySelector('table.sh-timetable-periods')).not.toBeNull()
    expect(root.querySelector('.sh-timetable-print__foot')!.textContent).toBe('Printed Sep 30, 2026')
  })

  it('says which week in week mode', () => {
    const tt = timetable({ entries: schoolWeek() })
    const { container } = render(
      <TimetablePrint tt={tt} days={[0, 1, 2, 3, 4]} week="W41 · Oct 5 – 11" now={NOW} />)
    expect(container.querySelector('.sh-timetable-print__meta')!.textContent)
      .toBe("Mon–Fri · W41 · Oct 5 – 11 · with this week's changes")
  })

  it('the list drops Room / Teacher columns a day has no values for, and sets lang for hyphenation', () => {
    const tt = timetable({ days: [0, 1], entries: [
      entry(0, '08:00', '08:45', { title: 'Mathe', room: '204' }),
      entry(0, '09:00', '09:45', { title: 'Deutsch', teacher: 'Fr. Huber' }),
      entry(1, '07:15', '08:00', { title: 'Musik' }),
    ] })
    const { container } = render(<TimetablePrint tt={tt} days={[0, 1]} now={NOW} />)
    expect(container.querySelector('.sh-timetable-print')!.getAttribute('lang')).toBe('en')
    const tables = Array.from(container.querySelectorAll('table.sh-timetable-list__day'))
    const heads = tables.map(tb => Array.from(tb.querySelectorAll('thead th')).map(th => th.textContent))
    expect(heads).toEqual([['Time', 'Lesson', 'Room', 'Teacher'], ['Time', 'Lesson']])
    // Body rows have as many cells as the header.
    expect(tables[1].querySelector('tbody tr')!.children).toHaveLength(2)
  })
})
