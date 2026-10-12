/**
 * HouseholdCard — a confirmed household. Admins get a button that opens
 * Manage; members get a static row. The version chip, honey ring and ↑
 * badge are admin-only (``h.behind`` is always 0 for members).
 */
import type { CompatPeer } from '@/store/federationCompat'
import { featureLabels } from '@/utils/capabilityLabels'
import { ConnAvatar } from './ConnAvatar'
import { ConnCard, cardDomId } from './ConnCard'
import { ConnChip } from './ConnChip'
import { behindText, householdAriaLabel, householdMeta } from './connMeta'
import { initialOf, tintFor, type Household } from './model'

interface Props {
  household: Household
  compat?: CompatPeer
  onManage?: () => void
}

export function HouseholdCard({ household: h, compat, onManage }: Props) {
  const c = h.conn
  const mods = h.status === 'down' ? ['down'] : h.status === 'warn' ? ['warn'] : []
  return (
    <ConnCard
      id={cardDomId(c.instance_id)}
      avatar={<ConnAvatar letter={initialOf(c.display_name)} tint={tintFor(c.instance_id)} status={h.status} />}
      name={c.display_name}
      chips={h.status === 'warn' && (
        <ConnChip
          tone="warn"
          title={compat ? featureLabels(compat.lacking_features, compat.lacking_feature_keys).join(', ') : undefined}
        >
          {behindText(h.behind)}
        </ConnChip>
      )}
      meta={householdMeta(h)}
      mods={mods}
      onOpen={onManage}
      ariaLabel={householdAriaLabel(h)}
    />
  )
}
