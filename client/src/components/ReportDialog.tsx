/**
 * ReportDialog — report content flow (§23.67).
 *
 * Who gets the report depends on where the content lives:
 *
 * - **inside a space** (a space post, page, task, … or a member's conduct
 *   there — pass ``spaceId``) — the space's moderators (owner, admins,
 *   moderators), on every household that moderates it. The server derives
 *   the space from the content itself; ``spaceId`` is required only for a
 *   member report.
 * - **anything else** — the household admin, and by default every
 *   connected Global Federation Server (GFS) so repeated fraud aggregates
 *   across households. The "Keep in my household only" checkbox opts that
 *   forward off per report. Content of a private space never reaches a GFS.
 */
import { signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { t } from '@/i18n/i18n'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'

const open = signal(false)
const targetType = signal('')
const targetId = signal('')
const spaceScope = signal<string | null>(null)
const category = signal('')
const notes = signal('')
const householdOnly = signal(false)
const submitting = signal(false)

// Values match ``socialhome.domain.report.ReportCategory``.
const CATEGORIES = ['spam', 'harassment', 'inappropriate', 'misinformation', 'other'] as const

/** Open the dialog for ``(type, id)``; ``spaceId`` when the item (or the
 *  member) is inside a space, so the report goes to its moderators. */
export function openReport(type: string, id: string, spaceId?: string | null) {
  targetType.value = type
  targetId.value = id
  spaceScope.value = spaceId || null
  category.value = ''
  notes.value = ''
  householdOnly.value = false
  open.value = true
}

/** The success toast for a filed report (exported for tests). */
export function reportSentMessage(resp: {
  space_id?: string | null
  federated?: boolean
  forwarded_to_gfs?: boolean
} | null | undefined): string {
  if (resp?.space_id) return t('report.sent.space')
  return resp?.forwarded_to_gfs ? t('report.sent.household_gfs') : t('report.sent.household')
}

export function ReportDialog() {
  const inSpace = spaceScope.value !== null
  const submit = async () => {
    if (!category.value || submitting.value) return
    submitting.value = true
    try {
      const body: Record<string, unknown> = {
        target_type: targetType.value,
        target_id:   targetId.value,
        category:    category.value,
        notes:       notes.value || undefined,
        forward_gfs: !householdOnly.value,
      }
      // Only a member report needs the space named — content carries
      // its own space, and the server derives it.
      if (spaceScope.value && targetType.value === 'user') body.space_id = spaceScope.value
      const resp = await api.post('/api/reports', body) as {
        space_id?: string | null; federated?: boolean; forwarded_to_gfs?: boolean
      }
      showToast(reportSentMessage(resp), 'success')
      open.value = false
    } catch (e: unknown) {
      const dup = e instanceof ApiError && e.status === 409
      showToast(t(dup ? 'report.duplicate' : 'report.failed'), dup ? 'info' : 'error')
      if (dup) open.value = false
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal
      open={open.value}
      onClose={() => (open.value = false)}
      title={t(targetType.value === 'user' ? 'report.title_member' : 'report.title')}
    >
      <div class="sh-form">
        <p class="sh-muted" style={{ marginTop: 0 }}>
          {t(inSpace ? 'report.intro.space' : 'report.intro.household')}
        </p>
        <label>
          {t('report.category_label')}
          <select
            value={category.value}
            onChange={(e) =>
              (category.value = (e.target as HTMLSelectElement).value)
            }
          >
            <option value="">{t('report.select_reason')}</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {t(`report.category.${c}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('report.notes_label')}
          <textarea
            value={notes.value}
            onInput={(e) =>
              (notes.value = (e.target as HTMLTextAreaElement).value)
            }
            rows={3}
            maxLength={1000}
            placeholder={t('report.notes_placeholder')}
          />
        </label>
        {!inSpace && (
          <label style="display:flex;gap:8px;align-items:flex-start;margin:0">
            <input
              type="checkbox"
              checked={householdOnly.value}
              onChange={(e) =>
                (householdOnly.value = (e.target as HTMLInputElement).checked)
              }
              style={{ marginTop: '4px' }}
            />
            <span>
              {t('report.household_only')}
              <span class="sh-muted" style={{ display: 'block', fontSize: 'var(--sh-font-size-xs)' }}>
                {t('report.household_only_hint')}
              </span>
            </span>
          </label>
        )}
        <div class="sh-form-actions">
          <Button
            variant="secondary"
            onClick={() => { open.value = false }}
          >
            {t('report.cancel')}
          </Button>
          <Button
            onClick={submit}
            loading={submitting.value}
            disabled={!category.value}
          >
            {t('report.submit')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
