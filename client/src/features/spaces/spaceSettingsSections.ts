/**
 * A space's settings page — which sections it has for the viewer, in
 * which order, and where each one lives in the URL. Pure, so the gating
 * is testable without rendering the page.
 *
 * One page, one URL per section: ``/spaces/:id/settings`` opens on the
 * first section, ``/spaces/:id/settings/:section`` scrolls to that one
 * (Refresh and Back keep it; More's "Edit sections" deep-links to
 * ``…/settings/sections``). Paths are stripped (no ingress prefix); the
 * links wrap them in ``addBase``.
 *
 * Who sees what:
 *   - ``admin`` — owner / admin of a space hosted here: everything.
 *   - ``remote-admin`` — an admin of a space hosted on another household:
 *     only what forwards to the host (no zones / bots of the space / age
 *     gate — host-local).
 *   - ``member`` — member or moderator: their own notifications, "You in
 *     this space", their bots, and Leave.
 *   - ``follower`` — notifications and Unfollow, nothing else.
 */
import { t } from '@/i18n/i18n'

export type SettingsSection =
  | 'basics'
  | 'find'
  | 'members'
  | 'sections'
  | 'notifications'
  | 'location'
  | 'moderation'
  | 'bots'
  | 'danger'
  | 'you'
  | 'leave'
  | 'unfollow'

export type SettingsViewer = 'admin' | 'remote-admin' | 'member' | 'follower'

const ORDER: Record<SettingsViewer, readonly SettingsSection[]> = {
  admin: [
    'basics', 'find', 'members', 'sections', 'notifications',
    'location', 'moderation', 'bots', 'danger',
  ],
  'remote-admin': [
    'basics', 'find', 'members', 'sections', 'notifications',
    'location', 'moderation', 'bots', 'danger',
  ],
  member: ['notifications', 'you', 'bots', 'leave'],
  follower: ['notifications', 'unfollow'],
}

const ALL: readonly SettingsSection[] = [
  'basics', 'find', 'members', 'sections', 'notifications', 'location',
  'moderation', 'bots', 'danger', 'you', 'leave', 'unfollow',
]

/** The viewer's sections, in page order. */
export function settingsSections(viewer: SettingsViewer): readonly SettingsSection[] {
  return ORDER[viewer]
}

/** The section a ``/settings/:section`` segment names, or ``null`` for
 *  one this page doesn't know (no segment → ``null`` too: the page then
 *  opens on its first section). */
export function parseSettingsSection(segment: unknown): SettingsSection | null {
  return typeof segment === 'string' && (ALL as readonly string[]).includes(segment)
    ? segment as SettingsSection
    : null
}

/** The section an old-style settings link asks for, or ``null``. The
 *  "Reviewed isn't available for a household" bell entry used to link to
 *  ``/settings?household=…``; the access levels it is about live in
 *  Members & joining. */
export function legacySettingsSection(
  query: Record<string, string | undefined> | undefined,
): SettingsSection | null {
  return query?.household ? 'members' : null
}

/** The section to show: the asked one when this viewer has it, else the
 *  viewer's first. */
export function resolveSettingsSection(
  segment: unknown, viewer: SettingsViewer,
): SettingsSection {
  const asked = parseSettingsSection(segment)
  const mine = settingsSections(viewer)
  return asked && mine.includes(asked) ? asked : mine[0]
}

/** The stripped in-app path of a settings section. */
export function settingsSectionPath(spaceId: string, section: SettingsSection): string {
  return `/spaces/${spaceId}/settings/${section}`
}

/** The section's heading (and its rail / chip label). */
export function settingsSectionLabel(section: SettingsSection): string {
  switch (section) {
    case 'basics':        return t('space.settings_page.section.basics')
    case 'find':          return t('space.settings_page.section.find')
    case 'members':       return t('space.settings_page.section.members')
    case 'sections':      return t('space.settings_page.section.sections')
    case 'notifications': return t('space.settings_page.section.notifications')
    case 'location':      return t('space.settings_page.section.location')
    case 'moderation':    return t('space.settings_page.section.moderation')
    case 'bots':          return t('space.settings_page.section.bots')
    case 'danger':        return t('space.settings_page.section.danger')
    case 'you':           return t('space.settings_page.section.you')
    case 'leave':         return t('space.settings_page.section.leave')
    case 'unfollow':      return t('space.settings_page.section.unfollow')
  }
}
