/**
 * HomeNameSettings — admin editor for the household's federated name.
 *
 * The home name is what other households see once paired. It is served
 * from the DB ``display_name`` via ``GET /api/instance/config`` (mirrored
 * into the ``instanceConfig`` store) and changed with the admin-only
 * ``PATCH /api/admin/instance`` (body ``{ display_name }``, 1–80 chars),
 * which re-broadcasts the new name to peers. On a successful save we
 * optimistically reflect the new name into the store so the UI updates
 * without a re-fetch.
 */
import { useState } from 'preact/hooks'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { instanceConfig } from '@/store/instance'
import { t } from '@/i18n/i18n'

const MAX_LEN = 80

export function HomeNameSettings() {
  const current = instanceConfig.value?.instance_name ?? ''
  const [name, setName] = useState(current)
  const [saving, setSaving] = useState(false)

  const trimmed = name.trim()
  const invalid =
    trimmed === '' || trimmed.length > MAX_LEN || trimmed === current.trim()

  const save = async () => {
    if (invalid) return
    setSaving(true)
    try {
      await api.patch('/api/admin/instance', { display_name: trimmed })
      instanceConfig.value = { ...instanceConfig.value!, instance_name: trimmed }
      showToast(t('home_name.updated'), 'success')
    } catch {
      showToast(t('home_name.update_failed'), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div class="sh-settings-subcard" id="home-name">
      <h3 class="sh-settings-panel-heading">{t('home_name.title')}</h3>
      <p class="sh-muted sh-settings-panel-blurb">
        {t('home_name.blurb')}
      </p>
      <label class="sh-form-row">
        {t('home_name.title')}
        <input
          type="text"
          maxLength={MAX_LEN}
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
        />
      </label>
      <div class="sh-form-actions">
        <Button onClick={() => void save()} loading={saving} disabled={invalid}>
          {t('common.save')}
        </Button>
      </div>
    </div>
  )
}
