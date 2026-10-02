/**
 * Which tabs a space shows — pure, so the gating is testable without
 * rendering the page.
 *
 * Per-space feature toggles (set by an admin in SpaceSettings) hide
 * their tab when off. Feed + members stay visible always — they anchor
 * the page. Defaults track the backend ``SpaceFeatures`` dataclass:
 * every tab on, except the opt-ins — location (privacy contract,
 * §23.8.6) and the timetable. The Calendar tab holds the events and/or
 * the space timetable, so it shows when either is on.
 */
import { t } from '@/i18n/i18n'
import type { SpaceTab } from '@/components/SpaceSubHeader'

/** The toggles the tab strip reads (a subset of ``SpaceFeatures``). */
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

/** What the Calendar tab shows. */
export type CalendarMode = 'events' | 'timetable'

/** ``canModerate`` — the viewer holds content authority (owner / admin /
 *  moderator) and so works the Moderation tab. */
export function visibleSpaceTabs(
  f: SpaceTabFeatures | undefined, canModerate: boolean,
): SpaceTab[] {
  return [
    'feed', 'members',
    ...((f?.pages ?? true) ? (['pages'] as const) : []),
    ...(calendarModes(f).length > 0 ? (['calendar'] as const) : []),
    ...((f?.todo ?? true) ? (['tasks'] as const) : []),
    ...((f?.stickies ?? true) ? (['stickies'] as const) : []),
    ...((f?.gallery ?? true) ? (['gallery'] as const) : []),
    ...((f?.bazaar ?? true) ? (['bazaar'] as const) : []),
    ...(f?.location ? (['map'] as const) : []),
    ...(canModerate ? (['moderation'] as const) : []),
  ]
}

/** The Calendar tab's contents, in switch order. Both → the
 *  Events | Timetable switch; one → that one alone; none → no tab. */
export function calendarModes(f: SpaceTabFeatures | undefined): CalendarMode[] {
  return [
    ...((f?.calendar ?? true) ? (['events'] as const) : []),
    ...(f?.timetable ? (['timetable'] as const) : []),
  ]
}

/** "Calendar" — or "Timetable" when the tab holds nothing else. */
export function calendarTabLabel(f: SpaceTabFeatures | undefined): string {
  const modes = calendarModes(f)
  return modes.length === 1 && modes[0] === 'timetable' ? t('nav.timetable') : t('nav.calendar')
}

/** Tabs a ``?tab=`` deep link may open (moderation needs a role that
 *  loads later, so a link never lands there). */
const LINKABLE_TABS: readonly SpaceTab[] = [
  'feed', 'members', 'pages', 'calendar', 'tasks', 'stickies', 'gallery', 'bazaar', 'map',
]

/** The tab a ``?tab=`` query value names, or ``null`` (unknown / none). */
export function parseSpaceTab(v: unknown): SpaceTab | null {
  return typeof v === 'string' && (LINKABLE_TABS as readonly string[]).includes(v)
    ? v as SpaceTab
    : null
}
