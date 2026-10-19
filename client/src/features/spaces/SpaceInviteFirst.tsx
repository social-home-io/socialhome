/**
 * SpaceInviteFirst — "Invite your first member", on the Feed of a space
 * where its owner / admin is still alone. One button, the space's one
 * Invite door (``RemoteInviteDialog``: people from the household and
 * paired households, or an invite link for anyone else).
 */
import { t } from '@/i18n/i18n'
import { Button } from '@/components/Button'
import { openRemoteInviteDialog } from '@/components/RemoteInviteDialog'
import type { SpaceRole } from './spaceRoles'

export function SpaceInviteFirst({ spaceId, role }: { spaceId: string; role: SpaceRole | undefined }) {
  return (
    <section class="sh-space-invite-first" aria-labelledby={`invite-first-${spaceId}`}>
      <h2 id={`invite-first-${spaceId}`}>{t('space.invite_first.title')}</h2>
      <p>{t('space.invite_first.body')}</p>
      <Button variant="secondary" onClick={() => openRemoteInviteDialog(spaceId, role)}>
        {t('space.invite_first.action')}
      </Button>
    </section>
  )
}
