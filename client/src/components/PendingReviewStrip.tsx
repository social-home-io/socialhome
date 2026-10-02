/**
 * PendingReviewStrip — "Pending review (n)": the viewer's OWN items in
 * a space that wait for a moderator (§4.3 "Reviewed"), at the top of
 * the matching space tab.
 *
 * Reads ``store/moderationMine`` (``GET .../moderation/mine``, refreshed
 * by ``useModerationMine`` on the page and on every
 * ``space.moderation.mine`` frame). Only pending items of ``feature``
 * show, dimmed, each with an "Awaiting review" badge, what kind of
 * change it is and a short preview — nobody else sees them until they
 * are approved. Renders nothing when there are none.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { pendingMine } from '@/store/moderationMine'
import {
  actionLabel, expiryLabel, previewText, type ModerationItem,
} from '@/features/spaces/moderationItems'

/** Items shown before "Show all". */
const COLLAPSED = 3

export function PendingReviewStrip({ spaceId, feature, filter }: {
  spaceId: string
  feature: string
  /** Narrow further (e.g. the bazaar tab: only posts that carry a listing). */
  filter?: (item: ModerationItem) => boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const items = pendingMine(spaceId, feature).filter(i => !filter || filter(i))
  if (items.length === 0) return null
  const shown = expanded ? items : items.slice(0, COLLAPSED)
  const headingId = `sh-pending-review-${spaceId}-${feature}`
  return (
    <section class="sh-pending-review" aria-labelledby={headingId}>
      <h3 class="sh-pending-review__heading" id={headingId}>
        {t('moderation.mine.heading')}{' '}
        {/* The count is set in the body face with lining numerals: the
         *  display face's stylistic "4" (``ss01``) reads as a "1" at this
         *  size, so "Pending review (4)" looked like "(1)". */}
        <span class="sh-pending-review__count" data-testid="pending-review-count">
          ({items.length})
        </span>
      </h3>
      <p class="sh-pending-review__hint sh-muted">{t('moderation.mine.hint')}</p>
      <ul class="sh-pending-review__list">
        {shown.map(item => {
          const text = previewText(item)
          return (
            <li key={item.id} class="sh-pending-review__item">
              <span class="sh-badge sh-badge--pending">{t('moderation.mine.badge')}</span>
              <span class="sh-pending-review__body">
                <span class="sh-pending-review__action">{actionLabel(item)}</span>
                {text && <span class="sh-pending-review__text">{text}</span>}
              </span>
              <span class="sh-pending-review__expiry sh-muted">{expiryLabel(item.expires_at)}</span>
            </li>
          )
        })}
      </ul>
      {items.length > COLLAPSED && (
        <button
          type="button"
          class="sh-link-button sh-pending-review__more"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded
            ? t('moderation.mine.show_less')
            : t('moderation.mine.show_all', { n: String(items.length) })}
        </button>
      )}
    </section>
  )
}
