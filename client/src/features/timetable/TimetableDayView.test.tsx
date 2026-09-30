import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { TimetableDayView } from './TimetableDayView'
import { entry, schoolWeek, timetable } from './testUtils'
import type { Timetable } from '@/types'

const WED = new Date(2026, 8, 30, 10, 0) // Wednesday
const SAT = new Date(2026, 9, 3, 10, 0) // Saturday

function renderDay(tt: Timetable, now = WED) {
  const onEdit = vi.fn()
  const onAdd = vi.fn()
  const utils = render(<TimetableDayView tt={tt} picture={false} onEdit={onEdit} onAdd={onAdd} now={now} />)
  return { ...utils, onEdit, onAdd }
}

const selected = (container: Element) =>
  container.querySelector('[role="tab"][aria-selected="true"]')!.textContent

const week = () => timetable({ entries: [
  ...schoolWeek([0, 1, 2, 3, 4], [['Mathe'], ['Deutsch'], ['Sport'], ['Kunst'], ['Musik']]),
] })

describe('TimetableDayView', () => {
  it('defaults to today', () => {
    const { container, getByRole } = renderDay(week())
    expect(selected(container)).toBe('Wed')
    expect(getByRole('tabpanel').textContent).toContain('Sport')
  })

  it('defaults to the next school day on a weekend', () => {
    const { container } = renderDay(week(), SAT)
    expect(selected(container)).toBe('Mon')
  })

  it('switches day with the chips', () => {
    const { container, getByRole } = renderDay(week())
    fireEvent.click(getByRole('tab', { name: 'Friday' }))
    expect(selected(container)).toBe('Fri')
    expect(getByRole('tabpanel').textContent).toContain('Musik')
  })

  it('swipes left to the next day and right to the previous, ignoring short or vertical drags', () => {
    const { container, getByRole } = renderDay(week())
    const panel = getByRole('tabpanel')
    fireEvent.pointerDown(panel, { clientX: 200, clientY: 100 })
    fireEvent.pointerUp(panel, { clientX: 120, clientY: 110 })
    expect(selected(container)).toBe('Thu')
    fireEvent.pointerDown(panel, { clientX: 100, clientY: 100 })
    fireEvent.pointerUp(panel, { clientX: 180, clientY: 100 })
    expect(selected(container)).toBe('Wed')
    // Too short.
    fireEvent.pointerDown(panel, { clientX: 100, clientY: 100 })
    fireEvent.pointerUp(panel, { clientX: 70, clientY: 100 })
    expect(selected(container)).toBe('Wed')
    // Mostly vertical — that's a scroll.
    fireEvent.pointerDown(panel, { clientX: 100, clientY: 100 })
    fireEvent.pointerUp(panel, { clientX: 40, clientY: 300 })
    expect(selected(container)).toBe('Wed')
  })

  it('does not swipe past the last day', () => {
    const { container, getByRole } = renderDay(week())
    fireEvent.click(getByRole('tab', { name: 'Friday' }))
    const panel = getByRole('tabpanel')
    fireEvent.pointerDown(panel, { clientX: 200, clientY: 100 })
    fireEvent.pointerUp(panel, { clientX: 100, clientY: 100 })
    expect(selected(container)).toBe('Fri')
  })

  it('opens a lesson on tap and adds after the last one', () => {
    const { getByRole, onEdit, onAdd } = renderDay(week())
    fireEvent.click(getByRole('button', { name: /Wednesday, 1st lesson, 08:00–08:45, Sport/ }))
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ title: 'Sport' }))
    fireEvent.click(getByRole('button', { name: '+ Add lesson' }))
    expect(onAdd).toHaveBeenCalledWith({ weekday: 2, start: '11:35', end: '12:20' })
  })

  it('shows an empty day with the add button still there', () => {
    const tt = timetable({ entries: [entry(0, '08:00', '08:45')] })
    const { getByRole } = renderDay(tt)
    expect(getByRole('tabpanel').textContent).toContain('Nothing planned for Wednesday.')
    expect(getByRole('button', { name: '+ Add lesson' })).toBeTruthy()
  })

  it('is a real tablist: roving tabindex and Left / Right / Home / End', () => {
    const { container, getByRole } = renderDay(week())
    const tabs = () => Array.from(container.querySelectorAll<HTMLElement>('[role="tab"]'))
    expect(tabs().map(t => t.tabIndex)).toEqual([-1, -1, 0, -1, -1])
    const wed = getByRole('tab', { name: /Wednesday/ })
    wed.focus()
    fireEvent.keyDown(wed, { key: 'ArrowRight' })
    expect(selected(container)).toBe('Thu')
    expect(document.activeElement?.textContent).toBe('Thu')
    fireEvent.keyDown(document.activeElement!, { key: 'End' })
    expect(selected(container)).toBe('Fri')
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' }) // wraps
    expect(selected(container)).toBe('Mon')
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' }) // wraps back
    expect(selected(container)).toBe('Fri')
    fireEvent.keyDown(document.activeElement!, { key: 'Home' })
    expect(selected(container)).toBe('Mon')
    expect(tabs().map(t => t.tabIndex)).toEqual([0, -1, -1, -1, -1])
    expect(document.activeElement).toBe(tabs()[0])
  })

  it('merges a double lesson into one block over the full span, like Periods', () => {
    const tt = timetable({ entries: [
      entry(2, '08:00', '08:45', { title: 'Mathe' }),
      entry(2, '08:45', '09:05', { kind: 'break', title: 'Pause' }),
      entry(2, '09:05', '09:50', { title: 'Mathe' }), // after a break — separate
      entry(2, '09:55', '10:40', { title: 'Schwimmen', room: 'Hallenbad', icon: '🏊' }),
      entry(2, '10:45', '11:30', { title: 'Schwimmen', room: 'Hallenbad', icon: '🏊' }),
      entry(2, '11:30', '12:15', { title: 'Schwimmen', room: 'B1', icon: '🏊' }), // other room
      entry(2, '12:40', '13:25', { title: 'Werken' }),
      entry(2, '13:40', '14:25', { title: 'Werken' }), // 15-min gap > the 5-min default
    ] })
    const { getByRole, getAllByRole, container } = renderDay(tt)
    const swim = getByRole('button', { name: 'Wednesday, 3rd lesson, 09:55–11:30, Schwimmen, room Hallenbad' })
    expect(swim).toBeTruthy()
    const rows = container.querySelectorAll('.sh-timetable-day__row')
    expect(rows).toHaveLength(7)
    const swimRow = swim.closest('.sh-timetable-day__row')!
    expect(swimRow.querySelector('.sh-timetable-day__time')!.textContent).toBe('09:5511:30')
    expect(getAllByRole('button', { name: /Mathe/ })).toHaveLength(2)
    expect(getAllByRole('button', { name: /Werken/ })).toHaveLength(2)
  })
})
