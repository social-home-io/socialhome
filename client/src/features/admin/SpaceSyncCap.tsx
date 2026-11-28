/**
 * SpaceSyncCap — Admin › Storage: "Storage for synced space files" (owner
 * 2026-11-28).
 *
 * Files of a space that reaches this home through a GFS are kept before
 * anyone opens them (galleries, current pages, new posts, upcoming events,
 * active listings), so they are around and this home can pass them on.
 * This is how much of them one space may keep; past it, the least recently
 * viewed go first, never one opened in the last 30 days. ``0`` = only what
 * someone here opens. Reads ``GET /api/storage/usage`` and saves with
 * ``PUT /api/admin/storage/quota`` on change (instant save), the same
 * settings the household storage limit uses.
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { formatBytes } from '@/utils/bytes'
import { t } from '@/i18n/i18n'

const MB = 1024 ** 2
const GB = 1024 ** 3

/** The sizes on offer, per space; ``0`` = only what is opened. 2 GB is the default. */
export const SYNC_CHOICES: readonly number[] = [0, 500 * MB, GB, 2 * GB, 5 * GB, 10 * GB]

export function SpaceSyncCap() {
  const cap = useSignal<number | null>(null)
  const saving = useSignal(false)

  useEffect(() => {
    api.get<{ space_sync_bytes?: number }>('/api/storage/usage')
      .then(u => { cap.value = typeof u?.space_sync_bytes === 'number' ? u.space_sync_bytes : null })
      .catch(() => { cap.value = null })
  }, [])

  if (cap.value === null) return null  // couldn't read it: no control to mis-set

  const choices = SYNC_CHOICES.includes(cap.value)
    ? SYNC_CHOICES
    : [...SYNC_CHOICES, cap.value].sort((a, b) => a - b)

  const onChange = async (e: Event) => {
    const next = Number((e.target as HTMLSelectElement).value)
    const prev = cap.value
    cap.value = next
    saving.value = true
    try {
      await api.put('/api/admin/storage/quota', { space_sync_bytes: next })
      showToast(t('admin.storage.space_sync_saved'), 'success')
    } catch (err: unknown) {
      cap.value = prev
      showToast(t('admin.storage.space_sync_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      saving.value = false
    }
  }

  return (
    <section class="sh-admin-moment-cap" aria-labelledby="sh-space-sync-heading">
      <h3 id="sh-space-sync-heading">{t('admin.storage.space_sync_heading')}</h3>
      <div class="sh-dashboard-row sh-dashboard-row--select">
        <div class="sh-dashboard-row__label">
          <label for="sh-space-sync">{t('admin.storage.space_sync_label')}</label>
          <span id="sh-space-sync-hint" class="sh-form-hint">{t('admin.storage.space_sync_hint')}</span>
        </div>
        <select
          id="sh-space-sync"
          class="sh-dashboard-row__control"
          value={String(cap.value)}
          aria-describedby="sh-space-sync-hint"
          disabled={saving.value}
          onChange={(e) => void onChange(e)}
        >
          {choices.map(n => (
            <option key={n} value={String(n)}>
              {n === 0 ? t('admin.storage.space_sync_none') : formatBytes(n)}
            </option>
          ))}
        </select>
      </div>
    </section>
  )
}
