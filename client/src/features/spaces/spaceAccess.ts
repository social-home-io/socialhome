/**
 * Space feature access levels (§4.3) in the SPA — the mirror of
 * ``SpaceFeatures.*_access`` / ``SpaceFeatureAccess`` server-side.
 *
 * Each collaborative feature (posts, pages, tasks, stickies, calendar)
 * has one level: ``open`` (every member), ``moderated`` (members' new items, and their
 * changes to other people's, wait for review), ``admin_only`` (owners and
 * admins; moderators and members read). These helpers only decide what
 * the UI offers and says; the server re-checks every write and answers
 * ``403 ACCESS_ADMIN_ONLY`` when it refuses one.
 */
import { t } from '@/i18n/i18n'
import type { SpaceAccessLevel, SpaceFeatures } from '@/types'
import { hasSettingsAuthority, isWriterRole, type SpaceRole } from './spaceRoles'

export type AccessFeature = 'posts' | 'pages' | 'tasks' | 'stickies' | 'calendar'

/** In the settings' display order. */
export const ACCESS_FEATURES: readonly AccessFeature[] = [
  'posts', 'pages', 'tasks', 'stickies', 'calendar',
]

const LEVELS: readonly SpaceAccessLevel[] = ['open', 'moderated', 'admin_only']

/** The level of ``feature``. Absent (an older host / a fresh stub) is
 *  ``open``; a value this build doesn't know is treated as ``admin_only``
 *  so the UI never offers a write the host will refuse. */
export function accessLevel(
  features: Partial<SpaceFeatures> | undefined,
  feature: AccessFeature,
): SpaceAccessLevel {
  const raw = (features as Record<string, unknown> | undefined)?.[`${feature}_access`]
  if (raw === undefined || raw === null) return 'open'
  return (LEVELS as readonly unknown[]).includes(raw) ? raw as SpaceAccessLevel : 'admin_only'
}

/** The levels the settings offer — every feature can be Reviewed
 *  (``moderated``), in spaces shared with other households too (federated
 *  moderation, v_43; a household too old for it shows up in the
 *  ``409 PEERS_TOO_OLD`` prompt when Reviewed is picked). */
export function levelOptions(): SpaceAccessLevel[] {
  return [...LEVELS]
}

/** Is ``role`` a writer the ADMIN_ONLY ``level`` alone keeps out — a
 *  member or moderator (a subscriber reads everywhere anyway)? The case a
 *  "only admins can …" note is for. */
export function blockedByAdminOnly(
  level: SpaceAccessLevel,
  role: SpaceRole | undefined,
): boolean {
  return level === 'admin_only' && isWriterRole(role) && !hasSettingsAuthority(role)
}

/** The short "only admins can …" note for a read-only viewer — also the
 *  text of the toast a ``403 ACCESS_ADMIN_ONLY`` turns into. */
export function accessNote(feature: string): string {
  switch (feature) {
    case 'posts': return t('space.access.note.posts')
    case 'pages': return t('space.access.note.pages')
    case 'tasks': return t('space.access.note.tasks')
    case 'stickies': return t('space.access.note.stickies')
    case 'calendar': return t('space.access.note.calendar')
    default: return t('space.access.note.generic')
  }
}

/** The label of a level in the settings select. */
export function levelLabel(level: SpaceAccessLevel): string {
  switch (level) {
    case 'open': return t('space.access.level.open')
    case 'moderated': return t('space.access.level.moderated')
    default: return t('space.access.level.admin_only')
  }
}

/** The label of a feature in the settings. */
export function featureLabel(feature: AccessFeature): string {
  switch (feature) {
    case 'posts': return t('space.access.feature.posts')
    case 'pages': return t('space.access.feature.pages')
    case 'tasks': return t('space.access.feature.tasks')
    case 'stickies': return t('space.access.feature.stickies')
    default: return t('space.access.feature.calendar')
  }
}

export interface BehindHousehold {
  instance_id: string
  display_name: string
  proto_version: number
}

/** The households a ``409 PEERS_TOO_OLD`` names — ``null`` for any other
 *  error. Duck-typed on ``code`` / ``extra`` (an ``ApiError``). */
export function peersTooOldHouseholds(err: unknown): BehindHousehold[] | null {
  const e = err as { code?: unknown; extra?: { households?: unknown } } | null
  if (!e || e.code !== 'PEERS_TOO_OLD') return null
  const raw = Array.isArray(e.extra?.households) ? e.extra.households : []
  return raw.flatMap((h): BehindHousehold[] => {
    if (!h || typeof h !== 'object') return []
    const r = h as Record<string, unknown>
    const id = typeof r.instance_id === 'string' ? r.instance_id : ''
    if (!id) return []
    return [{
      instance_id: id,
      display_name: typeof r.display_name === 'string' && r.display_name ? r.display_name : id,
      proto_version: typeof r.proto_version === 'number' ? r.proto_version : 0,
    }]
  })
}

/** The toast for a space event whose feed announcement was dropped
 *  (``announce_suppressed`` on the create response) — ``null`` when it
 *  was announced or never asked to be. */
export function announceSuppressedMessage(res: unknown): string | null {
  const r = res as { announce_suppressed?: unknown; announce_suppressed_reason?: unknown } | null
  if (!r || r.announce_suppressed !== true) return null
  return r.announce_suppressed_reason === 'moderated'
    ? t('event.announce_suppressed.moderated')
    : t('event.announce_suppressed.admin_only')
}
