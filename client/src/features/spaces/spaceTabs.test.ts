/**
 * Space tab gating: which tabs a space's feature toggles show, and what
 * the Calendar tab holds (Events, Timetable, or both behind a switch).
 */
import { describe, it, expect } from 'vitest'
import { calendarModes, calendarTabLabel, visibleSpaceTabs } from './spaceTabs'

describe('visibleSpaceTabs', () => {
  it('defaults: every tab but the opt-ins (map, timetable-only)', () => {
    expect(visibleSpaceTabs(undefined, false)).toEqual(
      ['feed', 'members', 'pages', 'calendar', 'tasks', 'stickies', 'gallery', 'bazaar'])
  })

  it('the Calendar tab shows when calendar or timetable is on', () => {
    const tabs = (f: Record<string, boolean>) => visibleSpaceTabs(f, false).includes('calendar')
    expect(tabs({ calendar: true, timetable: false })).toBe(true)
    expect(tabs({ calendar: true, timetable: true })).toBe(true)
    expect(tabs({ calendar: false, timetable: true })).toBe(true)
    expect(tabs({ calendar: false, timetable: false })).toBe(false)
    expect(tabs({ calendar: false })).toBe(false)
  })

  it('admins get moderation, location adds the map', () => {
    const tabs = visibleSpaceTabs({ location: true }, true)
    expect(tabs).toContain('map')
    expect(tabs[tabs.length - 1]).toBe('moderation')
  })
})

describe('calendarModes / calendarTabLabel', () => {
  it('calendar only (timetable off by default) → events, labelled Calendar', () => {
    expect(calendarModes(undefined)).toEqual(['events'])
    expect(calendarModes({ calendar: true })).toEqual(['events'])
    expect(calendarTabLabel({ calendar: true })).toBe('Calendar')
  })

  it('both → the Events | Timetable switch', () => {
    expect(calendarModes({ calendar: true, timetable: true })).toEqual(['events', 'timetable'])
    expect(calendarTabLabel({ calendar: true, timetable: true })).toBe('Calendar')
  })

  it('timetable only → straight to the timetable, labelled Timetable', () => {
    expect(calendarModes({ calendar: false, timetable: true })).toEqual(['timetable'])
    expect(calendarTabLabel({ calendar: false, timetable: true })).toBe('Timetable')
  })
})

describe('parseSpaceTab', () => {
  it('accepts a linkable tab name and nothing else', async () => {
    const { parseSpaceTab } = await import('./spaceTabs')
    expect(parseSpaceTab('tasks')).toBe('tasks')
    expect(parseSpaceTab('stickies')).toBe('stickies')
    expect(parseSpaceTab('moderation')).toBeNull()
    expect(parseSpaceTab('nope')).toBeNull()
    expect(parseSpaceTab(undefined)).toBeNull()
    expect(parseSpaceTab(['tasks'])).toBeNull()
  })
})
