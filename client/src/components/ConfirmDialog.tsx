/**
 * ConfirmDialog — confirmation pattern (§23.55).
 */
import { Modal } from './Modal'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

interface ConfirmDialogProps {
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  /** A notice with one way out: no cancel button (Escape / ✕ still close). */
  notice?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({
  open, title, message, confirmLabel = t('common.confirm'),
  cancelLabel = t('common.cancel'), destructive, notice, onConfirm, onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal open={open} onClose={onCancel} title={title}>
      <p class="sh-confirm-message">{message}</p>
      <div class="sh-confirm-actions">
        {!notice && <Button variant="secondary" onClick={onCancel}>{cancelLabel}</Button>}
        <Button variant={destructive ? 'danger' : 'primary'} onClick={onConfirm}>
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  )
}
