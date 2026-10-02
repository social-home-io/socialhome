/**
 * Page scope — which wiki the shared page editor (``PagesView``) works on
 * and what the viewer may do there.
 *
 * The household Pages tab and a space's Pages tab render the same list /
 * viewer / editor / history; they differ in:
 *
 * - the routes: ``/api/pages`` or ``/api/spaces/{id}/pages``;
 * - edit locks and revert — household only. Space pages have neither
 *   route (they 404), so the space scope never calls them;
 * - who may write: anyone in the household; in a space, a writer seat
 *   allowed by the space's ``pages`` access level (§4.3), never in an
 *   archived space;
 * - review (§4.3 "Reviewed"): under ``moderated`` a member's new page and
 *   their edit / delete of somebody else's is held for a moderator. Such
 *   an edit can't autosave — every save would queue another item — so
 *   the editor submits it once.
 *
 * The pattern of ``features/tasks/scope.ts``.
 */
import { currentUser } from '@/store/auth'
import { householdDisplayName, loadHouseholdUsers } from '@/store/householdUsers'
import { loadSpaceMembers } from '@/store/spaceMembers'
import { t } from '@/i18n/i18n'
import { spacePeople, personName } from '@/features/tasks/scope'
import { canModerate, type SpaceRole } from '@/features/spaces/spaceRoles'
import type { SpaceAccessLevel } from '@/types'
import type { Page } from '@/types'

export interface PageScope {
  /** Remount key (per-scope editor state). */
  key: string
  /** Collection route, e.g. ``/api/pages``. Item routes append ``/{id}``. */
  base: string
  /** The space, for the review queue — ``null`` for the household. */
  spaceId: string | null
  /** Household edit locks (``/lock`` routes). */
  locks: boolean
  /** May the viewer restore an older version (``/revert``)? */
  canRevert: boolean
  /** The note under the history when restoring isn't possible. */
  revertNote: () => string
  /** May the viewer create / edit / delete pages here at all? */
  canWrite: boolean
  /** Is this edit / delete of ``page`` held for review (no autosave)? */
  reviewed: (page: Pick<Page, 'created_by'>) => boolean
  /** Is a new page held for review? */
  reviewedCreate: boolean
  /** A user's name here. */
  nameOf: (uid: string) => string
  /** Make sure ``nameOf`` can resolve. */
  loadPeople: () => void
}

export function householdPageScope(): PageScope {
  return {
    key: 'household',
    base: '/api/pages',
    spaceId: null,
    locks: true,
    canRevert: currentUser.value?.is_admin ?? false,
    revertNote: () => t('pages.history.restore_admins'),
    canWrite: true,
    reviewed: () => false,
    reviewedCreate: false,
    nameOf: uid => householdDisplayName(uid),
    loadPeople: () => { void loadHouseholdUsers() },
  }
}

export interface SpacePageScopeOpts {
  spaceId: string
  role: SpaceRole | undefined
  /** The space's ``pages_access`` level. */
  level: SpaceAccessLevel
  /** May write: a writer seat the level lets in (``canContribute``). */
  writable: boolean
  archived: boolean
}

export function spacePageScope({
  spaceId, role, level, writable, archived,
}: SpacePageScopeOpts): PageScope {
  const me = currentUser.value?.user_id
  // Under "Reviewed" a moderator / admin / owner writes directly; a member
  // only on their own pages.
  const reviewing = level === 'moderated' && !canModerate(role)
  return {
    key: `space:${spaceId}`,
    base: `/api/spaces/${spaceId}/pages`,
    spaceId,
    locks: false,
    canRevert: false,
    revertNote: () => t('pages.history.read_only_space'),
    canWrite: writable && !archived,
    reviewed: page => reviewing && page.created_by !== me,
    reviewedCreate: reviewing,
    nameOf: uid => personName(spacePeople(spaceId, { subscribers: true }), uid),
    loadPeople: () => { void loadSpaceMembers(spaceId) },
  }
}
