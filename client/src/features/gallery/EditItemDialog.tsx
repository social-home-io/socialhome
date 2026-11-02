/**
 * EditItemDialog — a photo's caption and the day it was taken.
 *
 * The date is a day (no time — the gallery never keeps more of the
 * camera's clock); empty clears it, and the photo then sorts by the day it
 * was added. Household photos only, for whoever may delete them.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'

export const CAPTION_MAX = 300

/** Today as ``YYYY-MM-DD`` in local time — the date picker's upper bound. */
function today(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export interface EditableItem {
  id: string
  item_type: 'photo' | 'video'
  caption?: string | null
  taken_at?: string | null
}

export function EditItemDialog<T extends EditableItem>({ item, onClose, onSaved }: {
  item: T
  onClose: () => void
  onSaved: (updated: T) => void
}) {
  const [caption, setCaption] = useState(item.caption ?? '')
  const [day, setDay] = useState(item.taken_at && /^\d{4}-\d{2}-\d{2}$/.test(item.taken_at) ? item.taken_at : '')
  const [busy, setBusy] = useState(false)

  const submit = async (e: Event) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const updated = await api.patch<T>(`/api/gallery/items/${encodeURIComponent(item.id)}`, {
        caption: caption.trim() || null,
        taken_at: day || null,
      })
      onSaved({ ...item, ...updated })
      showToast(t('gallery.manage.edit_saved'), 'success')
      onClose()
    } catch (err) {
      showToast(String((err as Error)?.message ?? t('gallery.manage.edit_failed')), 'error')
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={item.item_type === 'video' ? t('gallery.manage.edit_title_video') : t('gallery.manage.edit_title_photo')}
    >
      <form onSubmit={submit} class="sh-edit-item">
        <label>
          {t('gallery.manage.edit_caption')}
          <textarea
            maxLength={CAPTION_MAX}
            value={caption}
            rows={3}
            onInput={(e) => setCaption((e.target as HTMLTextAreaElement).value)}
          />
        </label>
        <label>
          {t('gallery.manage.edit_taken')}
          <input
            type="date"
            value={day}
            min="1800-01-01"
            max={today()}
            onInput={(e) => setDay((e.target as HTMLInputElement).value)}
          />
        </label>
        <p class="sh-muted sh-edit-item-hint">{t('gallery.manage.edit_taken_hint')}</p>
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy}>{t('common.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}
