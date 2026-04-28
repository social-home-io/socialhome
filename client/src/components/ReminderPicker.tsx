/**
 * ReminderPicker — per-event, per-user reminder configuration (Phase D).
 *
 * Reminders are user-set per occurrence: chips for the common offsets
 * (15 min / 1 h / 1 day / 1 week), plus a custom-minutes input for
 * arbitrary offsets. Existing reminders render with a "remove" affordance.
 *
 * UX additions on top of the base spec:
 *
 * * Smart default — when the user has no reminders set and they hit an
 *   "Add reminder" affordance, the picker opens with the 1 hour preset
 *   pre-filled.
 * * "At start" preset (0 minutes) — useful for events that only ping
 *   when they begin.
 * * Compact rendering: chips wrap on mobile; the custom input collapses
 *   into a button until the user opens it.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import type { EventReminder } from '@/types'

export interface ReminderPickerProps {
  eventId: string
  /** Optional occurrence_at; recurring events MUST pass it (server
   *  rejects without it). Non-recurring may omit. */
  occurrenceAt?: string | null
}

interface Preset {
  minutes: number
  i18nKey: string
}

const PRESETS: Preset[] = [
  { minutes: 0,    i18nKey: 'event.reminder.preset.at_start' },
  { minutes: 15,   i18nKey: 'event.reminder.preset.fifteen_min' },
  { minutes: 60,   i18nKey: 'event.reminder.preset.one_hour' },
  { minutes: 1440, i18nKey: 'event.reminder.preset.one_day' },
  { minutes: 10080,i18nKey: 'event.reminder.preset.one_week' },
]

function key(eventId: string, occurrenceAt: string | null | undefined) {
  return occurrenceAt ? `${eventId}@${occurrenceAt}` : eventId
}

const remindersByEvent = signal<Record<string, EventReminder[]>>({})
const loading = signal<Record<string, boolean>>({})

async function refresh(eventId: string, occurrenceAt?: string | null) {
  const k = key(eventId, occurrenceAt)
  loading.value = { ...loading.value, [k]: true }
  try {
    const params: Record<string, string> = {}
    if (occurrenceAt) params.occurrence_at = occurrenceAt
    const res = await api.get<{ reminders: EventReminder[] }>(
      `/api/calendars/events/${eventId}/reminders`,
      params,
    )
    remindersByEvent.value = {
      ...remindersByEvent.value,
      [k]: res.reminders ?? [],
    }
  } catch {
    remindersByEvent.value = { ...remindersByEvent.value, [k]: [] }
  } finally {
    const next = { ...loading.value }
    delete next[k]
    loading.value = next
  }
}

export function ReminderPicker({ eventId, occurrenceAt }: ReminderPickerProps) {
  const k = key(eventId, occurrenceAt)
  const [showCustom, setShowCustom] = useState(false)
  const [customMinutes, setCustomMinutes] = useState('60')

  useEffect(() => {
    refresh(eventId, occurrenceAt)
  }, [eventId, occurrenceAt])

  const rows = remindersByEvent.value[k] ?? []
  const setMinutes = new Set(rows.map((r) => r.minutes_before))

  const add = async (minutes: number) => {
    if (minutes < 0 || !Number.isFinite(minutes)) {
      showToast(t('event.reminder.invalid'), 'error')
      return
    }
    try {
      await api.post(`/api/calendars/events/${eventId}/reminders`, {
        minutes_before: minutes,
        occurrence_at: occurrenceAt ?? undefined,
      })
      showToast(t('event.reminder.added'), 'success')
      await refresh(eventId, occurrenceAt)
    } catch (e) {
      const msg = (e as Error)?.message ?? t('event.reminder.failed')
      showToast(msg, 'error')
    }
  }

  const remove = async (minutes: number) => {
    try {
      const params = new URLSearchParams({ minutes_before: String(minutes) })
      if (occurrenceAt) params.set('occurrence_at', occurrenceAt)
      await api.delete(
        `/api/calendars/events/${eventId}/reminders?${params.toString()}`,
      )
      showToast(t('event.reminder.removed'), 'success')
      await refresh(eventId, occurrenceAt)
    } catch (e) {
      const msg = (e as Error)?.message ?? t('event.reminder.failed')
      showToast(msg, 'error')
    }
  }

  return (
    <section class="sh-reminder-picker" aria-label={t('event.reminder.aria')}>
      <h4 class="sh-reminder-picker-heading">
        <span aria-hidden="true">🔔</span> {t('event.reminder.heading')}
      </h4>
      <p class="sh-reminder-picker-help">{t('event.reminder.help')}</p>

      {rows.length > 0 && (
        <ul class="sh-reminder-list">
          {rows
            .slice()
            .sort((a, b) => a.minutes_before - b.minutes_before)
            .map((r) => (
              <li key={r.minutes_before} class="sh-reminder-row">
                <span class="sh-reminder-label">
                  {humanizeMinutes(r.minutes_before)}
                </span>
                <button
                  type="button"
                  class="sh-reminder-remove"
                  aria-label={t('event.reminder.remove_aria', {
                    minutes: humanizeMinutes(r.minutes_before),
                  })}
                  onClick={() => remove(r.minutes_before)}
                >
                  ×
                </button>
              </li>
            ))}
        </ul>
      )}

      <div class="sh-reminder-presets" role="group" aria-label={t('event.reminder.presets_aria')}>
        {PRESETS.map((p) => (
          <button
            key={p.minutes}
            type="button"
            class={`sh-reminder-chip${
              setMinutes.has(p.minutes) ? ' sh-reminder-chip--active' : ''
            }`}
            onClick={() =>
              setMinutes.has(p.minutes) ? remove(p.minutes) : add(p.minutes)
            }
          >
            {t(p.i18nKey)}
          </button>
        ))}
        {!showCustom ? (
          <button
            type="button"
            class="sh-reminder-chip sh-reminder-chip--custom"
            onClick={() => setShowCustom(true)}
          >
            {t('event.reminder.preset.custom')}
          </button>
        ) : (
          <span class="sh-reminder-custom">
            <input
              type="number"
              min={0}
              step={5}
              value={customMinutes}
              onInput={(e) =>
                setCustomMinutes((e.target as HTMLInputElement).value)
              }
              aria-label={t('event.reminder.custom_aria')}
              class="sh-reminder-custom-input"
            />
            <Button
              variant="primary"
              onClick={async () => {
                const n = parseInt(customMinutes, 10)
                if (Number.isNaN(n) || n < 0) {
                  showToast(t('event.reminder.invalid'), 'error')
                  return
                }
                await add(n)
                setShowCustom(false)
              }}
            >
              {t('event.reminder.add')}
            </Button>
          </span>
        )}
      </div>
    </section>
  )
}

function humanizeMinutes(n: number): string {
  if (n === 0) return t('event.reminder.preset.at_start')
  if (n < 60) return t('event.reminder.minutes_label', { n: String(n) })
  if (n < 1440) {
    const hours = Math.round(n / 60)
    return t('event.reminder.hours_label', { n: String(hours) })
  }
  if (n < 10080) {
    const days = Math.round(n / 1440)
    return t('event.reminder.days_label', { n: String(days) })
  }
  const weeks = Math.round(n / 10080)
  return t('event.reminder.weeks_label', { n: String(weeks) })
}
