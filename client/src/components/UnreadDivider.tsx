/**
 * UnreadDivider — "New messages" separator inside a chat thread.
 *
 * Rendered immediately above the first message the caller hasn't
 * read yet on thread entry. Telegram/Signal style: a thin horizontal
 * rule with a centred pill label, terracotta accent. Stays in the
 * DOM for the rest of the thread session so the user can scroll
 * back up to find their place after reading on; the parent clears
 * the anchor when the caller reaches the bottom (catches up).
 *
 * Stateless, presentational — the surrounding container picks the
 * insertion point based on the ``unreadAnchor`` signal.
 */
import type { JSX } from 'preact'
import { t } from '@/i18n/i18n'

export function UnreadDivider(): JSX.Element {
  return (
    <div
      class="sh-dm-unread-divider"
      role="separator"
      aria-label={t('dms.unread_divider_aria')}
    >
      <span class="sh-dm-unread-divider__pill">{t('dms.unread_divider')}</span>
    </div>
  )
}
