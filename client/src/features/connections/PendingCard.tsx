/**
 * PendingCard — a pairing still waiting on the other household (admin
 * only). Ghost avatar + pending badge, a meta line that says what it waits
 * on (``pending_kind``), a tertiary "Cancel" (confirmed in a dialog by the
 * page) and — for a pairing code only — a secondary "Start again" (a fresh
 * code).
 */
import type { Connection } from '@/store/connections'
import { t } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ConnAvatar } from './ConnAvatar'
import { ConnCard } from './ConnCard'
import { ConnIcon, type ConnIconName } from './ConnIcon'
import { initialOf, pendingKind, type PendingKind } from './model'

const META: Record<PendingKind, { icon: ConnIconName; key: string }> = {
  code: { icon: 'qr', key: 'connections.meta.waiting_code' },
  request: { icon: 'users', key: 'connections.meta.waiting_request' },
  approval: { icon: 'users', key: 'connections.meta.waiting_approval' },
}

interface Props {
  conn: Connection
  onCancel: () => void
  onRestart: () => void
}

export function PendingCard({ conn, onCancel, onRestart }: Props) {
  const kind = pendingKind(conn)
  const meta = META[kind]
  return (
    <ConnCard
      avatar={<ConnAvatar letter={initialOf(conn.display_name)} tint="ghost" status="pending" />}
      name={conn.display_name}
      meta={[
        <span data-pending-kind={kind}><ConnIcon name={meta.icon} />{t(meta.key, { name: conn.display_name })}</span>,
        conn.paired_at ? <span>{relativeDocsTime(conn.paired_at)}</span> : null,
      ]}
      mods={['dashed', 'action']}
      trailing={(
        <span class="sh-conn-card__actions">
          <button
            type="button"
            class="sh-btn sh-conn-btn-text"
            aria-label={t('connections.pending.cancel_label', { name: conn.display_name })}
            onClick={onCancel}
          >
            {t('connections.pending.cancel')}
          </button>
          {kind === 'code' && (
            <button type="button" class="sh-btn sh-btn--secondary" onClick={onRestart}>
              {t('connections.pending.restart')}
            </button>
          )}
        </span>
      )}
    />
  )
}
