/**
 * Space sections: which ones a space's feature toggles show, in which
 * order, and where each one lives in the URL (old ``?tab=`` / ``?view=``
 * / ``#`` links included, and Calendar links that meant the timetable
 * before it was its own section).
 */
import { describe, it, expect } from 'vitest'
import {
  SECTION_ORDER, gamesSectionShown, legacySpaceSection, parseSpaceSection, redirectedSection,
  sectionAllowed, sectionLabel, spaceSectionPath, visibleSpaceSections,
} from './spaceTabs'

const member = { chat: true, follower: false }
const follower = { chat: false, follower: true }

describe('visibleSpaceSections', () => {
  it('defaults: every section but the opt-ins (map, timetable), in the one fixed order', () => {
    expect(visibleSpaceSections(undefined, member)).toEqual(
      ['feed', 'chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar'])
  })

  it('never puts Members or Moderation in the bar (they are rows of More)', () => {
    const all = visibleSpaceSections({ location: true, timetable: true, allowed_apps: ['chess'] }, member)
    expect(all).not.toContain('members')
    expect(all).not.toContain('moderation')
    expect(all).toEqual([...SECTION_ORDER])
  })

  it('Games follows the games list: shown while a game is allowed, after Chat', () => {
    expect(visibleSpaceSections({ allowed_apps: ['chess'] }, member)).toEqual(
      ['feed', 'chat', 'games', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar'])
    expect(visibleSpaceSections({ allowed_apps: [] }, member)).not.toContain('games')
    expect(visibleSpaceSections(undefined, member)).not.toContain('games')
  })

  it('the people who choose the games see Games before any is allowed (D9)', () => {
    const chooser = { ...member, choosesGames: true }
    expect(visibleSpaceSections({ allowed_apps: [] }, chooser)).toContain('games')
    expect(gamesSectionShown(undefined, chooser)).toBe(true)
  })

  it('no Games without a known writing seat (a non-member, a role still loading)', () => {
    expect(gamesSectionShown({ allowed_apps: ['chess'] }, { ...member, writer: false })).toBe(false)
    expect(sectionAllowed('games', { allowed_apps: ['chess'] }, { ...member, writer: false, canModerate: false }))
      .toBe(false)
  })

  it('never Games for a follower, nor in a global space', () => {
    expect(visibleSpaceSections({ allowed_apps: ['chess'] }, follower)).not.toContain('games')
    expect(gamesSectionShown({ allowed_apps: ['chess'] }, { ...member, choosesGames: true, global: true }))
      .toBe(false)
    expect(sectionAllowed('games', { allowed_apps: ['chess'] }, { ...follower, canModerate: false }))
      .toBe(false)
    expect(sectionAllowed('games', { allowed_apps: ['chess'] }, { ...member, canModerate: false }))
      .toBe(true)
    expect(sectionLabel('games')).toBe('Games')
    expect(parseSpaceSection('games')).toBe('games')
    expect(spaceSectionPath('sp-1', 'games')).toBe('/spaces/sp-1/games')
  })

  it('keeps SECTION_ORDER whatever the toggles', () => {
    const s = visibleSpaceSections({ pages: false, todo: false, location: true }, member)
    expect(s).toEqual(['feed', 'chat', 'calendar', 'gallery', 'stickies', 'bazaar', 'map'])
  })

  it('Timetable is its own section, last in the order (so it usually sits in More)', () => {
    expect(visibleSpaceSections({ timetable: true }, member).at(-1)).toBe('timetable')
    expect(SECTION_ORDER.at(-1)).toBe('timetable')
    expect(visibleSpaceSections({ timetable: false }, member)).not.toContain('timetable')
  })

  it('Calendar follows the calendar feature alone — a timetable no longer keeps it', () => {
    const has = (f: Record<string, boolean>) => visibleSpaceSections(f, member).includes('calendar')
    expect(has({ calendar: true, timetable: false })).toBe(true)
    expect(has({ calendar: false, timetable: true })).toBe(false)
    expect(has({})).toBe(true)
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

describe('redirectedSection — Calendar links from when the timetable lived there', () => {
  it('a Calendar link in a space with only a timetable opens the Timetable', () => {
    expect(redirectedSection('calendar', { calendar: false, timetable: true })).toBe('timetable')
  })

  it('anything else stays', () => {
    expect(redirectedSection('calendar', { calendar: true, timetable: true })).toBeNull()
    expect(redirectedSection('calendar', { calendar: false, timetable: false })).toBeNull()
    expect(redirectedSection('calendar', undefined)).toBeNull()
    expect(redirectedSection('tasks', { calendar: false, timetable: true })).toBeNull()
  })
})

describe('sectionLabel', () => {
  it('Calendar is always "Calendar"; Timetable its own label', () => {
    expect(sectionLabel('calendar')).toBe('Calendar')
    expect(sectionLabel('timetable')).toBe('Timetable')
  })
})

describe('section paths', () => {
  it('the feed is the space root, every other section its own path', () => {
    expect(spaceSectionPath('sp-1', 'feed')).toBe('/spaces/sp-1')
    expect(spaceSectionPath('sp-1', 'chat')).toBe('/spaces/sp-1/chat')
    expect(spaceSectionPath('sp-1', 'members')).toBe('/spaces/sp-1/members')
    expect(spaceSectionPath('sp-1', 'timetable')).toBe('/spaces/sp-1/timetable')
  })

  it('parses a path segment: none → feed, unknown → null', () => {
    expect(parseSpaceSection(undefined)).toBe('feed')
    expect(parseSpaceSection('')).toBe('feed')
    expect(parseSpaceSection('calendar')).toBe('calendar')
    expect(parseSpaceSection('moderation')).toBe('moderation')
    expect(parseSpaceSection('timetable')).toBe('timetable')
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
    expect(legacySpaceSection({ tab: 'timetable' }, '')).toBe('timetable')
  })

  it('ignores anything else', () => {
    expect(legacySpaceSection({}, '')).toBeNull()
    expect(legacySpaceSection(undefined, undefined)).toBeNull()
    expect(legacySpaceSection({ tab: 'feed' }, '')).toBeNull()
    expect(legacySpaceSection({ tab: 'nope' }, '#other')).toBeNull()
  })
})
