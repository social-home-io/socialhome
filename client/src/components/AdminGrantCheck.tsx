/**
 * AdminGrantCheck — what the owner reads before making another
 * household's member an admin of a space.
 *
 * With "Let admins act for me" on (``features.delegated_admin_authority``)
 * an admin's household is handed the space's signing seed: it can act
 * for the space while the owner is offline. That holds for a household
 * seated from an invite link and reached only through a GFS too, so a
 * leaked admin link must not be enough — the owner checks WHO asks. The
 * block shows the asking household's ID (the fingerprint of its identity
 * key, the one its own Connections page shows — the one thing on the row
 * the other side cannot choose) and, with the switch on, says calmly
 * what approving gives. The ID shows with the switch off too: turning it
 * on later reaches the admins made before.
 *
 * Shown on every surface that grants admin to a remote household: the
 * join-request review (:mod:`JoinRequestList`, an admin link's pending
 * elevation) and the member sheet's role picker (:mod:`MemberActionSheet`).
 */
import { useId } from 'preact/hooks'
import { HouseholdId } from './HouseholdId'
import { t } from '@/i18n/i18n'

export function AdminGrantCheck({ householdId, actsForSpace }: {
  householdId: string
  /** The space lets its admins act for the owner — the grant hands this
   *  household the signing seed. */
  actsForSpace: boolean
}) {
  const labelId = useId()
  return (
    <div class="sh-admin-grant-check" role="group" aria-labelledby={labelId}
         data-testid="admin-grant-check">
      {actsForSpace && (
        <p class="sh-admin-grant-check__warning">{t('admin_grant.warning')}</p>
      )}
      <div class="sh-admin-grant-check__id">
        <span class="sh-admin-grant-check__label" id={labelId}>
          {t('connections.detail.household_id')}
        </span>
        <HouseholdId id={householdId} />
      </div>
      <p class="sh-admin-grant-check__hint">{t('admin_grant.check')}</p>
    </div>
  )
}
