/**
 * JoinRequestModal — reusable "Request to join this space" dialog.
 *
 * Works for both local-household spaces (join_mode=request, same
 * household) and cross-household spaces (remote peer, same mechanism
 * via §D2 federated join-request).
 */
import { useState } from 'preact/hooks'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'

export interface JoinRequestModalProps {
  open: boolean
  onClose: () => void
  spaceName: string
  hostDisplayName: string
  hostIsPaired: boolean
  /** Called with the message body on submit. Caller does the POST. */
  onSubmit: (message: string) => Promise<void>
}

export function JoinRequestModal({
  open, onClose, spaceName, hostDisplayName, hostIsPaired, onSubmit,
}: JoinRequestModalProps) {
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!open) return null

  const submit = async () => {
    setSubmitting(true); setError(null)
    try {
      await onSubmit(message)
      setMessage('')
      onClose()
    } catch (exc) {
      setError((exc as Error).message || t('space.join_request.failed'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={t('space.join_request.title', { name: spaceName })}>
      <p class="sh-muted">
        {t('space.join_request.sent_to', { host: hostDisplayName })}
        {' '}
        {hostIsPaired
          ? t('space.join_request.auto_added')
          : t('space.join_request.needs_connection')}
      </p>
      <label class="sh-form-field">
        <span>{t('space.join_request.message_label')}</span>
        <textarea
          value={message}
          rows={4}
          placeholder={t('space.join_request.message_placeholder')}
          onInput={(e) => setMessage((e.target as HTMLTextAreaElement).value)}
        />
      </label>
      {error && <p class="sh-error">{error}</p>}
      <div class="sh-modal-actions">
        <Button variant="secondary" onClick={onClose} disabled={submitting}>
          {t('common.cancel')}
        </Button>
        <Button
          variant="primary" onClick={submit} loading={submitting}
        >
          {t('space.join_request.send')}
        </Button>
      </div>
    </Modal>
  )
}
