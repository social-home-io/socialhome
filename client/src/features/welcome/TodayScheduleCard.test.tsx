import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render } from '@testing-library/preact'
import type { TodayLesson, TodayTimetable, WelcomeEvent } from './cards'
import { FILTER_KEY, TodayScheduleCard } from './TodayScheduleCard'
import { buildAgenda, clock, clockRange, collapsePast, scheduleStatus } from './schedule'

// A fixed local day so every time below is a wall-clock time in the
// test runner's zone (the card formats in the viewer's zone too).
const DAY = [2026, 8, 28] as const // Mon 28 Sep 2026
const at = (h: number, m = 0) => new Date(DAY[0], DAY[1], DAY[2], h, m)
const iso = (h: number, m = 0) => at(h, m).toISOString()
const hm = (h: number, m = 0) => `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`

function lesson(
  id: string, [sh, sm]: [number, number], [eh, em]: [number, number],
  over: Partial<TodayLesson> = {},
): TodayLesson {
  return {
    source_id: id, date: '2026-09-28', start: hm(sh, sm), end: hm(eh, em),
    kind: 'lesson', label: null, title: id, room: null, teacher: null, note: null,
    color: null, icon: null, status: 'normal', override_id: null, original: null,
    start_at: iso(sh, sm), end_at: iso(eh, em),
    ...over,
  }
}

function timetable(id: string, name: string, lessons: TodayLesson[]): TodayTimetable {
  return { timetable_id: id, name, color: 'sky', tz: 'UTC', date: '2026-09-28', lessons }
}

function event(id: string, [sh, sm]: [number, number], [eh, em]: [number, number],
  over: Partial<WelcomeEvent> = {}): WelcomeEvent {
  return { id, summary: id, start: iso(sh, sm), end: iso(eh, em), all_day: false, ...over }
}

const EMMA = timetable('tt-emma', 'Emma', [
  lesson('Mathe', [8, 0], [8, 45], { label: '1.', room: '204', icon: '🔢' }),
  lesson('Deutsch', [8, 50], [9, 35], { label: '2.' }),
  lesson('Pause', [9, 35], [9, 55], { kind: 'break' }),
  lesson('Sport', [9, 55], [10, 40], { label: '3.', room: 'Halle' }),
])

const rowTitles = (c: Element) =>
  [...c.querySelectorAll('.sh-schedule__row .sh-schedule__name, .sh-schedule__row .sh-schedule__break')]
    .map(el => el.textContent)

const hrefs = (c: Element) => [...c.querySelectorAll('a')].map(a => a.getAttribute('href'))

const status = (c: Element) => c.querySelector('.sh-schedule__status')!.textContent

function renderAt(now: Date, props: { timetables: TodayTimetable[]; events?: WelcomeEvent[] }) {
  vi.setSystemTime(now)
  return render(<TodayScheduleCard timetables={props.timetables} events={props.events ?? []} />)
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  sessionStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('TodayScheduleCard', () => {
  it('merges lessons and events in time order, with breaks between lessons', () => {
    const { container } = renderAt(at(7), {
      timetables: [EMMA],
      events: [event('Dentist', [9, 0], [9, 20]), event('Breakfast', [7, 15], [7, 30])],
    })
    expect(rowTitles(container)).toEqual([
      'Breakfast', 'Mathe', 'Deutsch', 'Dentist', 'Pause · 20 min', 'Sport',
    ])
    expect(container.querySelector('ul.sh-schedule__list > li')).not.toBeNull()
  })

  it('hides a break that is not between two lessons, and untitled slots', () => {
    const tt = timetable('t', 'T', [
      lesson('Früh', [7, 30], [7, 50], { kind: 'break' }),
      lesson('Mathe', [8, 0], [8, 45]),
      lesson('empty', [8, 50], [9, 35], { title: null, label: '2.' }),
      lesson('Mittag', [12, 0], [12, 45], { kind: 'break' }),
    ])
    const { container } = renderAt(at(7), { timetables: [tt] })
    expect(rowTitles(container)).toEqual(['Mathe'])
  })

  it('puts the now line before the first row that has not started and moves it every minute', () => {
    const { container } = renderAt(at(8, 49), { timetables: [EMMA] })
    const kids = () => [...container.querySelectorAll('.sh-schedule__list > li')]
    const nowAt = () => kids().findIndex(li => li.classList.contains('sh-schedule__now'))
    expect(kids()[nowAt()].getAttribute('aria-hidden')).toBe('true')
    // Mathe is past, Deutsch (08:50) is next → line between them.
    expect(nowAt()).toBe(1)
    expect(kids()[0].classList.contains('is-past')).toBe(true)
    act(() => { vi.advanceTimersByTime(2 * 60_000) })
    // 08:51 — Deutsch is running, so the line moved below it.
    expect(nowAt()).toBe(2)
    expect(kids()[1].classList.contains('is-current')).toBe(true)
  })

  it('cleans up its timer on unmount', () => {
    const { unmount } = renderAt(at(8), { timetables: [EMMA] })
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('header: before, during, between and after school', () => {
    const r1 = renderAt(at(7, 48), { timetables: [EMMA] })
    expect(status(r1.container)).toBe(`Next: Mathe · ${clock(at(8).getTime())} · Room 204 · in 12 min`)
    expect(r1.container.querySelector('[aria-live="polite"]')).not.toBeNull()
    cleanup()
    const r2 = renderAt(at(9, 10), { timetables: [EMMA] })
    expect(status(r2.container)).toBe(`Now: Deutsch until ${clock(at(9, 35).getTime())}`)
    cleanup()
    const r3 = renderAt(at(9, 40), { timetables: [EMMA] })
    expect(status(r3.container)).toBe(
      `Next: Sport · ${clock(at(9, 55).getTime())} · Room Halle · in 15 min`)
    cleanup()
    const r4 = renderAt(at(14), { timetables: [EMMA] })
    expect(status(r4.container)).toBe("School's out · 3 lessons today")
  })

  it('header skips cancelled lessons and says so when nothing is left', () => {
    const cancelled = timetable('t', 'T', [
      lesson('Mathe', [8, 0], [8, 45], { status: 'cancelled' }),
    ])
    const agenda = buildAgenda([cancelled], [])
    expect(scheduleStatus(agenda.rows, at(7).getTime()).text).toBe('No lessons today')
  })

  it('marks an event overlapping a lesson on both rows', () => {
    const { container } = renderAt(at(7), {
      timetables: [EMMA], events: [event('Dentist', [8, 30], [9, 0])],
    })
    const warns = [...container.querySelectorAll('.sh-schedule__warn')]
      .map(w => w.getAttribute('aria-label'))
    expect(warns).toEqual([
      `Overlaps with Dentist ${clockRange(at(8, 30).getTime(), at(9).getTime())}`,
      `Overlaps with Mathe ${clockRange(at(8).getTime(), at(8, 45).getTime())}; `
        + `Overlaps with Deutsch ${clockRange(at(8, 50).getTime(), at(9, 35).getTime())}`,
      `Overlaps with Dentist ${clockRange(at(8, 30).getTime(), at(9).getTime())}`,
    ])
    expect(container.querySelectorAll('.has-overlap')).toHaveLength(3)
  })

  it('a cancelled lesson never conflicts', () => {
    const tt = timetable('t', 'T', [
      lesson('Mathe', [8, 0], [8, 45], { status: 'cancelled' }),
    ])
    const { container } = renderAt(at(7), {
      timetables: [tt], events: [event('Dentist', [8, 0], [8, 30])],
    })
    expect(container.querySelector('.sh-schedule__warn')).toBeNull()
  })

  it('filters lessons by timetable with chips; events always show; the choice is remembered', () => {
    const leo = timetable('tt-leo', 'Leo', [lesson('Kunst', [8, 10], [8, 55])])
    const props = { timetables: [EMMA, leo], events: [event('Dentist', [11, 0], [11, 30])] }
    const { container, getByRole } = renderAt(at(7), props)
    const chips = [...container.querySelectorAll('.sh-schedule__chip')].map(c => c.textContent)
    expect(chips).toEqual(['All', 'Emma', 'Leo'])
    // "All": names in front of lesson titles.
    expect(rowTitles(container)).toContain('Leo · Kunst')
    expect(rowTitles(container)).toContain('Emma · Mathe')
    fireEvent.click(getByRole('button', { name: 'Leo' }))
    expect(rowTitles(container)).toEqual(['Kunst', 'Dentist'])
    expect(getByRole('button', { name: 'Leo' }).getAttribute('aria-pressed')).toBe('true')
    expect(localStorage.getItem(FILTER_KEY)).toBe('tt-leo')
    expect(hrefs(container)).toContain('/calendar?tab=timetable&tt=tt-leo')
    cleanup()
    // Remembered on the next visit.
    const again = renderAt(at(7), props)
    expect(rowTitles(again.container)).toEqual(['Kunst', 'Dentist'])
  })

  it('no chips with a single timetable, and a stale remembered filter falls back to all', () => {
    localStorage.setItem(FILTER_KEY, 'tt-gone')
    const { container } = renderAt(at(7), { timetables: [EMMA] })
    expect(container.querySelector('.sh-schedule__chip')).toBeNull()
    expect(rowTitles(container)[0]).toBe('Mathe')
    expect(hrefs(container)).toContain('/calendar?tab=timetable&tt=tt-emma')
  })

  it('survives blocked storage', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const leo = timetable('tt-leo', 'Leo', [lesson('Kunst', [8, 10], [8, 55])])
    const { getByRole } = renderAt(at(7), { timetables: [EMMA, leo] })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    fireEvent.click(getByRole('button', { name: 'Leo' }))
    expect(getByRole('button', { name: 'Leo' }).getAttribute('aria-pressed')).toBe('true')
    get.mockRestore()
    vi.restoreAllMocks()
  })

  it('shows all-day events in their own labelled row, as links — not like filter buttons', () => {
    const leo = timetable('tt-leo', 'Leo', [lesson('Kunst', [8, 10], [8, 55])])
    const { container, getByRole } = renderAt(at(7), {
      timetables: [EMMA, leo],
      events: [event('Wandertag', [0, 0], [23, 59], { all_day: true })],
    })
    const row = container.querySelector('.sh-schedule__allday')!
    expect(row.querySelector('.sh-schedule__allday-label')?.textContent).toBe('All day')
    const item = row.querySelector('a.sh-schedule__allday-item')!
    expect(item.textContent).toContain('Wandertag')
    expect(item.getAttribute('href')).toBe('/calendar')
    expect(item.querySelector('.sh-schedule__dot')).not.toBeNull()
    expect(row.querySelector('button')).toBeNull()
    expect(item.hasAttribute('aria-pressed')).toBe(false)
    // Filter chips: a separate group, labelled "Show:", of aria-pressed buttons.
    const group = getByRole('group', { name: 'Show:' })
    expect(group.contains(item)).toBe(false)
    const chips = [...group.querySelectorAll('.sh-schedule__chip')]
    expect(chips.every(c => c.tagName === 'BUTTON' && c.hasAttribute('aria-pressed'))).toBe(true)
    expect(rowTitles(container)).not.toContain('Wandertag')
    // The all-day row sits between the filter and the list.
    const order = [...container.querySelectorAll('.sh-schedule__filter, .sh-schedule__allday, .sh-schedule__list')]
      .map(el => el.className.split(' ')[0])
    expect(order).toEqual(['sh-schedule__filter', 'sh-schedule__allday', 'sh-schedule__list'])
  })

  it('styles cancelled, changed and extra lessons', () => {
    const tt = timetable('t', 'T', [
      lesson('Mathe', [8, 0], [8, 45], { status: 'cancelled' }),
      lesson('Deutsch', [8, 50], [9, 35], {
        status: 'changed', room: '112',
        original: {
          id: 'Deutsch', weekday: 0, start: '08:50', end: '09:35', kind: 'lesson', label: null,
          title: 'Deutsch', room: '204', teacher: null, note: null, color: null, icon: null,
        },
      }),
      lesson('Chor', [13, 0], [13, 45], { status: 'added' }),
    ])
    const { container } = renderAt(at(7), { timetables: [tt] })
    const rows = [...container.querySelectorAll('.sh-schedule__row--lesson')]
    expect(rows[0].classList.contains('is-cancelled')).toBe(true)
    expect(rows[0].querySelector('.sh-timetable-badge--cancelled')?.textContent).toBe('Cancelled')
    expect(rows[0].querySelector('.sr-only')?.textContent).toContain('cancelled')
    expect(rows[1].classList.contains('is-changed')).toBe(true)
    expect(rows[1].querySelector('.sh-schedule__meta--change')?.textContent).toBe('Room 204 → 112')
    expect(rows[2].querySelector('.sh-timetable-badge--added')?.textContent).toBe('Extra')
    // Row labels carry time range, title, room and status.
    expect(rows[1].querySelector('.sr-only')?.textContent).toBe(
      `${clockRange(at(8, 50).getTime(), at(9, 35).getTime())}, Deutsch, room 112, changed: Room 204 → 112`)
  })

  it('shows icons large in Picture view', () => {
    localStorage.setItem('sh-timetable-view:tt-emma', JSON.stringify({ picture: true }))
    const { container } = renderAt(at(7), { timetables: [EMMA] })
    const first = container.querySelector('.sh-schedule__row--lesson')!
    expect(first.classList.contains('is-picture')).toBe(true)
    expect(first.querySelector('.sh-schedule__icon')?.textContent).toBe('🔢')
    // Without an icon the title's first letter stands in.
    const second = container.querySelectorAll('.sh-schedule__row--lesson')[1]
    expect(second.querySelector('.sh-schedule__icon')?.textContent).toBe('D')
    cleanup()
    localStorage.clear()
    const plain = renderAt(at(7), { timetables: [EMMA] })
    expect(plain.container.querySelector('.is-picture')).toBeNull()
  })

  it('links events to the calendar', () => {
    const { container } = renderAt(at(7), {
      timetables: [EMMA], events: [event('Dentist', [11, 0], [11, 30])],
    })
    const link = container.querySelector('.sh-schedule__row--event a')
    expect(link?.getAttribute('href')).toBe('/calendar')
  })

  it('collapses all but the most recent past row behind "Show N earlier"', () => {
    const tt = timetable('t', 'T', [
      lesson('A', [7, 0], [7, 30]),
      lesson('B', [7, 30], [8, 0]),
      lesson('C', [8, 0], [8, 30]),
      lesson('D', [8, 30], [9, 0]),
      lesson('E', [9, 0], [9, 30]),
    ])
    // A long event that started early and is still running is never hidden.
    const long = event('Handwerker', [6, 30], [12, 0])
    const { container, getByRole } = renderAt(at(8, 40), { timetables: [tt], events: [long] })
    expect(rowTitles(container)).toEqual(['Handwerker', 'C', 'D', 'E'])
    const toggle = getByRole('button', { name: 'Show 2 earlier' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.classList.contains('sh-schedule__earlier')).toBe(true)
    // The toggle is the first thing in the list area.
    expect(container.querySelector('.sh-schedule__list')?.previousElementSibling).toBe(toggle)
    fireEvent.click(toggle)
    expect(rowTitles(container)).toEqual(['Handwerker', 'A', 'B', 'C', 'D', 'E'])
    expect(getByRole('button', { name: 'Hide earlier' }).getAttribute('aria-expanded')).toBe('true')
    // Remembered for today (per tab session).
    cleanup()
    const again = renderAt(at(8, 41), { timetables: [tt], events: [long] })
    expect(rowTitles(again.container)).toEqual(['Handwerker', 'A', 'B', 'C', 'D', 'E'])
    cleanup()
    // ...but not on another day.
    sessionStorage.setItem('sh-welcome-schedule:earlier', '2026-09-27')
    const other = renderAt(at(8, 42), { timetables: [tt], events: [long] })
    expect(rowTitles(other.container)).toEqual(['Handwerker', 'C', 'D', 'E'])
  })

  it('does not collapse with two or fewer past rows, and hides past breaks with the rest', () => {
    const { container, queryByRole } = renderAt(at(8, 49), { timetables: [EMMA] })
    expect(queryByRole('button', { name: /earlier/ })).toBeNull()
    expect(rowTitles(container)[0]).toBe('Mathe')
    const rows = buildAgenda([EMMA], []).rows
    // 10:00 — Mathe, Deutsch, Pause ended; Sport runs. 2 lessons past → no collapse.
    expect(collapsePast(rows, at(10).getTime()).hidden).toBe(0)
    const tt = timetable('t', 'T', [
      lesson('A', [7, 0], [7, 30]), lesson('P', [7, 30], [7, 40], { kind: 'break' }),
      lesson('B', [7, 40], [8, 0]), lesson('C', [8, 0], [8, 30]), lesson('D', [9, 0], [9, 30]),
    ])
    const r = collapsePast(buildAgenda([tt], []).rows, at(8, 45).getTime())
    expect(r.hidden).toBe(2)
    expect(r.rows.map(x => (x.type === 'event' ? x.event.summary : x.lesson.title))).toEqual(['C', 'D'])
  })

  it('survives blocked sessionStorage for the earlier toggle', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('x') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('x') })
    const tt = timetable('t', 'T', [
      lesson('A', [7, 0], [7, 20]), lesson('B', [7, 20], [7, 40]),
      lesson('C', [7, 40], [8, 0]), lesson('D', [9, 0], [9, 30]),
    ])
    const { getByRole, container } = renderAt(at(8, 30), { timetables: [tt] })
    fireEvent.click(getByRole('button', { name: 'Show 2 earlier' }))
    expect(rowTitles(container)).toEqual(['A', 'B', 'C', 'D'])
    vi.restoreAllMocks()
  })

  it('header: an ongoing event shows as "Now", after an ongoing lesson', () => {
    const agenda = (evs: WelcomeEvent[], tts = [EMMA]) => buildAgenda(tts, evs).rows
    // Event only (between lessons).
    const onlyEvent = agenda([event('Dentist', [9, 36], [10, 30])])
    expect(scheduleStatus(onlyEvent, at(9, 40).getTime()).text)
      .toBe(`Now: Dentist until ${clock(at(10, 30).getTime())}`)
    // Lesson and event together: lesson first, then the event.
    const both = agenda([event('Dentist', [9, 0], [9, 20])])
    expect(scheduleStatus(both, at(9, 10).getTime())).toEqual({
      state: 'now',
      text: `Now: Deutsch until ${clock(at(9, 35).getTime())} · Dentist until ${clock(at(9, 20).getTime())}`,
      soon: null,
    })
  })

  it('header: "Next" picks the earliest upcoming lesson or event', () => {
    const rows = buildAgenda([EMMA], [event('Bus', [7, 40], [7, 50])]).rows
    expect(scheduleStatus(rows, at(7, 30).getTime())).toMatchObject({
      text: `Next: Bus · ${clock(at(7, 40).getTime())}`, soon: 'in 10 min',
    })
    expect(scheduleStatus(rows, at(7, 55).getTime())).toMatchObject({
      text: `Next: Mathe · ${clock(at(8).getTime())} · Room 204`, soon: 'in 5 min',
    })
    // After school with an evening event: both facts.
    const evening = buildAgenda([EMMA], [event('Elternabend', [18, 0], [19, 0])]).rows
    expect(scheduleStatus(evening, at(14).getTime()).text)
      .toBe(`School's out · 3 lessons today · Next: Elternabend · ${clock(at(18).getTime())}`)
    // Only events, no lessons left.
    const cancelled = timetable('t', 'T', [lesson('Mathe', [8, 0], [8, 45], { status: 'cancelled' })])
    const r = buildAgenda([cancelled], [event('Arzt', [10, 0], [10, 30])]).rows
    expect(scheduleStatus(r, at(9).getTime()).text)
      .toBe(`Next: Arzt · ${clock(at(10).getTime())}`)
    expect(scheduleStatus(r, at(11).getTime()).text).toBe('No lessons today')
  })

  it('keeps the countdown out of the live region: a minute tick changes only the aria-hidden part', () => {
    const { container } = renderAt(at(7, 48), { timetables: [EMMA] })
    const live = () => container.querySelector('[aria-live="polite"]')!
    const countdown = () => container.querySelector('.sh-schedule__soon')!
    expect(live().textContent).toBe(`Next: Mathe · ${clock(at(8).getTime())} · Room 204`)
    expect(countdown().getAttribute('aria-hidden')).toBe('true')
    expect(live().contains(countdown())).toBe(false)
    expect(countdown().textContent).toContain('in 12 min')
    const before = live().textContent
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(countdown().textContent).toContain('in 11 min')
    expect(live().textContent).toBe(before)
  })

  it('an event ending exactly when a lesson starts does not conflict', () => {
    const { container } = renderAt(at(7), {
      timetables: [EMMA], events: [event('Bus', [7, 30], [8, 0])],
    })
    expect(container.querySelector('.sh-schedule__warn')).toBeNull()
  })

  it('a timed event that started before today shows its start day', () => {
    const lateStart = new Date(DAY[0], DAY[1], DAY[2] - 1, 21, 0)
    const ev: WelcomeEvent = {
      id: 'party', summary: 'Party', start: lateStart.toISOString(), end: iso(1, 0), all_day: false,
    }
    const { container } = renderAt(at(0, 30), { timetables: [EMMA], events: [ev] })
    const time = container.querySelector('.sh-schedule__row--event .sh-schedule__time')!
    const wd = lateStart.toLocaleDateString(undefined, { weekday: 'short' })
    expect(time.textContent).toBe(`${wd} ${clock(lateStart.getTime())}`)
    // Today's rows carry no day prefix.
    expect(container.querySelector('.sh-schedule__row--lesson .sh-schedule__day')).toBeNull()
  })

  it('a11y: the label chip is in the row text, the earlier toggle controls the list, arrows are hidden', () => {
    const tt = timetable('t', 'T', [
      lesson('A', [7, 0], [7, 20], { label: '1.' }), lesson('B', [7, 20], [7, 40]),
      lesson('C', [7, 40], [8, 0]), lesson('D', [9, 0], [9, 30], { label: '4.', room: '12' }),
    ])
    const { container, getByRole } = renderAt(at(8, 30), { timetables: [tt] })
    const d = [...container.querySelectorAll('.sh-schedule__row--lesson')].at(-1)!
    expect(d.querySelector('.sr-only')?.textContent)
      .toBe(`4., ${clockRange(at(9).getTime(), at(9, 30).getTime())}, D, room 12`)
    const toggle = getByRole('button', { name: 'Show 2 earlier' })
    const list = container.querySelector('.sh-schedule__list')!
    expect(list.id).not.toBe('')
    expect(toggle.getAttribute('aria-controls')).toBe(list.id)
    const links = [...container.querySelectorAll('.sh-schedule__links a')]
    expect(links.map(a => a.querySelector('[aria-hidden="true"]')?.textContent)).toEqual(['→', '→'])
    expect(getByRole('link', { name: 'View timetable' })).toBeTruthy()
  })
})
