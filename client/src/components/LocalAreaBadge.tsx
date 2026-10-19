/**
 * LocalAreaBadge — "In your area" / "Outside your area" / "Near the edge"
 * for a local community THIS household hosts, worked out in the browser
 * against our own home with the server's own rule
 * (``utils/localArea.areaVerdict``). Says nothing while the home is still
 * loading. A paired household's community has no badge: its host decides
 * on the ~1 km location a join carries.
 */
import { t } from '@/i18n/i18n'
import type { AreaVerdict } from '@/utils/localArea'

export function localAreaVerdictText(verdict: AreaVerdict): string | null {
  switch (verdict) {
    case 'inside': return t('local_area.badge.inside')
    case 'outside': return t('local_area.badge.outside')
    case 'edge': return t('local_area.badge.edge')
    case 'no_home': return t('local_area.badge.no_home')
    case 'unknown': return null
  }
}

/** May the person try to join from here? From inside, and near the edge
 *  (it becomes a request an admin reviews) — every other answer would be
 *  refused. */
export function verdictAllowsJoin(verdict: AreaVerdict): boolean {
  return verdict === 'inside' || verdict === 'edge' || verdict === 'unknown'
}

export function LocalAreaBadge({ verdict }: { verdict: AreaVerdict }) {
  const text = localAreaVerdictText(verdict)
  if (!text) return null
  const icon = verdict === 'inside' ? '✓' : verdict === 'outside' ? '✕' : verdict === 'edge' ? '◐' : '🏠'
  return (
    <span class={`sh-local-area-badge sh-local-area-badge--${verdict}`} data-testid="local-area-badge">
      <span aria-hidden="true">{icon}</span> {text}
    </span>
  )
}
