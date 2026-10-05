/**
 * SecretReveal — show a just-minted secret exactly once.
 *
 * The backend stores only a hash of API tokens and iCal feed tokens, so
 * the raw value exists in the one response that minted it. This panel
 * is deliberately loud about that: the secret, a Copy button, and a
 * "you won't see this again" line, plus an explicit dismiss so the user
 * decides when it disappears (never a timeout).
 */
import type { ComponentChildren } from 'preact'
import { useState } from 'preact/hooks'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

interface Props {
  /** Short heading, e.g. "Your new token". */
  title: string
  /** The raw secret (token or URL) to show and copy. */
  secret: string
  /** Accessible name for the secret block, e.g. "API token". */
  secretLabel: string
  /** Extra guidance under the warning (usage example, next steps). */
  children?: ComponentChildren
  /** Dismiss button label. Omit to render no dismiss button. */
  dismissLabel?: string
  onDismiss?: () => void
}

export function SecretReveal({
  title, secret, secretLabel, children, dismissLabel, onDismiss,
}: Props) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(secret)
      setCopied(true)
      showToast(t('secret.copied_toast'), 'success')
    } catch {
      // Clipboard API is blocked in insecure contexts (plain-http LAN
      // installs) and some iframes — the text stays selectable.
      showToast(t('secret.copy_failed'), 'error')
    }
  }
  return (
    <div class="sh-secret-reveal" role="status">
      <div class="sh-secret-reveal__title">{title}</div>
      <p class="sh-secret-reveal__warning">
        <strong>{t('secret.warning_strong')}</strong>{' '}
        {t('secret.warning_body')}
      </p>
      <div class="sh-secret-reveal__value">
        <code aria-label={secretLabel} tabIndex={0}>{secret}</code>
        <Button onClick={copy}>{copied ? t('secret.copied') : t('secret.copy')}</Button>
      </div>
      {children}
      {dismissLabel && onDismiss && (
        <div class="sh-secret-reveal__actions">
          <Button variant="secondary" onClick={onDismiss}>{dismissLabel}</Button>
        </div>
      )}
    </div>
  )
}
