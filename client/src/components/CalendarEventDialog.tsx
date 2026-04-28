/**
 * CalendarEventDialog — event creation + detail (§23.60).
 *
 * Phase C addition: optional ``capacity`` for space events. When set,
 * the server flips members' "going" RSVPs into ``requested`` pending
 * host approval. The field is gated behind a "Limit attendance"
 * checkbox so the simple-event happy path stays uncluttered.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Modal } from './Modal'
import { Button } from './Button'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

const open = signal(false)
const calendarId = signal('')
const spaceId = signal<string | null>(null)
const summary = signal('')
const startDate = signal('')
const startTime = signal('')
const endDate = signal('')
const endTime = signal('')
const allDay = signal(false)
const description = signal('')
const limitAttendance = signal(false)
const capacity = signal('')
const submitting = signal(false)

/** Open the dialog for a personal calendar (legacy entry point). */
export function openEventDialog(calId: string) {
  reset()
  calendarId.value = calId
  spaceId.value = null
  open.value = true
}

/** Open the dialog for a space calendar (Phase C). When ``spaceIdValue``
 *  is set, the form shows the "Limit attendance" capacity field and
 *  the submit goes to ``/api/spaces/{id}/calendar/events``. */
export function openSpaceEventDialog(spaceIdValue: string) {
  reset()
  calendarId.value = ''
  spaceId.value = spaceIdValue
  open.value = true
}

function reset() {
  summary.value = ''
  description.value = ''
  const now = new Date()
  startDate.value = now.toISOString().slice(0, 10)
  startTime.value = now.toTimeString().slice(0, 5)
  const end = new Date(now.getTime() + 3600000)
  endDate.value = end.toISOString().slice(0, 10)
  endTime.value = end.toTimeString().slice(0, 5)
  allDay.value = false
  limitAttendance.value = false
  capacity.value = ''
}

export function CalendarEventDialog({ onCreated }: { onCreated?: () => void }) {
  const isSpace = spaceId.value !== null

  const submit = async () => {
    if (!summary.value.trim() || submitting.value) return
    if (limitAttendance.value) {
      const cap = parseInt(capacity.value, 10)
      if (Number.isNaN(cap) || cap < 0) {
        showToast(t('event.dialog.capacity_invalid'), 'error')
        return
      }
    }
    submitting.value = true
    try {
      const start = allDay.value
        ? `${startDate.value}T00:00:00Z`
        : `${startDate.value}T${startTime.value}:00Z`
      const end = allDay.value
        ? `${endDate.value}T23:59:59Z`
        : `${endDate.value}T${endTime.value}:00Z`
      const body: Record<string, unknown> = {
        summary: summary.value,
        start,
        end,
        all_day: allDay.value,
        description: description.value || undefined,
      }
      if (limitAttendance.value && capacity.value) {
        body.capacity = parseInt(capacity.value, 10)
      }
      const url = isSpace
        ? `/api/spaces/${spaceId.value}/calendar/events`
        : `/api/calendars/${calendarId.value}/events`
      await api.post(url, body)
      showToast(t('event.dialog.created'), 'success')
      open.value = false
      onCreated?.()
    } catch (e) {
      const msg = (e as Error)?.message || t('event.dialog.failed')
      showToast(msg, 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value} onClose={() => (open.value = false)} title={t('event.dialog.title')}>
      <div class="sh-form">
        <label>
          {t('event.dialog.summary')} *
          <input
            value={summary.value}
            onInput={(e) =>
              (summary.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={allDay.value}
            onChange={() => (allDay.value = !allDay.value)}
          />{' '}
          {t('event.dialog.all_day')}
        </label>
        <label>
          {t('event.dialog.start_date')}
          <input
            type="date"
            value={startDate.value}
            onInput={(e) =>
              (startDate.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        {!allDay.value && (
          <label>
            {t('event.dialog.start_time')}
            <input
              type="time"
              value={startTime.value}
              onInput={(e) =>
                (startTime.value = (e.target as HTMLInputElement).value)
              }
            />
          </label>
        )}
        <label>
          {t('event.dialog.end_date')}
          <input
            type="date"
            value={endDate.value}
            onInput={(e) =>
              (endDate.value = (e.target as HTMLInputElement).value)
            }
          />
        </label>
        {!allDay.value && (
          <label>
            {t('event.dialog.end_time')}
            <input
              type="time"
              value={endTime.value}
              onInput={(e) =>
                (endTime.value = (e.target as HTMLInputElement).value)
              }
            />
          </label>
        )}
        <label>
          {t('event.dialog.description')}
          <textarea
            value={description.value}
            onInput={(e) =>
              (description.value = (e.target as HTMLTextAreaElement).value)
            }
            rows={2}
          />
        </label>

        {isSpace && (
          <>
            <label class="sh-form-row-cap">
              <input
                type="checkbox"
                checked={limitAttendance.value}
                onChange={() => (limitAttendance.value = !limitAttendance.value)}
              />{' '}
              {t('event.dialog.limit_attendance')}
            </label>
            {limitAttendance.value && (
              <label>
                {t('event.dialog.capacity')}
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={capacity.value}
                  onInput={(e) =>
                    (capacity.value = (e.target as HTMLInputElement).value)
                  }
                />
                <small class="sh-form-help">
                  {t('event.dialog.capacity_help')}
                </small>
              </label>
            )}
          </>
        )}

        <Button
          onClick={submit}
          loading={submitting.value}
          disabled={!summary.value.trim()}
        >
          {t('event.dialog.create')}
        </Button>
      </div>
    </Modal>
  )
}
