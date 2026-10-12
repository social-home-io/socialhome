/**
 * LoadError — a fetch failed. Says so (``role="alert"``) and offers a
 * retry; never falls back to the first-run "no households yet" state.
 */
import { t } from '@/i18n/i18n'
import { ConnIcon } from './ConnIcon'

interface Props {
  title: string
  body: string
  onRetry: () => void
}

export function LoadError({ title, body, onRetry }: Props) {
  return (
    <div class="sh-conn-error" role="alert">
      <span class="sh-conn-error__ico"><ConnIcon name="alert" /></span>
      <div>
        <h3>{title}</h3>
        <p>{body}</p>
      </div>
      <button type="button" class="sh-btn sh-btn--secondary" onClick={onRetry}>
        {t('connections.error.retry')}
      </button>
    </div>
  )
}
