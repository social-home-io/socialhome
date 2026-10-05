/**
 * ReadReceipts — checkmark indicators in DMs (§23.47d, §23.70).
 */
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'

interface ReadReceiptProps {
  sent: boolean
  delivered: boolean
  read: boolean
}

export function ReadReceipt({ sent, delivered, read }: ReadReceiptProps) {
  if (read) return <span class="sh-receipt sh-receipt--read" title={t('dms.receipt.read')}>✓✓</span>
  if (delivered) return <span class="sh-receipt sh-receipt--delivered" title={t('dms.receipt.delivered')}>✓✓</span>
  if (sent) return <span class="sh-receipt sh-receipt--sent" title={t('dms.receipt.sent')}>✓</span>
  return <span class="sh-receipt sh-receipt--pending" title={t('dms.receipt.sending')}>○</span>
}

/**
 * ReadReceiptsOptIn — privacy toggle (§23.70).
 */

export const readReceiptsEnabled = signal(
  localStorage.getItem('sh_read_receipts') !== 'off'
)

export function ReadReceiptsToggle() {
  const toggle = () => {
    readReceiptsEnabled.value = !readReceiptsEnabled.value
    localStorage.setItem('sh_read_receipts', readReceiptsEnabled.value ? 'on' : 'off')
  }
  return (
    <label class="sh-toggle-row">
      <input type="checkbox" checked={readReceiptsEnabled.value} onChange={toggle} />
      {t('dms.receipt.toggle')}
    </label>
  )
}
