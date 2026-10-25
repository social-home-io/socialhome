/**
 * LoadErrorState — what a list shows when its first load failed:
 * a warning, what went wrong, and a Retry button. Pairs with
 * ``utils/useLoad`` (``state === 'error'`` → render this with its
 * ``retry``) or ``utils/loadStatus`` (``failed``). Never fall back to
 * the empty state on a failed load — "nothing here" and "couldn't
 * fetch" call for different actions.
 *
 * StaleNotice — the inline line for a refresh that failed while the
 * list stays on screen ("Couldn't refresh. Showing … from 13:02."), or
 * for results kept from an earlier load.
 */
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'

interface LoadErrorStateProps {
  message?: string
  /** One sentence under the heading: what happened, what to do. */
  detail?: string
  onRetry: () => void
  /** The button's label; ``Retry`` by default. */
  retryLabel?: string
  /** The heading's level — ``2`` when the state sits straight under the
   *  page's h1 (axe heading-order), like the empty state it replaces. */
  headingLevel?: 2 | 3
}

export function LoadErrorState({
  message, detail, onRetry, retryLabel, headingLevel = 3,
}: LoadErrorStateProps) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3'
  return (
    <div class="sh-empty-state sh-load-error" role="alert">
      <div aria-hidden="true">⚠️</div>
      <Heading>{message ?? t('organize.load_failed')}</Heading>
      {detail && <p>{detail}</p>}
      <div class="sh-empty-state__cta-row">
        <Button onClick={onRetry}>{retryLabel ?? t('common.retry')}</Button>
      </div>
    </div>
  )
}

interface StaleNoticeProps {
  message: string
  onRetry: () => void
  /** ``role=alert`` when this line is the only sign of the failure
   *  (no list kept); ``status`` while the list stays on screen. */
  urgent?: boolean
}

export function StaleNotice({ message, onRetry, urgent = false }: StaleNoticeProps) {
  return (
    <div class="sh-stale-notice" role={urgent ? 'alert' : 'status'}>
      <span aria-hidden="true">⚠️</span>
      <span class="sh-stale-notice__text">{message}</span>
      <Button variant="secondary" onClick={onRetry}>{t('load.try_again')}</Button>
    </div>
  )
}
