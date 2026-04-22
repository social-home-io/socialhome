/**
 * ReportDialog — report content flow (§23.67).
 *
 * Reports always land with the local household admin; they also
 * auto-forward by default to every connected Global Federation Server
 * (GFS) so repeated fraud aggregates across households. The "Keep in
 * my household only" checkbox opts that forward off per-report.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

const open = signal(false)
const targetType = signal('')
const targetId = signal('')
const category = signal('')
const notes = signal('')
const householdOnly = signal(false)
const submitting = signal(false)

// Values match ``socialhome.domain.report.ReportCategory``.
const CATEGORIES: { value: string; label: string }[] = [
  { value: 'spam',           label: 'Spam' },
  { value: 'harassment',     label: 'Harassment' },
  { value: 'inappropriate',  label: 'Inappropriate content' },
  { value: 'misinformation', label: 'Misinformation' },
  { value: 'other',          label: 'Other' },
]

export function openReport(type: string, id: string) {
  targetType.value = type
  targetId.value = id
  category.value = ''
  notes.value = ''
  householdOnly.value = false
  open.value = true
}

export function ReportDialog() {
  const submit = async () => {
    if (!category.value || submitting.value) return
    submitting.value = true
    try {
      const resp: { federated?: boolean; forwarded_to_gfs?: boolean } =
        await api.post('/api/reports', {
          target_type: targetType.value,
          target_id:   targetId.value,
          category:    category.value,
          notes:       notes.value || undefined,
          forward_gfs: !householdOnly.value,
        })
      const bits: string[] = ['your household admin']
      if (resp?.federated) bits.push('the hosting household')
      if (resp?.forwarded_to_gfs) bits.push('any connected Global Server')
      showToast(`Report sent to ${bits.join(', ')}.`, 'success')
      open.value = false
    } catch {
      showToast('Failed to submit report', 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal
      open={open.value}
      onClose={() => (open.value = false)}
      title="Report Content"
    >
      <div class="sh-form">
        <label>
          Category *
          <select
            value={category.value}
            onChange={(e) =>
              (category.value = (e.target as HTMLSelectElement).value)
            }
          >
            <option value="">Select a reason...</option>
            {CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Additional notes
          <textarea
            value={notes.value}
            onInput={(e) =>
              (notes.value = (e.target as HTMLTextAreaElement).value)
            }
            rows={3}
            placeholder="Optional details..."
          />
        </label>
        <p class="sh-muted" style="margin:4px 0 2px">
          Reports are sent to your household admin. Reports on content
          from a Global Federation Server are also forwarded there for
          community review.
        </p>
        <label style="display:flex;gap:8px;align-items:center;margin:4px 0 10px">
          <input
            type="checkbox"
            checked={householdOnly.value}
            onChange={(e) =>
              (householdOnly.value = (e.target as HTMLInputElement).checked)
            }
          />
          Keep this report in my household only
        </label>
        <Button
          onClick={submit}
          loading={submitting.value}
          disabled={!category.value}
        >
          Submit report
        </Button>
      </div>
    </Modal>
  )
}
