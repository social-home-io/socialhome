/**
 * SpacePagesTab — a space's wiki: the shared {@link PagesView} on the
 * space scope (``/api/spaces/{id}/pages``; no edit locks, read-only
 * history, the ``pages`` access level of §4.3).
 *
 * A member or moderator in an ADMIN_ONLY space reads with the "only
 * admins …" note; under "Reviewed" a member's new page and their edits of
 * other people's pages go to the moderation queue (``contentWrite`` — the
 * toast, and the pending strip above the list).
 */
import { PendingReviewStrip } from '@/components/PendingReviewStrip'
import type { SpaceAccessLevel } from '@/types'
import { PagesView } from '@/features/pages/PagesView'
import { spacePageScope } from '@/features/pages/scope'
import { AccessNote } from './AccessNote'
import type { SpaceRole } from './spaceRoles'

export interface SpacePagesTabProps {
  spaceId: string
  role: SpaceRole | undefined
  level: SpaceAccessLevel
  /** A writer seat the ``pages`` level lets in; ``false`` until the
   *  member list answered. */
  writable: boolean
  /** Show the "only admins can …" note. */
  adminOnly: boolean
  archived: boolean
  /** The space's host household (v_48: it sequences the pages). */
  hostInstanceId?: string | null
}

export function SpacePagesTab({
  spaceId, role, level, writable, adminOnly, archived, hostInstanceId,
}: SpacePagesTabProps) {
  const scope = spacePageScope({
    spaceId, role, level, writable, archived, hostInstanceId,
  })
  return (
    <div class="sh-space-pages">
      <PagesView
        key={scope.key}
        scope={scope}
        header={(
          <>
            {adminOnly && <AccessNote feature="pages" />}
            <PendingReviewStrip spaceId={spaceId} feature="pages" />
          </>
        )}
      />
    </div>
  )
}
