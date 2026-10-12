/**
 * ConnMeta — the second line of a household card, and the card's spoken
 * status. Kept apart from the card shell so the wording is testable.
 *
 *   Direct · Last connected 2 min ago
 *   Over the internet · Unreachable since Mon · 12 messages waiting
 *
 * On phones the line stacks and "12 messages waiting" shortens to
 * "12 waiting" (both rendered, swapped by CSS); the full text stays in
 * the card's ``aria-label``.
 */
import type { ComponentChildren } from 'preact'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import type { TransportState } from '@/store/connections'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ConnIcon, type ConnIconName } from './ConnIcon'
import { formatSince, type Household } from './model'

const TRANSPORT: Record<Exclude<TransportState, null>, { icon: ConnIconName; key: string }> = {
  rtc: { icon: 'bolt', key: 'connections.transport.direct' },
  https: { icon: 'globe', key: 'connections.transport.internet' },
  gfs_relay: { icon: 'cloud', key: 'connections.transport.gfs' },
}

export function transportItem(transport: TransportState | undefined): ComponentChildren {
  if (!transport) return null
  const tx = TRANSPORT[transport]
  return (
    <span data-transport={transport}>
      <ConnIcon name={tx.icon} />
      {t(tx.key)}
    </span>
  )
}

function waitingText(n: number): string {
  return t(isOne(n) ? 'connections.meta.waiting_one' : 'connections.meta.waiting', { n: String(n) })
}

export function behindText(n: number): string {
  return t(isOne(n) ? 'connections.card.behind_one' : 'connections.card.behind', { n: String(n) })
}

/** Meta items for a confirmed household. */
export function householdMeta(h: Household, now: number = Date.now()): ComponentChildren[] {
  const c = h.conn
  const items: ComponentChildren[] = [transportItem(c.transport)]
  if (h.status === 'down') {
    const since = formatSince(c.unreachable_since, 'short', formatLocale(), now)
    const queued = c.queued_envelopes ?? 0
    items.push(
      <span class="sh-conn-down">
        {since
          ? t('connections.meta.unreachable_since', { when: since })
          : t('connections.meta.unreachable')}
        {queued > 0 && (
          <>
            {' · '}
            <span class="sh-conn-long">{waitingText(queued)}</span>
            <span class="sh-conn-short">
              {t('connections.meta.waiting_short', { n: String(queued) })}
            </span>
          </>
        )}
      </span>,
    )
  } else if (c.last_reachable_at) {
    items.push(
      <span>{t('connections.meta.last_connected', { ago: relativeDocsTime(c.last_reachable_at) })}</span>,
    )
  }
  return items.filter(Boolean)
}

/** Accessible name of a household card: "Epsilon House, unreachable
 *  since Monday, 12 messages waiting". */
export function householdAriaLabel(h: Household, now: number = Date.now()): string {
  const c = h.conn
  const parts = [c.display_name]
  if (h.status === 'down') {
    const since = formatSince(c.unreachable_since, 'long', formatLocale(), now)
    parts.push(since
      ? t('connections.card.status_unreachable_since', { when: since })
      : t('connections.card.status_unreachable'))
    const queued = c.queued_envelopes ?? 0
    if (queued > 0) parts.push(waitingText(queued))
  } else {
    parts.push(t('connections.card.status_connected'))
    if (h.status === 'warn') parts.push(behindText(h.behind))
  }
  return parts.join(', ')
}
