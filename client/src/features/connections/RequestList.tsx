/**
 * RequestList — the admin's pending pair requests (a paired household
 * introduced a new one). The only loud element on the page; always sits
 * above the hero. Pair approves right away (the introducer's vouch
 * replaces the QR scan); Decline asks first.
 */
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ConnAvatar } from './ConnAvatar'
import { initialOf } from './model'
import { fillNodes } from './richText'
import type { AutoPairRequest } from './connectionsData'

interface Props {
  requests: AutoPairRequest[]
  onApprove: (r: AutoPairRequest) => void
  onDecline: (r: AutoPairRequest) => void
}

export function RequestCard({ request: r, onApprove, onDecline }: {
  request: AutoPairRequest
  onApprove: () => void
  onDecline: () => void
}) {
  return (
    <div class="sh-conn-request">
      <span class="sh-conn-intro" aria-hidden="true">
        <ConnAvatar letter={initialOf(r.from_a_display)} tint="paper" />
        <span class="sh-conn-intro__by">{initialOf(r.via_b_display)}</span>
      </span>
      <div class="sh-conn-request__who">
        <div class="sh-conn-request__title">
          {fillNodes(t('connections.requests.wants_to_pair'), { name: <strong>{r.from_a_display}</strong> })}
        </div>
        <div class="sh-conn-request__meta">
          {fillNodes(t('connections.requests.introduced_by'), { name: <strong>{r.via_b_display}</strong> })}
          {' · '}
          <time dateTime={r.received_at} title={new Date(r.received_at).toLocaleString(formatLocale())}>
            {relativeDocsTime(r.received_at)}
          </time>
        </div>
      </div>
      <div class="sh-conn-request__actions">
        <button type="button" class="sh-btn sh-btn--secondary" onClick={onDecline}>
          {t('connections.requests.decline')}
        </button>
        <button type="button" class="sh-btn sh-btn--primary" onClick={onApprove}>
          {t('connections.requests.approve')}
        </button>
      </div>
    </div>
  )
}

export function RequestList({ requests, onApprove, onDecline }: Props) {
  if (requests.length === 0) return null
  const n = requests.length
  return (
    <section class="sh-conn-sect sh-conn-sect--tight" aria-labelledby="sh-conn-requests">
      <span class="sh-conn-eyebrow sh-conn-eyebrow--hearth" id="sh-conn-requests">
        {t(isOne(n) ? 'connections.requests.label_one' : 'connections.requests.label', { n: String(n) })}
      </span>
      <div class="sh-conn-stack">
        {requests.map((r) => (
          <RequestCard
            key={r.request_id}
            request={r}
            onApprove={() => onApprove(r)}
            onDecline={() => onDecline(r)}
          />
        ))}
      </div>
    </section>
  )
}
