/**
 * CallEmbedBlockedDialog — "open Social Home in its own tab to call".
 *
 * Shown instead of a generic error toast when the page embedding Social
 * Home denies the microphone to its frame — see :mod:`./embedPolicy`. Retrying can't help there, so
 * the dialog offers the one action that does: the same page in a
 * top-level tab.
 *
 * Mounted once at the app root; the call picker, the DM "call back"
 * button and the ringing dialog route their errors through
 * :func:`showCallError`.
 */
import { signal } from '@preact/signals'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { CallEmbedBlockedError, ownTabUrl } from './embedPolicy'
import { t } from '@/i18n/i18n'

const blocked = signal<string | null>(null)

/** Report a failed start / answer: the embed dialog for a policy denial,
 *  a toast (``"<prefix>: <reason>"``) for everything else. */
export function showCallError(prefix: string, err: unknown): void {
  if (err instanceof CallEmbedBlockedError) {
    blocked.value = err.message
    return
  }
  showToast(t('calls.error_reason', { prefix, reason: String((err as Error)?.message ?? err) }), 'error')
}

export function CallEmbedBlockedDialog() {
  if (blocked.value === null) return null
  const close = () => { blocked.value = null }
  return (
    <Modal open onClose={close} title={t('calls.embed.title')}>
      <p class="sh-call-embed-copy">{blocked.value}</p>
      <div class="sh-form-actions">
        <Button variant="secondary" onClick={close}>{t('common.not_now')}</Button>
        <a
          class="sh-btn sh-btn--primary"
          href={ownTabUrl()}
          target="_blank"
          rel="noopener noreferrer"
          onClick={close}
        >{t('calls.embed.open_tab')}</a>
      </div>
    </Modal>
  )
}
