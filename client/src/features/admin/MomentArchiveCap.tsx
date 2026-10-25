/**
 * MomentArchiveCap — Admin › Storage: how much moment media each person
 * keeps (decision 6a, owner sign-off 6).
 *
 * Everyone's own moments stay in their archive; past this per-person
 * limit their oldest pictures and videos go first (the text stays). Reads
 * the current limit from ``GET /api/moments/archive/usage`` and saves
 * with ``PUT /api/admin/moments/archive-cap`` on change (instant save).
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { formatBytes } from '@/utils/bytes'
import { t } from '@/i18n/i18n'

const MB = 1024 ** 2
const GB = 1024 ** 3

/** The limits on offer; ``0`` = no limit. 2 GB is the household default. */
export const CAP_CHOICES: readonly number[] = [500 * MB, GB, 2 * GB, 5 * GB, 10 * GB, 0]

export function MomentArchiveCap() {
  const cap = useSignal<number | null>(null)
  const saving = useSignal(false)

  useEffect(() => {
    api.get<{ cap_bytes: number }>('/api/moments/archive/usage')
      .then(u => { cap.value = u?.cap_bytes ?? null })
      .catch(() => { cap.value = null })
  }, [])

  if (cap.value === null) return null  // couldn't read it: no control to mis-set

  const choices = CAP_CHOICES.includes(cap.value)
    ? CAP_CHOICES
    : [...CAP_CHOICES.slice(0, -1), cap.value, 0].sort((a, b) => (a || Infinity) - (b || Infinity))

  const onChange = async (e: Event) => {
    const next = Number((e.target as HTMLSelectElement).value)
    const prev = cap.value
    cap.value = next
    saving.value = true
    try {
      await api.put('/api/admin/moments/archive-cap', { cap_bytes: next })
      showToast(t('admin.storage.moments_cap_saved'), 'success')
    } catch (err: unknown) {
      cap.value = prev
      showToast(t('admin.storage.moments_cap_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      saving.value = false
    }
  }

  return (
    <section class="sh-admin-moment-cap" aria-labelledby="sh-moment-cap-heading">
      <h3 id="sh-moment-cap-heading">{t('admin.storage.moments_heading')}</h3>
      <div class="sh-dashboard-row sh-dashboard-row--select">
        <div class="sh-dashboard-row__label">
          <label for="sh-moment-cap">{t('admin.storage.moments_cap_label')}</label>
          <span id="sh-moment-cap-hint" class="sh-form-hint">{t('admin.storage.moments_hint')}</span>
        </div>
        <select
          id="sh-moment-cap"
          class="sh-dashboard-row__control"
          value={String(cap.value)}
          aria-describedby="sh-moment-cap-hint"
          disabled={saving.value}
          onChange={(e) => void onChange(e)}
        >
          {choices.map(n => (
            <option key={n} value={String(n)}>
              {n === 0 ? t('admin.storage.moments_cap_none') : formatBytes(n)}
            </option>
          ))}
        </select>
      </div>
    </section>
  )
}
