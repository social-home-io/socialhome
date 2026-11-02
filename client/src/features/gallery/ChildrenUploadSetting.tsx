/**
 * Admin › Child protection: "Children may add photos" (gallery D6).
 *
 * On by default: a protected account uploads photos and makes albums like
 * anyone, deletes only its own and shares only where it may post anyway.
 * Off: it still sees what the household shows it, and adds nothing.
 * Saved on change.
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { showToast } from '@/components/Toast'
import { SwitchRow } from '@/features/admin/DashboardSwitchRow'
import { gallerySettings, loadGallerySettings, saveChildrenMayUpload } from './gallerySettings'

export function ChildrenUploadSetting() {
  const [saving, setSaving] = useState(false)
  useEffect(() => { void loadGallerySettings() }, [])
  const s = gallerySettings.value
  if (!s) return null  // couldn't read it: no switch to mis-set
  const onChange = async (next: boolean) => {
    setSaving(true)
    try {
      await saveChildrenMayUpload(next)
      showToast(t(next ? 'gallery.children.on_saved' : 'gallery.children.off_saved'), 'success')
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
    } finally {
      setSaving(false)
    }
  }
  return (
    <section class="sh-cp-gallery" aria-labelledby="sh-cp-gallery-heading">
      <h3 id="sh-cp-gallery-heading">{t('gallery.children.heading')}</h3>
      <SwitchRow
        id="sh-children-upload"
        label={t('gallery.children.label')}
        hint={t('gallery.children.hint')}
        checked={s.children_may_upload}
        disabled={saving || !s.can_edit}
        onChange={(v) => void onChange(v)}
      />
    </section>
  )
}
