/**
 * GfsSection — the household's GFS connections: one static row per GFS
 * (cloud avatar, live state, mono host, admin "⋯" menu with Copy address
 * and Disconnect), the GfsEmpty explainer, or a LoadError.
 */
import { t } from '@/i18n/i18n'
import type { GfsConnection } from '@/types'
import { ConnAvatar } from './ConnAvatar'
import { ConnCard } from './ConnCard'
import { ConnIcon } from './ConnIcon'
import { LoadError } from './LoadError'
import { OverflowMenu } from './OverflowMenu'
import { SectionHeader } from './SectionHeader'
import { SkeletonCards } from './ConnSkeleton'
import { gfsLiveState, hostOf } from './model'

export function GfsCard({ gfs, isAdmin, onCopy, onDisconnect }: {
  gfs: GfsConnection
  isAdmin: boolean
  onCopy: () => void
  onDisconnect: () => void
}) {
  const live = gfsLiveState(gfs)
  return (
    <ConnCard
      avatar={<ConnAvatar icon="cloud" tint="honey" status={live.status} />}
      name={gfs.display_name}
      meta={[
        <span data-gfs-state={live.status}>{t(live.labelKey)}</span>,
        <span class="sh-conn-mono" title={gfs.inbox_url}>{hostOf(gfs.inbox_url)}</span>,
      ]}
      trailing={isAdmin && (
        <OverflowMenu
          label={t('connections.gfs.more', { name: gfs.display_name })}
          actions={[
            { label: t('connections.gfs.copy'), icon: 'copy', onSelect: onCopy },
            { label: t('gfs.disconnect'), icon: 'off', danger: true, onSelect: onDisconnect },
          ]}
        />
      )}
    />
  )
}

export function GfsEmpty({ onAdd }: { onAdd?: () => void }) {
  return (
    <div class="sh-conn-empty">
      <span class="sh-conn-empty__art" aria-hidden="true"><ConnIcon name="cloud" /></span>
      <div>
        <h3>{t('connections.gfs.empty_title')}</h3>
        <p>{t('connections.gfs.empty_body')}</p>
      </div>
      {onAdd && (
        <div class="sh-conn-empty__cta">
          <button type="button" class="sh-btn sh-btn--secondary" onClick={onAdd}>
            <ConnIcon name="plus" />
            {t('connections.gfs.empty_add')}
          </button>
        </div>
      )}
    </div>
  )
}

interface Props {
  gfs: GfsConnection[]
  loading: boolean
  error: boolean
  isAdmin: boolean
  onAdd: () => void
  onRetry: () => void
  onCopy: (g: GfsConnection) => void
  onDisconnect: (g: GfsConnection) => void
}

export function GfsSection({ gfs, loading, error, isAdmin, onAdd, onRetry, onCopy, onDisconnect }: Props) {
  const showList = !loading && !error && gfs.length > 0
  return (
    <section class="sh-conn-sect" id="sh-conn-gfs" aria-labelledby="sh-conn-gfs-title" aria-busy={loading || undefined}>
      <SectionHeader
        id="sh-conn-gfs-title"
        title={t('connections.gfs.title')}
        count={showList ? gfs.length : undefined}
        right={isAdmin && showList && (
          <button type="button" class="sh-btn sh-btn--secondary" onClick={onAdd}>
            <ConnIcon name="plus" />
            {t('gfs.add')}
          </button>
        )}
      />
      {loading ? (
        <SkeletonCards widths={[['34%', '40%']]} />
      ) : error ? (
        <LoadError
          title={t('connections.error.gfs_title')}
          body={t('connections.error.body')}
          onRetry={onRetry}
        />
      ) : gfs.length === 0 ? (
        <GfsEmpty onAdd={isAdmin ? onAdd : undefined} />
      ) : (
        <ul class="sh-conn-list">
          {gfs.map((g) => (
            <li key={g.id}>
              <GfsCard gfs={g} isAdmin={isAdmin} onCopy={() => onCopy(g)} onDisconnect={() => onDisconnect(g)} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
