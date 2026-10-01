/**
 * LoadErrorState — what a list shows when its first load failed:
 * a warning, what went wrong, and a Retry button. Pairs with
 * ``utils/useLoad`` (``state === 'error'`` → render this with its
 * ``retry``). Never fall back to the empty state on a failed load —
 * "nothing here" and "couldn't fetch" call for different actions.
 */
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'

interface LoadErrorStateProps {
  message?: string
  onRetry: () => void
}

export function LoadErrorState({ message, onRetry }: LoadErrorStateProps) {
  return (
    <div class="sh-empty-state sh-load-error" role="alert">
      <div aria-hidden="true">⚠️</div>
      <h3>{message ?? t('organize.load_failed')}</h3>
      <div class="sh-empty-state__cta-row">
        <Button onClick={onRetry}>{t('common.retry')}</Button>
      </div>
    </div>
  )
}
