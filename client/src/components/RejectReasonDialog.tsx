/**
 * RejectReasonDialog — accessible textarea prompt for moderation
 * rejections + post removals. Replaces the ugly browser ``prompt()``.
 */
import { signal } from '@preact/signals'
import { Modal } from './Modal'
import { Button } from './Button'
import { t } from '@/i18n/i18n'

interface PendingAsk {
  title:   string
  label:   string
  onSubmit: (reason: string) => Promise<void> | void
}

const open = signal(false)
const ask = signal<PendingAsk | null>(null)
const reason = signal('')
const busy = signal(false)

export function openRejectReason(params: PendingAsk): void {
  ask.value = params
  reason.value = ''
  busy.value = false
  open.value = true
}

export function RejectReasonDialog() {
  const close = () => {
    open.value = false
    ask.value = null
    reason.value = ''
    busy.value = false
  }
  const submit = async () => {
    const current = ask.value
    if (!current || busy.value) return
    busy.value = true
    try {
      await current.onSubmit(reason.value.trim())
      close()
    } finally {
      busy.value = false
    }
  }
  const current = ask.value
  return (
    <Modal
      open={open.value}
      onClose={close}
      title={current?.title || t('reject_reason.title')}
    >
      <div class="sh-form">
        <label>
          {current?.label || t('reject_reason.label')}
          <textarea
            rows={3}
            maxLength={500}
            value={reason.value}
            onInput={(e) => {
              reason.value = (e.target as HTMLTextAreaElement).value
            }}
          />
        </label>
        <div class="sh-form-actions">
          <Button variant="secondary" onClick={close}>{t('common.cancel')}</Button>
          <Button onClick={submit} loading={busy.value}>{t('reject_reason.submit')}</Button>
        </div>
      </div>
    </Modal>
  )
}
