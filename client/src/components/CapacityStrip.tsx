/**
 * CapacityStrip — compact summary of an event's RSVP counts.
 *
 * Renders something like ``"12/20 going · 3 waitlist · 2 maybe · 5 pending"``
 * with the numerator suppressed when the event has no capacity. Skips
 * zero counts to reduce noise. The current user's status is highlighted
 * with a light pill so they can find themselves at a glance.
 *
 * Used in:
 * * :class:`EventPostCard` — feed-card variant of an event post.
 * * Calendar event detail card — under the title row.
 *
 * Phase C / Phase E UX layer (PR #33 deferred frontend).
 */
import { t } from '@/i18n/i18n'
import type { RsvpCounts } from '@/store/calendar'

export interface CapacityStripProps {
  /** Live counts. ``requested`` / ``waitlist`` may be missing on
   *  uncapped events; treated as zero. */
  counts: RsvpCounts | undefined
  /** Optional capacity for "X / N going". ``null`` / ``undefined`` =
   *  open RSVP, no denominator rendered. */
  capacity?: number | null
  /** Current user's RSVP status for this event/occurrence, if any.
   *  When set, the matching segment is rendered as a highlighted pill. */
  myStatus?: 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist' | null
}

const SEGMENT_ORDER: Array<keyof RsvpCounts> = [
  'going',
  'waitlist',
  'requested',
  'maybe',
  'declined',
]

export function CapacityStrip({
  counts,
  capacity,
  myStatus,
}: CapacityStripProps) {
  if (!counts) {
    return (
      <div class="sh-capacity-strip sh-capacity-strip--empty">
        {t('event.capacity.no_rsvps')}
      </div>
    )
  }
  const segments = SEGMENT_ORDER
    .map((key) => {
      const n = counts[key] ?? 0
      if (key !== 'going' && n === 0) return null
      const label = key === 'going' && capacity != null
        ? t('event.capacity.going_with_cap', {
            n: String(n),
            cap: String(capacity),
          })
        : t(`event.capacity.${key}`, { n: String(n) })
      return { key, label, mine: myStatus === key }
    })
    .filter((x): x is { key: keyof RsvpCounts; label: string; mine: boolean } => x !== null)

  if (segments.length === 0) {
    return (
      <div class="sh-capacity-strip sh-capacity-strip--empty">
        {t('event.capacity.no_rsvps')}
      </div>
    )
  }

  return (
    <div class="sh-capacity-strip" aria-label={t('event.capacity.aria')}>
      {segments.map((seg, i) => (
        <span
          key={seg.key}
          class={`sh-capacity-segment sh-capacity-segment--${seg.key}${
            seg.mine ? ' sh-capacity-segment--mine' : ''
          }`}
        >
          {seg.label}
          {i < segments.length - 1 && (
            <span class="sh-capacity-sep" aria-hidden="true"> · </span>
          )}
        </span>
      ))}
    </div>
  )
}
