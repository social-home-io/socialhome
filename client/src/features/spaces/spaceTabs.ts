/**
 * A space's sections — which ones it has, in which order, and where
 * each one lives in the URL. Pure, so the gating is testable without
 * rendering the page.
 *
 * Every section has its own address: ``/spaces/:id`` is the feed,
 * ``/spaces/:id/:section`` the rest (``/spaces/:id/chat``,
 * ``/spaces/:id/calendar`` …), so Refresh and Back keep the section and
 * a notification can link straight to it.
 *
 * Per-space feature toggles (set by an admin in SpaceSettings) hide
 * their section when off. Defaults track the backend ``SpaceFeatures``
 * dataclass: every section on, except the opt-ins — location (privacy
 * contract, §23.8.6) and the timetable. The Calendar section holds the
 * events and/or the space timetable, so it shows when either is on.
 *
 * The section bar shows the sections in ONE fixed order
 * (``SECTION_ORDER``) and moves whatever doesn't fit into More — it
 * never reorders by activity. Members and Moderation are never in the
 * bar: they are rows of More (owner decision 1).
 */
import { t } from '@/i18n/i18n'

/** Every section a space page can show. */
export type SpaceSection =
  | 'feed'
  | 'chat'
  | 'calendar'
  | 'tasks'
  | 'pages'
  | 'gallery'
  | 'stickies'
  | 'bazaar'
  | 'map'
  | 'members'
  | 'moderation'

/** The section bar's order. What doesn't fit goes to More, from the end. */
export const SECTION_ORDER: readonly SpaceSection[] = [
  'feed', 'chat', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar', 'map',
]

/** Sections that live in More, never in the bar. */
const MORE_ONLY: readonly SpaceSection[] = ['members', 'moderation']

const ALL_SECTIONS: readonly SpaceSection[] = [...SECTION_ORDER, ...MORE_ONLY]

/** The toggles the section bar reads (a subset of ``SpaceFeatures``). */
export interface SpaceTabFeatures {
  pages?: boolean
  calendar?: boolean
  timetable?: boolean
  todo?: boolean
  stickies?: boolean
  gallery?: boolean
  bazaar?: boolean
  location?: boolean
}

/** Who is looking: ``chat`` — the space chat is open to the viewer (a
 *  member, never a follower, while the space keeps chat on);
 *  ``follower`` — a read-only follower, who gets no Map (the pins and
 *  zones are the members' whereabouts; the server refuses them too). */
export interface SectionViewer {
  chat: boolean
  follower: boolean
}

/** What the Calendar section shows. */
export type CalendarMode = 'events' | 'timetable'

/** The bar's sections for this space and viewer, in ``SECTION_ORDER``. */
export function visibleSpaceSections(
  f: SpaceTabFeatures | undefined, viewer: SectionViewer,
): SpaceSection[] {
  const on: Record<string, boolean> = {
    feed: true,
    chat: viewer.chat,
    calendar: calendarModes(f).length > 0,
    tasks: f?.todo ?? true,
    pages: f?.pages ?? true,
    gallery: f?.gallery ?? true,
    stickies: f?.stickies ?? true,
    bazaar: f?.bazaar ?? true,
    map: !!f?.location && !viewer.follower,
  }
  return SECTION_ORDER.filter((s) => on[s])
}

/** May this viewer open ``section`` here? Members: everyone with a
 *  seat. Moderation: content authority (owner / admin / moderator). */
export function sectionAllowed(
  section: SpaceSection,
  f: SpaceTabFeatures | undefined,
  viewer: SectionViewer & { canModerate: boolean },
): boolean {
  if (section === 'members') return true
  if (section === 'moderation') return viewer.canModerate
  return visibleSpaceSections(f, viewer).includes(section)
}

/** The Calendar section's contents, in switch order. Both → the
 *  Events | Timetable switch; one → that one alone; none → no section. */
export function calendarModes(f: SpaceTabFeatures | undefined): CalendarMode[] {
  return [
    ...((f?.calendar ?? true) ? (['events'] as const) : []),
    ...(f?.timetable ? (['timetable'] as const) : []),
  ]
}

/** "Calendar" — or "Timetable" when the section holds nothing else. */
export function calendarTabLabel(f: SpaceTabFeatures | undefined): string {
  const modes = calendarModes(f)
  return modes.length === 1 && modes[0] === 'timetable' ? t('nav.timetable') : t('nav.calendar')
}

/** The section's name in the UI language. Reuses the nav labels where
 *  the section is the space's own copy of a household one. */
export function sectionLabel(section: SpaceSection, f?: SpaceTabFeatures): string {
  switch (section) {
    case 'feed':       return t('nav.feed')
    case 'chat':       return t('space.chat.view_chat')
    case 'calendar':   return calendarTabLabel(f)
    case 'tasks':      return t('nav.tasks')
    case 'pages':      return t('nav.pages')
    case 'gallery':    return t('nav.gallery')
    case 'stickies':   return t('nav.stickies')
    case 'bazaar':     return t('nav.bazaar')
    case 'map':        return t('space.tab.map')
    case 'members':    return t('spaces.members')
    case 'moderation': return t('space.tab.moderation')
  }
}

/** The in-app path of a space section (the feed is the space's root). */
export function spaceSectionPath(spaceId: string, section: SpaceSection): string {
  return section === 'feed' ? `/spaces/${spaceId}` : `/spaces/${spaceId}/${section}`
}

/** The section a ``/spaces/:id/:section`` path segment names: the feed
 *  when there is none, ``null`` for one this page doesn't know. */
export function parseSpaceSection(segment: unknown): SpaceSection | null {
  if (segment === undefined || segment === null || segment === '') return 'feed'
  return typeof segment === 'string' && (ALL_SECTIONS as readonly string[]).includes(segment)
    ? segment as SpaceSection
    : null
}

/** The section an old-style link asks for, or ``null``. Before every
 *  section had its own path, links (notifications already stored in the
 *  bell, bookmarks) said ``?tab=tasks`` / ``?tab=moderation``,
 *  ``?view=chat`` (the space chat), or ``#join-requests``. */
export function legacySpaceSection(
  query: Record<string, string | undefined> | undefined,
  hash: string | undefined,
): SpaceSection | null {
  if (query?.view === 'chat') return 'chat'
  const tab = query?.tab
  if (tab && tab !== 'feed') {
    const parsed = parseSpaceSection(tab)
    if (parsed) return parsed
  }
  if (hash === '#join-requests') return 'moderation'
  return null
}
