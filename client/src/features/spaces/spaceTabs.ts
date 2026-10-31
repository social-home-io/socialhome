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
 * contract, §23.8.6) and the timetable. The space timetable is its own
 * section (``/spaces/:id/timetable``), last in the order so it usually
 * sits in More; it used to be a switch inside Calendar, so an old
 * Calendar link in a space with only a timetable goes there
 * (``redirectedSection``).
 *
 * Games (group play, v_77) follows the space's games list instead of a
 * switch: it shows while the space allows at least one game, and for the
 * people who choose the games (owner, admins, moderators — D9) also while
 * the list is empty, so they find "Choose games". Never for a follower,
 * never in a global space (no games there).
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
  | 'games'
  | 'calendar'
  | 'tasks'
  | 'pages'
  | 'gallery'
  | 'stickies'
  | 'bazaar'
  | 'map'
  | 'timetable'
  | 'members'
  | 'moderation'

/** The section bar's order. What doesn't fit goes to More, from the end. */
export const SECTION_ORDER: readonly SpaceSection[] = [
  'feed', 'chat', 'games', 'calendar', 'tasks', 'pages', 'gallery', 'stickies', 'bazaar', 'map',
  'timetable',
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
  /** The space's games list (app ids). */
  allowed_apps?: string[]
}

/** Who is looking: ``chat`` — the space chat is open to the viewer (a
 *  member, never a follower, while the space keeps chat on);
 *  ``follower`` — a read-only follower, who gets no Map (the pins and
 *  zones are the members' whereabouts; the server refuses them too);
 *  ``choosesGames`` — the owner / an admin / a moderator (D9);
 *  ``global`` — a global space (no games); ``writer`` — ``false`` while
 *  the viewer has no known writing seat (a non-member, a role not loaded
 *  yet): the games can't be read, so no Games. */
export interface SectionViewer {
  chat: boolean
  follower: boolean
  choosesGames?: boolean
  global?: boolean
  writer?: boolean
}

/** Does the Games section show? */
export function gamesSectionShown(f: SpaceTabFeatures | undefined, viewer: SectionViewer): boolean {
  if (viewer.follower || viewer.global || viewer.writer === false) return false
  return (f?.allowed_apps?.length ?? 0) > 0 || viewer.choosesGames === true
}

/** The bar's sections for this space and viewer, in ``SECTION_ORDER``. */
export function visibleSpaceSections(
  f: SpaceTabFeatures | undefined, viewer: SectionViewer,
): SpaceSection[] {
  const on: Record<string, boolean> = {
    feed: true,
    chat: viewer.chat,
    games: gamesSectionShown(f, viewer),
    calendar: f?.calendar ?? true,
    tasks: f?.todo ?? true,
    pages: f?.pages ?? true,
    gallery: f?.gallery ?? true,
    stickies: f?.stickies ?? true,
    bazaar: f?.bazaar ?? true,
    map: !!f?.location && !viewer.follower,
    timetable: !!f?.timetable,
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

/** Where a link to ``section`` should go instead, or ``null``. The
 *  timetable lived inside Calendar until it became its own section: a
 *  Calendar link in a space that keeps only a timetable means it. */
export function redirectedSection(
  section: SpaceSection, f: SpaceTabFeatures | undefined,
): SpaceSection | null {
  if (section === 'calendar' && f?.calendar === false && f?.timetable) return 'timetable'
  return null
}

/** The section's name in the UI language. Reuses the nav labels where
 *  the section is the space's own copy of a household one. */
export function sectionLabel(section: SpaceSection): string {
  switch (section) {
    case 'feed':       return t('nav.feed')
    case 'chat':       return t('space.chat.view_chat')
    case 'games':      return t('space.games.section_title')
    case 'calendar':   return t('nav.calendar')
    case 'tasks':      return t('nav.tasks')
    case 'pages':      return t('nav.pages')
    case 'gallery':    return t('nav.gallery')
    case 'stickies':   return t('nav.stickies')
    case 'bazaar':     return t('nav.bazaar')
    case 'map':        return t('space.tab.map')
    case 'timetable':  return t('nav.timetable')
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
