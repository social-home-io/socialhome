/**
 * Space roles in the SPA — the mirror of ``domain/space.py``
 * (``SpaceRole``, ``SETTINGS_AUTHORITY_ROLES``, ``CONTENT_AUTHORITY_ROLES``,
 * ``WRITER_ROLES``, ``role_change_allowed``). These only decide what the
 * UI offers; the server re-checks every action.
 *
 * Order: owner > admin > moderator > member > subscriber. A moderator
 * holds content authority (the moderation queue, others' posts) and no
 * settings authority at all.
 */

export type SpaceRole = 'owner' | 'admin' | 'moderator' | 'member' | 'subscriber'

const ROLES: readonly SpaceRole[] = ['owner', 'admin', 'moderator', 'member', 'subscriber']

/** A server role string as a known :type:`SpaceRole`, else ``undefined``. */
export function parseSpaceRole(raw: unknown): SpaceRole | undefined {
  return typeof raw === 'string' && (ROLES as readonly string[]).includes(raw)
    ? raw as SpaceRole
    : undefined
}

/** Settings authority: owner / admin. */
export function hasSettingsAuthority(role: SpaceRole | undefined): boolean {
  return role === 'owner' || role === 'admin'
}

/** Content authority: owner / admin / moderator — works the queue. */
export function canModerate(role: SpaceRole | undefined): boolean {
  return hasSettingsAuthority(role) || role === 'moderator'
}

/** May create content: everyone but a read-only subscriber. */
export function isWriterRole(role: SpaceRole | undefined): boolean {
  return canModerate(role) || role === 'member'
}

/** The roles ``actor`` may move a ``target`` seat to (current one
 *  excluded), in display order. The owner: admin / moderator / member on
 *  any non-owner seat. An admin: member ↔ moderator only. */
export function roleChangeOptions(
  actor: SpaceRole | undefined, target: SpaceRole | undefined,
): SpaceRole[] {
  if (target === undefined || target === 'owner') return []
  let options: SpaceRole[] = []
  if (actor === 'owner') {
    options = ['admin', 'moderator', 'member']
  } else if (actor === 'admin' && (target === 'member' || target === 'moderator')) {
    options = ['moderator', 'member']
  }
  return options.filter(r => r !== target)
}
