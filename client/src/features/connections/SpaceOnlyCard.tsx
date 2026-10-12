/**
 * SpaceOnlyCard — a household met through an invite link (§D2b). It is a
 * peer for the shared space only: no DMs, profile sync or presence, and
 * nothing to manage. The client has no space names, so the subtitle is
 * always "Shares a space with you".
 */
import type { Connection } from '@/store/connections'
import { t } from '@/i18n/i18n'
import { ConnAvatar } from './ConnAvatar'
import { ConnCard } from './ConnCard'
import { ConnChip } from './ConnChip'
import { ConnIcon } from './ConnIcon'
import { initialOf, tintFor } from './model'

export function SpaceOnlyCard({ conn }: { conn: Connection }) {
  return (
    <ConnCard
      avatar={<ConnAvatar letter={initialOf(conn.display_name)} tint={tintFor(conn.instance_id)} />}
      name={conn.display_name}
      chips={<ConnChip tone="muted">{t('connections.type.space_only')}</ConnChip>}
      meta={[<span data-testid={`space-only-note-${conn.instance_id}`}><ConnIcon name="spaces" />{t('connections.meta.shares_space')}</span>]}
      mods={['dashed']}
    />
  )
}
