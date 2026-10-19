/**
 * CalendarGrid (Month / Week): day cells are the controls (one Tab stop,
 * arrows move), a multi-day event is one spanning bar, "+N more" where
 * bars don't fit, and the picked day's events list under the grid.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/preact'
import type { CalendarEvent } from '@/types'
import { CalendarGrid } from './CalendarGrid'

afterEach(cleanup)

const NOW = new Date(2026, 9, 19, 9, 0)
const allDay = (id: string, from: string, to = from): CalendarEvent => ({
  id, calendar_id: 'c', summary: id, description: null,
  start: `${from}T00:00:00Z`, end: `${to}T23:59:00Z`, all_day: true, tz: 'UTC', created_by: 'u',
})

function mount(view: 'month' | 'week', events: CalendarEvent[], maxLanes?: number) {
  return render(
    <CalendarGrid view={view} cursor={NOW} now={NOW} weekStart={0} events={events}
                  maxLanes={maxLanes} renderDetail={(e) => <p>detail of {e.summary}</p>} />,
  )
}

const day = (r: ReturnType<typeof mount>, key: string) =>
  r.container.querySelector<HTMLButtonElement>(`[data-day="${key}"]`)!

describe('CalendarGrid — Month', () => {
  it('lays out whole weeks; today is current and picked, one Tab stop', () => {
    const r = mount('month', [])
    const cells = r.container.querySelectorAll('.sh-cal-grid__day')
    expect(cells).toHaveLength(35)
    const today = day(r, '2026-10-19')
    expect(today.getAttribute('aria-current')).toBe('date')
    expect(today.getAttribute('aria-pressed')).toBe('true')
    expect([...cells].filter((c) => c.getAttribute('tabindex') === '0')).toEqual([today])
    expect(day(r, '2026-09-28').classList.contains('is-outside')).toBe(true)
  })

  it('a multi-day event is one bar spanning its days, in each week row', () => {
    const r = mount('month', [allDay('Autumn holidays', '2026-10-26', '2026-10-31')])
    const bars = r.container.querySelectorAll<HTMLElement>('.sh-cal-bar')
    expect(bars).toHaveLength(1)
    expect(bars[0].style.gridColumn).toBe('1 / span 6')
    expect(bars[0].getAttribute('aria-hidden')).toBe('true')
    expect(bars[0].classList.contains('sh-cal-bar--multi')).toBe(true)
  })

  it('a span across a week boundary is one bar per row, open on the joined sides', () => {
    const r = mount('month', [allDay('Camp', '2026-10-23', '2026-10-27')])
    const bars = [...r.container.querySelectorAll<HTMLElement>('.sh-cal-bar')]
    expect(bars.map((b) => b.style.gridColumn)).toEqual(['5 / span 3', '1 / span 2'])
    expect(bars[0].classList.contains('is-cont-after')).toBe(true)
    expect(bars[1].classList.contains('is-cont-before')).toBe(true)
  })

  it('more events than lanes → "+N more" on the day; its name counts them all', () => {
    const r = mount('month', [
      allDay('a', '2026-10-21'), allDay('b', '2026-10-21'), allDay('c', '2026-10-21'),
    ], 2)
    const cell = day(r, '2026-10-21')
    expect(cell.textContent).toContain('+1 more')
    expect(cell.getAttribute('aria-label')).toMatch(/^Wednesday.*21.*, 3 events$/)
  })

  it('picking a day lists its events under the grid; arrows move the pick', () => {
    const r = mount('month', [allDay('Birthday', '2026-10-29')])
    expect(r.getByText('Nothing on this day.')).toBeTruthy()
    fireEvent.click(day(r, '2026-10-29'))
    fireEvent.click(r.getByRole('button', { name: /Birthday/ }))
    expect(r.getByText('detail of Birthday')).toBeTruthy()
    fireEvent.keyDown(day(r, '2026-10-29'), { key: 'ArrowRight' })
    expect(day(r, '2026-10-30').getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement).toBe(day(r, '2026-10-30'))
    fireEvent.keyDown(day(r, '2026-10-30'), { key: 'ArrowUp' })
    expect(day(r, '2026-10-23').getAttribute('aria-pressed')).toBe('true')
    fireEvent.keyDown(day(r, '2026-10-23'), { key: 'Home' })
    expect(day(r, '2026-10-19').getAttribute('aria-pressed')).toBe('true')
  })
})

describe('CalendarGrid — Week', () => {
  it('one row of seven days, every bar shown', () => {
    const r = mount('week', [
      allDay('a', '2026-10-21'), allDay('b', '2026-10-21'), allDay('c', '2026-10-21'), allDay('d', '2026-10-21'),
    ])
    expect(r.container.querySelectorAll('.sh-cal-grid__day')).toHaveLength(7)
    expect(r.container.querySelectorAll('.sh-cal-bar')).toHaveLength(4)
    expect(r.container.querySelector('.sh-cal-grid__more')).toBeNull()
  })
})

describe('CalendarGrid — decor', () => {
  it('a bar and its row take the decorated hue', () => {
    const r = render(
      <CalendarGrid view="month" cursor={NOW} now={NOW} weekStart={0}
                    events={[allDay('Swim', '2026-10-19')]}
                    decorate={() => ({ hue: '#abcdef' })}
                    renderDetail={() => null} />,
    )
    const bar = r.container.querySelector<HTMLElement>('.sh-cal-bar')!
    expect(bar.classList.contains('sh-cal-bar--hued')).toBe(true)
    expect(bar.style.getPropertyValue('--sh-cal-hue')).toBe('#abcdef')
    expect(r.container.querySelector('.sh-cal-day .sh-cal-row--hued')).not.toBeNull()
  })
})
