/**
 * HouseholdsSection — section C: header (h2 + count + List/Map toggle),
 * then the household cards, the "waiting for the other household" and
 * "met through a shared space" subgroups — or the map, the skeleton, the
 * LoadError, or (no households yet) PairingSteps.
 */
import { lazy, Suspense, useMemo } from 'preact/compat'
import { t } from '@/i18n/i18n'
import type { Connection } from '@/store/connections'
import type { CompatPeer } from '@/store/federationCompat'
import { SkeletonCards } from './ConnSkeleton'
import { HouseholdCard } from './HouseholdCard'
import { LoadError } from './LoadError'
import { MapFoot } from './MapFoot'
import { PairingSteps } from './PairingSteps'
import { PendingCard } from './PendingCard'
import { SectionHeader, SubgroupDivider } from './SectionHeader'
import { SpaceOnlyCard } from './SpaceOnlyCard'
import { ViewToggle, type ConnView } from './ViewToggle'
import { isHousehold, type Household } from './model'
import { defaultPinStatus } from './MapPin'
import type { AvatarStatus } from './ConnAvatar'

const FederationMap = lazy(() => import('./FederationMap'))

interface Props {
  view: ConnView
  onView: (v: ConnView) => void
  loading: boolean
  error: boolean
  isAdmin: boolean
  households: Household[]
  /** Admin only — members never get pending rows. */
  pending: Connection[]
  spaceOnly: Connection[]
  compat: Map<string, CompatPeer>
  onManage?: (c: Connection) => void
  onCancelPending: (c: Connection) => void
  onRestartPending: () => void
  onRetry: () => void
}

export function HouseholdsSection(p: Props) {
  // Pin status = the card's status (incl. the admin-only "needs an update").
  // Keyed on the statuses so the map only rebuilds its pins when one changes.
  const statusKey = p.households.map((h) => `${h.conn.instance_id}:${h.status}`).join('|')
  const statusOf = useMemo(() => {
    const byId = new Map(p.households.map((h) => [h.conn.instance_id, h.status]))
    return (c: Connection): AvatarStatus | undefined => byId.get(c.instance_id) ?? defaultPinStatus(c)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusKey])
  const header = (
    <SectionHeader
      id="sh-conn-hh-title"
      focusable
      title={t('connections.households')}
      count={p.loading || p.error ? undefined : p.households.length}
      right={<ViewToggle value={p.view} onChange={p.onView} />}
    />
  )
  const wrap = (children: preact.ComponentChildren, busy = false) => (
    <section class="sh-conn-sect" aria-labelledby="sh-conn-hh-title" aria-busy={busy || undefined}>
      {header}
      {children}
    </section>
  )

  if (p.error) {
    return wrap(
      <LoadError
        title={t('connections.error.title')}
        body={t('connections.error.body')}
        onRetry={p.onRetry}
      />,
    )
  }
  if (p.loading) return wrap(<SkeletonCards widths={[['38%', '62%'], ['30%', '54%'], ['44%', '48%']]} />, true)

  const subgroups = (
    <>
      {p.pending.length > 0 && (
        <>
          <SubgroupDivider>{t('connections.sub.waiting')}</SubgroupDivider>
          <ul class="sh-conn-list">
            {p.pending.map((c) => (
              <li key={c.instance_id}>
                <PendingCard conn={c} onCancel={() => p.onCancelPending(c)} onRestart={p.onRestartPending} />
              </li>
            ))}
          </ul>
        </>
      )}
      {p.spaceOnly.length > 0 && (
        <>
          <SubgroupDivider>{t('connections.sub.space')}</SubgroupDivider>
          <ul class="sh-conn-list">
            {p.spaceOnly.map((c) => (
              <li key={c.instance_id}><SpaceOnlyCard conn={c} /></li>
            ))}
          </ul>
        </>
      )}
    </>
  )

  if (p.households.length === 0) {
    return (
      <section class="sh-conn-sect" aria-labelledby="sh-conn-steps">
        <PairingSteps />
        {subgroups}
      </section>
    )
  }

  if (p.view === 'map') {
    const rows: Connection[] = [...p.households.map((h) => h.conn), ...p.pending, ...p.spaceOnly]
    const hidden = rows.filter((c) => c.home_lat == null || c.home_lon == null).map((c) => c.display_name)
    return wrap(
      <>
        <div class="sh-conn-map">
          <Suspense fallback={<div class="sh-federation-map__loading">{t('connections.loading_map')}</div>}>
            <FederationMap
              onManage={p.onManage}
              canManage={isHousehold}
              showOffMapFooter={false}
              statusOf={statusOf}
              rows={rows}
            />
          </Suspense>
        </div>
        <MapFoot isAdmin={p.isAdmin} hidden={hidden} onShowList={() => p.onView('list')} />
      </>,
    )
  }

  return wrap(
    <>
      <ul class="sh-conn-list">
        {p.households.map((h) => (
          <li key={h.conn.instance_id}>
            <HouseholdCard
              household={h}
              compat={p.compat.get(h.conn.instance_id)}
              onManage={p.onManage ? () => p.onManage!(h.conn) : undefined}
            />
          </li>
        ))}
      </ul>
      {subgroups}
    </>,
  )
}
