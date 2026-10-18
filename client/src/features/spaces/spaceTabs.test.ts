/**
 * Space sections: which ones a space's feature toggles show, in which
 * order, what the Calendar section holds, and where each one lives in
 * the URL (old ``?tab=`` / ``?view=`` / ``#`` links included).
 */
import { describe, it, expect } from 'vitest'
import {
  SECTION_ORDER, calendarModes, calendarTabLabel, legacySpaceSection, parseSpaceSection,
  sectionAllowed, spaceSectionPath, visibleSpaceSections,
} from './spaceTabs'

const member = { chat: true, follower: false }
const follower = { chat: false, follower: true }

describe('visibleSpaceSections', () => {
  it('defaults: every section but the opt-ins (map, timetable-only), in the one fixed order', () => {
    expect(visibleSpaceSections(undefined, member)).toEqual(
      ['feed', 'chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar'])
  })

  it('never puts Members or Moderation in the bar (they are rows of More)', () => {
    const all = visibleSpaceSections({ location: true }, member)
    expect(all).not.toContain('members')
    expect(all).not.toContain('moderation')
    expect(all).toEqual([...SECTION_ORDER])
  })

  it('keeps SECTION_ORDER whatever the toggles', () => {
    const s = visibleSpaceSections({ pages: false, todo: false, location: true }, member)
    expect(s).toEqual(['feed', 'chat', 'calendar', 'gallery', 'stickies', 'bazaar', 'map'])
  })

  it('the Calendar section shows when calendar or timetable is on', () => {
    const has = (f: Record<string, boolean>) => visibleSpaceSections(f, member).includes('calendar')
    expect(has({ calendar: true, timetable: false })).toBe(true)
    expect(has({ calendar: true, timetable: true })).toBe(true)
    expect(has({ calendar: false, timetable: true })).toBe(true)
    expect(has({ calendar: false, timetable: false })).toBe(false)
    expect(has({ calendar: false })).toBe(false)
  })

  it('Chat only when the space chat is open to the viewer', () => {
    expect(visibleSpaceSections(undefined, { chat: false, follower: false })).not.toContain('chat')
  })
})

describe('visibleSpaceSections — a follower', () => {
  it("gets no Map (the pins and zones are the members' whereabouts) and no Chat", () => {
    expect(visibleSpaceSections({ location: true }, follower)).not.toContain('map')
    expect(visibleSpaceSections({ location: true }, follower)).not.toContain('chat')
    expect(visibleSpaceSections({ location: true }, member)).toContain('map')
  })
})

describe('sectionAllowed', () => {
  it('Members for everyone, Moderation for content authority only', () => {
    expect(sectionAllowed('members', undefined, { ...follower, canModerate: false })).toBe(true)
    expect(sectionAllowed('moderation', undefined, { ...member, canModerate: false })).toBe(false)
    expect(sectionAllowed('moderation', undefined, { ...member, canModerate: true })).toBe(true)
  })

  it('a section whose feature is off is not allowed', () => {
    expect(sectionAllowed('gallery', { gallery: false }, { ...member, canModerate: true })).toBe(false)
    expect(sectionAllowed('map', { location: true }, { ...follower, canModerate: false })).toBe(false)
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

describe('section paths', () => {
  it('the feed is the space root, every other section its own path', () => {
    expect(spaceSectionPath('sp-1', 'feed')).toBe('/spaces/sp-1')
    expect(spaceSectionPath('sp-1', 'chat')).toBe('/spaces/sp-1/chat')
    expect(spaceSectionPath('sp-1', 'members')).toBe('/spaces/sp-1/members')
  })

  it('parses a path segment: none → feed, unknown → null', () => {
    expect(parseSpaceSection(undefined)).toBe('feed')
    expect(parseSpaceSection('')).toBe('feed')
    expect(parseSpaceSection('calendar')).toBe('calendar')
    expect(parseSpaceSection('moderation')).toBe('moderation')
    expect(parseSpaceSection('nope')).toBeNull()
    expect(parseSpaceSection(['tasks'])).toBeNull()
  })
})

describe('legacySpaceSection — links from before sections had paths', () => {
  it('maps ?tab=, ?view=chat and #join-requests', () => {
    expect(legacySpaceSection({ tab: 'tasks' }, '')).toBe('tasks')
    expect(legacySpaceSection({ tab: 'moderation' }, '')).toBe('moderation')
    expect(legacySpaceSection({ view: 'chat' }, '')).toBe('chat')
    expect(legacySpaceSection({}, '#join-requests')).toBe('moderation')
  })

  it('ignores anything else', () => {
    expect(legacySpaceSection({}, '')).toBeNull()
    expect(legacySpaceSection(undefined, undefined)).toBeNull()
    expect(legacySpaceSection({ tab: 'feed' }, '')).toBeNull()
    expect(legacySpaceSection({ tab: 'nope' }, '#other')).toBeNull()
  })
})
