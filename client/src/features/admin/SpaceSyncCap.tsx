/**
 * SpaceSyncCap — Admin › Storage: "Storage for synced space files" (owner
 * 2026-11-28).
 *
 * Files of a space that reaches this home through a GFS are kept before
 * anyone opens them (galleries, current pages, new posts, upcoming events,
 * active listings), so they are around and this home can pass them on.
 * Two settings bound them: how much one space may keep, and how much every
 * space may keep together. They only ever use the household storage
 * limit's free space — the household's own uploads always come first and
 * push synced files out. Past a setting, the least recently viewed go
 * first, never one opened in the last 30 days. ``0`` per space = only what
 * someone here opens. Reads ``GET /api/storage/usage`` and saves with
 * ``PUT /api/admin/storage/quota`` on change (instant save, kept across
 * restarts), the same settings the household storage limit uses.
 */
import { useEffect } from 'preact/hooks'
import { useSignal, type Signal } from '@preact/signals'
import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { formatBytes } from '@/utils/bytes'
import { t } from '@/i18n/i18n'

const MB = 1024 ** 2
const GB = 1024 ** 3

/** The sizes on offer, per space; ``0`` = only what is opened. 2 GB is the default. */
export const SYNC_CHOICES: readonly number[] = [0, 500 * MB, GB, 2 * GB, 5 * GB, 10 * GB]

/** The sizes on offer for every space together. 8 GB is the default. */
export const SYNC_TOTAL_CHOICES: readonly number[] = [GB, 2 * GB, 5 * GB, 8 * GB, 20 * GB, 50 * GB]

type Field = 'space_sync_bytes' | 'space_sync_total_bytes'

interface Usage {
  space_sync_bytes?: number
  space_sync_total_bytes?: number
}

export function SpaceSyncCap() {
  const perSpace = useSignal<number | null>(null)
  const total = useSignal<number | null>(null)
  const saving = useSignal(false)

  useEffect(() => {
    api.get<Usage>('/api/storage/usage')
      .then(u => {
        perSpace.value = typeof u?.space_sync_bytes === 'number' ? u.space_sync_bytes : null
        total.value = typeof u?.space_sync_total_bytes === 'number' ? u.space_sync_total_bytes : null
      })
      .catch(() => { perSpace.value = null; total.value = null })
  }, [])

  if (perSpace.value === null) return null  // couldn't read it: no control to mis-set

  const save = async (field: Field, value: Signal<number | null>, next: number) => {
    const prev = value.value
    value.value = next
    saving.value = true
    try {
      await api.put('/api/admin/storage/quota', { [field]: next })
      showToast(t('admin.storage.space_sync_saved'), 'success')
    } catch (err: unknown) {
      value.value = prev
      showToast(t('admin.storage.space_sync_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      saving.value = false
    }
  }

  return (
    <section class="sh-admin-moment-cap" aria-labelledby="sh-space-sync-heading">
      <h3 id="sh-space-sync-heading">{t('admin.storage.space_sync_heading')}</h3>
      <SizeRow
        id="sh-space-sync"
        label={t('admin.storage.space_sync_label')}
        hint={t('admin.storage.space_sync_hint')}
        value={perSpace.value}
        choices={SYNC_CHOICES}
        zeroLabel={t('admin.storage.space_sync_none')}
        disabled={saving.value}
        onPick={n => void save('space_sync_bytes', perSpace, n)}
      />
      {total.value !== null && (
        <SizeRow
          id="sh-space-sync-total"
          label={t('admin.storage.space_sync_total_label')}
          hint={t('admin.storage.space_sync_total_hint')}
          value={total.value}
          choices={SYNC_TOTAL_CHOICES}
          zeroLabel={t('admin.storage.space_sync_total_none')}
          disabled={saving.value}
          onPick={n => void save('space_sync_total_bytes', total, n)}
        />
      )}
    </section>
  )
}

function SizeRow(props: {
  id: string
  label: string
  hint: string
  value: number
  choices: readonly number[]
  zeroLabel: string
  disabled: boolean
  onPick: (n: number) => void
}) {
  const { id, value } = props
  const choices = props.choices.includes(value)
    ? props.choices
    : [...props.choices, value].sort((a, b) => a - b)
  return (
    <div class="sh-dashboard-row sh-dashboard-row--select">
      <div class="sh-dashboard-row__label">
        <label for={id}>{props.label}</label>
        <span id={`${id}-hint`} class="sh-form-hint">{props.hint}</span>
      </div>
      <select
        id={id}
        class="sh-dashboard-row__control"
        value={String(value)}
        aria-describedby={`${id}-hint`}
        disabled={props.disabled}
        onChange={(e) => props.onPick(Number((e.target as HTMLSelectElement).value))}
      >
        {choices.map(n => (
          <option key={n} value={String(n)}>
            {n === 0 ? props.zeroLabel : formatBytes(n)}
          </option>
        ))}
      </select>
    </div>
  )
}
