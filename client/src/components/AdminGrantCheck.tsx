/**
 * AdminGrantCheck — what the owner reads before making another
 * household's member an admin of a space.
 *
 * An admin's household is handed the space's signing seed: it can act
 * for the space while the owner is offline. That holds for a household
 * seated from an invite link and reached only through a GFS too, so a
 * leaked admin link must not be enough — the owner checks WHO asks. The
 * block shows the asking household's ID (the fingerprint of its identity
 * key, the one its own Connections page shows) and says, calmly, what
 * approving gives.
 *
 * Shown on every surface that grants admin to a remote household: the
 * join-request review (:mod:`JoinRequestList`, an admin link's pending
 * elevation) and the member sheet's role picker (:mod:`MemberActionSheet`).
 */
import { HouseholdId } from './HouseholdId'
import { t } from '@/i18n/i18n'

export function AdminGrantCheck({ householdId }: { householdId: string }) {
  return (
    <div class="sh-admin-grant-check" role="group"
         aria-label={t('connections.detail.household_id')}
         data-testid="admin-grant-check">
      <p class="sh-admin-grant-check__warning">{t('admin_grant.warning')}</p>
      <div class="sh-admin-grant-check__id">
        <span class="sh-admin-grant-check__label">{t('connections.detail.household_id')}</span>
        <HouseholdId id={householdId} />
      </div>
      <p class="sh-admin-grant-check__hint">{t('admin_grant.check')}</p>
    </div>
  )
}
