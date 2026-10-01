/**
 * SpaceCalendarHost — the space Calendar tab's Events | Timetable
 * switch (only when both features are on) and the gating combinations.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'

const tabProps = vi.fn()
vi.mock('./SpaceTimetableTab', () => ({
  SpaceTimetableTab: (p: { spaceId: string; canEdit: boolean }) => {
    tabProps(p)
    return <div data-testid="space-timetable">timetable of {p.spaceId}</div>
  },
}))

import { SpaceCalendarHost, resetCalendarModes } from './SpaceCalendarHost'

const events = () => <div data-testid="events">agenda</div>

beforeEach(() => { tabProps.mockReset(); resetCalendarModes() })
afterEach(() => cleanup())

describe('SpaceCalendarHost', () => {
  it('calendar only: the agenda, no switch', () => {
    const { getByTestId, queryByRole, queryByTestId } = render(
      <SpaceCalendarHost spaceId="s1" features={{ calendar: true, timetable: false }} canEdit events={events} />)
    expect(getByTestId('events')).toBeTruthy()
    expect(queryByRole('group', { name: 'Calendar view' })).toBeNull()
    expect(queryByTestId('space-timetable')).toBeNull()
  })

  it('timetable only: straight to the timetable, no switch', () => {
    const { getByTestId, queryByRole, queryByTestId } = render(
      <SpaceCalendarHost spaceId="s1" features={{ calendar: false, timetable: true }} canEdit={false} events={events} />)
    expect(getByTestId('space-timetable')).toBeTruthy()
    expect(queryByTestId('events')).toBeNull()
    expect(queryByRole('group', { name: 'Calendar view' })).toBeNull()
    expect(tabProps).toHaveBeenLastCalledWith({ spaceId: 's1', canEdit: false })
  })

  it('both: Events | Timetable, Events first; the choice sticks per space', () => {
    const { getByRole, getByTestId, queryByTestId, rerender } = render(
      <SpaceCalendarHost spaceId="s1" features={{ calendar: true, timetable: true }} canEdit events={events} />)
    const group = getByRole('group', { name: 'Calendar view' })
    expect(group).toBeTruthy()
    const ev = getByRole('button', { name: 'Events' })
    const tt = getByRole('button', { name: 'Timetable' })
    expect(ev.getAttribute('aria-pressed')).toBe('true')
    expect(getByTestId('events')).toBeTruthy()
    fireEvent.click(tt)
    expect(tt.getAttribute('aria-pressed')).toBe('true')
    expect(getByTestId('space-timetable')).toBeTruthy()
    expect(queryByTestId('events')).toBeNull()
    expect(tabProps).toHaveBeenLastCalledWith({ spaceId: 's1', canEdit: true })
    // Leaving the tab and coming back keeps Timetable…
    cleanup()
    const again = render(
      <SpaceCalendarHost spaceId="s1" features={{ calendar: true, timetable: true }} canEdit events={events} />)
    expect(again.getByTestId('space-timetable')).toBeTruthy()
    // …another space starts on Events.
    again.rerender(
      <SpaceCalendarHost spaceId="s2" features={{ calendar: true, timetable: true }} canEdit events={events} />)
    expect(again.getByTestId('events')).toBeTruthy()
    void rerender
  })

  it('a remembered Timetable falls back to Events when the feature goes off', () => {
    const a = render(
      <SpaceCalendarHost spaceId="s1" features={{ calendar: true, timetable: true }} canEdit events={events} />)
    fireEvent.click(a.getByRole('button', { name: 'Timetable' }))
    a.rerender(<SpaceCalendarHost spaceId="s1" features={{ calendar: true, timetable: false }} canEdit events={events} />)
    expect(a.getByTestId('events')).toBeTruthy()
    expect(a.queryByTestId('space-timetable')).toBeNull()
  })
})
