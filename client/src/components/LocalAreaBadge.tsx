/**
 * LocalAreaBadge — "In your area" / "Outside your area" / "Share your
 * home …" for a local community, worked out in the browser against this
 * household's own home (``utils/localArea.areaVerdict``). Says nothing
 * while the home is still loading. It is a hint: the space's host decides.
 */
import { t } from '@/i18n/i18n'
import type { AreaVerdict } from '@/utils/localArea'

export function localAreaVerdictText(verdict: AreaVerdict, hostName?: string | null): string | null {
  switch (verdict) {
    case 'inside': return t('local_area.badge.inside')
    case 'outside': return t('local_area.badge.outside')
    case 'edge': return t('local_area.badge.edge')
    case 'no_home': return t('local_area.badge.no_home')
    case 'not_shared': return hostName
      ? t('local_area.badge.not_shared', { host: hostName })
      : t('local_area.badge.not_shared_generic')
    case 'unknown': return null
  }
}

/** May the person try to join from here? From inside, and near the edge
 *  (the host decides on the exact distance) — every other answer would be
 *  refused by the host. */
export function verdictAllowsJoin(verdict: AreaVerdict): boolean {
  return verdict === 'inside' || verdict === 'edge' || verdict === 'unknown'
}

export function LocalAreaBadge({ verdict, hostName }: {
  verdict: AreaVerdict
  hostName?: string | null
}) {
  const text = localAreaVerdictText(verdict, hostName)
  if (!text) return null
  const icon = verdict === 'inside' ? '✓' : verdict === 'outside' ? '✕' : verdict === 'edge' ? '◐' : '🏠'
  return (
    <span class={`sh-local-area-badge sh-local-area-badge--${verdict}`} data-testid="local-area-badge">
      <span aria-hidden="true">{icon}</span> {text}
    </span>
  )
}
