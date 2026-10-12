/**
 * CancelPairingDialog — confirm before cancelling a pairing that is still
 * waiting on the other household. Wording follows what is pending:
 *   code     "Cancel pairing with {name}?" — the code stops working (we
 *            can't tell whether we issued or scanned it, so stay neutral);
 *   request  "Cancel your request to pair with {name}?";
 *   approval "Withdraw your approval for {name}?" — you won't be paired.
 * The only filled destructive button on the page lives here. Esc / "Keep
 * waiting" back out; focus returns to the card's Cancel button (Modal).
 */
import { Modal } from '@/components/Modal'
import { t } from '@/i18n/i18n'
import type { PendingKind } from './model'

const COPY: Record<PendingKind, { title: string; body: string; ok: string }> = {
  code: {
    title: 'connections.pending.confirm_title',
    body: 'connections.pending.confirm_body',
    ok: 'connections.pending.confirm_ok',
  },
  request: {
    title: 'connections.pending.request_title',
    body: 'connections.pending.request_body',
    ok: 'connections.pending.request_ok',
  },
  approval: {
    title: 'connections.pending.approval_title',
    body: 'connections.pending.approval_body',
    ok: 'connections.pending.approval_ok',
  },
}

interface Props {
  target: { name: string; kind: PendingKind } | null
  busy: boolean
  onConfirm: () => void
  onClose: () => void
}

export function CancelPairingDialog({ target, busy, onConfirm, onClose }: Props) {
  const copy = COPY[target?.kind ?? 'code']
  return (
    <Modal
      open={target !== null}
      onClose={onClose}
      title={t(copy.title, { name: target?.name ?? '' })}
      class="sh-conn-confirm"
      role="alertdialog"
    >
      <p>{t(copy.body, { name: target?.name ?? '' })}</p>
      <div class="sh-conn-confirm__actions">
        <button type="button" class="sh-btn sh-btn--secondary" onClick={onClose}>
          {t('connections.pending.keep')}
        </button>
        <button type="button" class="sh-btn sh-btn--danger" disabled={busy} onClick={onConfirm}>
          {t(copy.ok)}
        </button>
      </div>
    </Modal>
  )
}
