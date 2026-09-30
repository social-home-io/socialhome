import { describe, it, expect, vi, onTestFinished } from 'vitest'
import { render, fireEvent, within } from '@testing-library/preact'
import { TimetableGrid, todayColumn } from './TimetableGrid'
import type { EntryPrefill } from './layout'
import { DEFAULT_VIEW_PREFS, type ViewPrefs } from './viewPrefs'
import { entry, schoolWeek, timetable } from './testUtils'
import { BASE_PPM } from './layout'
import type { Timetable, TimetableEntry } from '@/types'

// 2026-09-30 is a Wednesday (weekday 2).
const WED = new Date(2026, 8, 30, 10, 0)

function renderGrid(tt: Timetable, prefs: Partial<ViewPrefs> = {}) {
  const onEdit = vi.fn<(e: TimetableEntry, group?: string[]) => void>()
  const onAdd = vi.fn<(p: EntryPrefill) => void>()
  const onPrefs = vi.fn<(p: Partial<ViewPrefs>) => void>()
  const utils = render(
    <TimetableGrid tt={tt} prefs={{ ...DEFAULT_VIEW_PREFS, ...prefs }} onPrefs={onPrefs}
                   onEdit={onEdit} onAdd={onAdd} now={WED} />,
  )
  return { ...utils, onEdit, onAdd, onPrefs }
}

describe('TimetableGrid — layout choice', () => {
  it('uses the Periods table when every day has the same slots', () => {
    const { container } = renderGrid(timetable({ entries: schoolWeek() }))
    expect(container.querySelector('table.sh-timetable-periods')).not.toBeNull()
    expect(container.querySelector('.sh-timetable-timeline')).toBeNull()
  })

  it('falls back to the Timeline when days differ', () => {
    const entries = [...schoolWeek([0, 2, 3, 4]), entry(1, '07:15', '08:00', { title: 'Musik' })]
    const { container } = renderGrid(timetable({ entries }))
    expect(container.querySelector('.sh-timetable-timeline')).not.toBeNull()
    expect(container.querySelector('table.sh-timetable-periods')).toBeNull()
  })

  it('honours a forced layout and reports a new choice', () => {
    const { container, getByRole, onPrefs } = renderGrid(
      timetable({ entries: schoolWeek() }), { layout: 'timeline' })
    expect(container.querySelector('.sh-timetable-timeline')).not.toBeNull()
    expect(getByRole('button', { name: 'Timeline' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(getByRole('button', { name: 'Periods' }))
    expect(onPrefs).toHaveBeenCalledWith({ layout: 'periods' })
  })
})

describe('TimetableGrid — Periods table', () => {
  it('is a captioned table with column and row headers', () => {
    const tt = timetable({ entries: schoolWeek() })
    const { container } = renderGrid(tt)
    const table = container.querySelector('table.sh-timetable-periods')!
    expect(table.querySelector('caption')?.textContent).toBe(tt.name)
    const cols = Array.from(table.querySelectorAll('thead th[scope="col"]')).slice(1)
    expect(cols.map(th => th.querySelector('[aria-hidden]')?.textContent))
      .toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri'])
    const firstRow = table.querySelector('tbody tr th[scope="row"]')!
    expect(firstRow.textContent).toBe('1.08:00–08:45')
  })

  it('renders a shared break as one thin spanning band', () => {
    const { container } = renderGrid(timetable({ entries: schoolWeek() }))
    const band = container.querySelector('tr.sh-timetable-periods__band')!
    expect(band.querySelector('td')!.getAttribute('colspan')).toBe('5')
    expect(band.textContent).toContain('Pause')
    expect(band.textContent).toContain('09:35–09:55')
  })

  it('the band edits that break on every day (a group)', () => {
    const tt = timetable({ entries: schoolWeek() })
    const { container, onEdit } = renderGrid(tt)
    fireEvent.click(container.querySelector('.sh-timetable-band')!)
    const breaks = tt.entries.filter(e => e.kind === 'break')
    expect(onEdit).toHaveBeenCalledWith(
      expect.objectContaining({ weekday: 0, kind: 'break' }),
      breaks.map(b => b.id))
  })

  it('merges a double lesson with rowSpan', () => {
    const titles = [['Mathe', 'Mathe', 'Deutsch', 'Sport']]
    const { container } = renderGrid(timetable({ entries: schoolWeek([0], titles), days: [0] }))
    const merged = container.querySelector('td[rowspan="2"]')!
    expect(merged.textContent).toContain('Mathe')
    // The merged block spans both periods.
    expect(merged.textContent).toContain('08:00–09:35')
    expect(merged.querySelector('button')!.getAttribute('aria-label'))
      .toBe('Monday, 1st lesson, 08:00–09:35, Mathe')
    expect(container.querySelectorAll('.sh-timetable-visual tbody td button[aria-label*="Mathe"]'))
      .toHaveLength(1)
  })

  it('opens the entry on click', () => {
    const tt = timetable({ entries: schoolWeek() })
    const { container, onEdit } = renderGrid(tt)
    const first = container.querySelector('tbody td .sh-timetable-block') as HTMLElement
    fireEvent.click(first)
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ weekday: 0, start: '08:00' }))
  })

  it('highlights today', () => {
    const { container } = renderGrid(timetable({ entries: schoolWeek() }))
    const today = container.querySelector('thead .sh-timetable-dayhead--today')!
    expect(today.textContent).toContain('Wed')
  })
})

describe('TimetableGrid — Timeline', () => {
  const uneven = () => timetable({ entries: [
    entry(0, '08:00', '08:45', { title: 'Mathe' }),
    entry(1, '07:15', '08:00', { title: 'Musik' }),
    entry(1, '08:00', '08:45', { title: 'Deutsch' }),
  ] })

  it('positions blocks proportionally on the shared axis', () => {
    const { container } = renderGrid(uneven())
    const slots = Array.from(container.querySelectorAll<HTMLElement>('.sh-timetable-timeline__slot'))
    const byTitle = (s: string) => slots.find(el => el.textContent!.includes(s))!
    // Axis starts at 07:00 (07:15 − 15 min, rounded down to the half hour).
    expect(byTitle('Musik').style.top).toBe(`${15 * BASE_PPM}px`)
    expect(byTitle('Mathe').style.top).toBe(`${60 * BASE_PPM}px`)
    expect(parseFloat(byTitle('Mathe').style.height)).toBeCloseTo(45 * BASE_PPM)
  })

  it('makes each day a labelled list of buttons with a full aria-label', () => {
    const tt = uneven()
    tt.entries[2] = { ...tt.entries[2], room: '204' }
    const { getAllByRole, getByRole } = renderGrid(tt)
    const lists = getAllByRole('list')
    expect(lists).toHaveLength(5)
    const tue = getByRole('list', { name: 'Tuesday' })
    const buttons = within(tue).getAllByRole('button')
    expect(buttons.map(b => b.getAttribute('aria-label'))).toEqual([
      'Tuesday, 1st lesson, 07:15–08:00, Musik',
      'Tuesday, 2nd lesson, 08:00–08:45, Deutsch, room 204',
    ])
  })

  it('compresses a long empty stretch into a ⋯ marker', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '09:00'), entry(0, '15:00', '16:00')] })
    const { container } = renderGrid(tt, { layout: 'timeline' })
    const gaps = container.querySelectorAll('.sh-timetable-timeline__gap')
    expect(gaps).toHaveLength(1)
    expect(gaps[0].textContent).toBe('⋯')
  })

  it('renders breaks as hatched bands', () => {
    const tt = timetable({ entries: [
      entry(0, '08:00', '08:45'), entry(0, '08:45', '09:05', { kind: 'break', title: 'Pause' }),
      entry(1, '09:00', '09:45'),
    ] })
    const { container } = renderGrid(tt)
    const brk = container.querySelector('.sh-timetable-block--break')!
    expect(brk.textContent).toContain('Pause')
    expect(brk.getAttribute('aria-label')).toBe('Monday, Break, 08:45–09:05, Pause')
  })

  it('clicking empty column space opens a prefilled new entry, snapped to 5 min', () => {
    const { getByRole, onAdd } = renderGrid(uneven())
    const wed = getByRole('list', { name: 'Wednesday, today' })
    // y = 62 px → 07:00 + 62/1.2 min ≈ 07:51.7 → 07:50.
    fireEvent.click(wed, { clientY: 62 })
    expect(onAdd).toHaveBeenCalledWith({ weekday: 2, start: '07:50', end: '08:35' })
  })

  it('"+" in a day heading adds after the last entry plus the gap', () => {
    const { getByRole, onAdd } = renderGrid(uneven())
    fireEvent.click(getByRole('button', { name: 'Add a lesson on Tuesday' }))
    expect(onAdd).toHaveBeenCalledWith({ weekday: 1, start: '08:50', end: '09:35' })
    fireEvent.click(getByRole('button', { name: 'Add a lesson on Friday' }))
    expect(onAdd).toHaveBeenLastCalledWith({ weekday: 4, start: '08:00', end: '08:45' })
  })

  it('renders an untitled lesson as a dashed "+" slot', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '08:45'), entry(1, '09:00', '09:45')] })
    const { container } = renderGrid(tt)
    const empty = container.querySelector('.sh-timetable-block--empty')!
    expect(empty.textContent).toBe('+')
    expect(empty.getAttribute('aria-label')).toBe('Monday, 1st lesson, 08:00–08:45, Empty slot')
  })
})

describe('TimetableGrid — column order', () => {
  it('keeps Mon–Fri in order for a Sunday week start without Sunday', () => {
    const tt = timetable({ week_start: 6, days: [0, 1, 2, 3, 4], entries: [entry(0, '08:00', '08:45')] })
    const { getAllByRole } = renderGrid(tt, { layout: 'timeline' })
    expect(getAllByRole('list').map(l => l.getAttribute('aria-labelledby'))).toEqual([
      'sh-tt-tt1-t-0', 'sh-tt-tt1-t-1', 'sh-tt-tt1-t-2', 'sh-tt-tt1-t-3', 'sh-tt-tt1-t-4',
    ])
  })

  it('puts Sunday first for a Sunday week start that includes it', () => {
    const tt = timetable({ week_start: 6, days: [0, 1, 2, 3, 6], entries: [entry(0, '08:00', '08:45')] })
    const { getAllByRole } = renderGrid(tt, { layout: 'timeline' })
    expect(getAllByRole('list').map(l => l.getAttribute('aria-labelledby')))
      .toEqual(['sh-tt-tt1-t-6', 'sh-tt-tt1-t-0', 'sh-tt-tt1-t-1', 'sh-tt-tt1-t-2', 'sh-tt-tt1-t-3'])
  })
})

describe('TimetableGrid — Picture view and List view', () => {
  it('Picture view leads with the icon, or the first letter, and drops the room', () => {
    const tt = timetable({ days: [0], entries: [
      entry(0, '08:00', '08:45', { title: 'Schwimmen', icon: '🏊', room: 'Hallenbad' }),
      entry(0, '08:50', '09:35', { title: 'Werken', room: 'B1' }),
    ] })
    const { container } = renderGrid(tt, { picture: true })
    const blocks = container.querySelectorAll('.sh-timetable-block--picture')
    expect(blocks).toHaveLength(2)
    expect(blocks[0].querySelector('.sh-timetable-block__icon')?.textContent).toBe('🏊')
    expect(blocks[1].querySelector('.sh-timetable-block__initial')?.textContent).toBe('W')
    expect(container.querySelector('.sh-timetable-visual')!.textContent).not.toContain('Hallenbad')
    // The accessible name still carries everything.
    expect(blocks[0].getAttribute('aria-label')).toContain('room Hallenbad')
  })

  it('an icon-only lesson is named by its preset label', () => {
    const tt = timetable({ days: [0], entries: [entry(0, '08:00', '08:45', { icon: '🎵' })] })
    const { container } = renderGrid(tt)
    expect(container.querySelector('.sh-timetable-block')!.getAttribute('aria-label'))
      .toBe('Monday, 1st lesson, 08:00–08:45, Music')
  })

  it('List view renders one semantic table per day', () => {
    const tt = timetable({ entries: schoolWeek([0, 1]), days: [0, 1] })
    const { getAllByRole, container } = renderGrid(tt, { list: true })
    const tables = getAllByRole('table')
    expect(tables).toHaveLength(2)
    expect(tables[0].querySelector('caption')?.textContent).toBe('Monday')
    expect(container.querySelector('.sh-timetable-visual')).toBeNull()
    // The toggle itself lives in the header row now.
    expect(container.querySelector('.sh-timetable-seg')).toBeNull()
  })

  it('keeps a hidden print twin: the Periods table when the days line up, else the list', () => {
    expect(renderGrid(timetable({ id: 'tt0', entries: schoolWeek() })).container
      .querySelector('.sh-timetable-print')).toBeNull() // only while printing
    window.dispatchEvent(new Event('beforeprint'))
    onTestFinished(() => { window.dispatchEvent(new Event('afterprint')) })
    const { container } = renderGrid(timetable({ entries: schoolWeek() }))
    const print = container.querySelector('.sh-timetable-print-only')!
    expect(print.getAttribute('aria-hidden')).toBe('true')
    expect(print.querySelector('table.sh-timetable-periods')).not.toBeNull()
    const uneven = [...schoolWeek([0, 2, 3, 4]), entry(1, '07:15', '08:00', { title: 'Musik' })]
    const other = renderGrid(timetable({ id: 'tt2', entries: uneven })).container
    const print2 = other.querySelector('.sh-timetable-print-only')!
    expect(print2.querySelector('table.sh-timetable-periods')).toBeNull()
    expect(print2.querySelectorAll('table.sh-timetable-list__day')).toHaveLength(5)
  })
})

describe('todayColumn', () => {
  it('is null when the timetable is not valid today or today is not a school day', () => {
    expect(todayColumn(timetable(), WED)).toBe(2)
    expect(todayColumn(timetable({ valid_today: false }), WED)).toBeNull()
    expect(todayColumn(timetable({ days: [0, 1] }), WED)).toBeNull()
  })
})
