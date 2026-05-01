import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { useTitle } from '@/store/pageTitle'
import type { CalendarEvent } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { CalendarEventDialog, openEventDialog } from '@/components/CalendarEventDialog'
import { CapacityStrip } from '@/components/CapacityStrip'
import { HostApprovalQueue } from '@/components/HostApprovalQueue'
import { ReminderPicker } from '@/components/ReminderPicker'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { events, rsvpCounts, myRsvpStatus, activeCalendarScope } from '@/store/calendar'
import {
  advanceDate, dateRangeForMode, formatRangeHeading, groupEventsByDay,
  type CalendarViewMode,
} from '@/utils/calendar'
import { t } from '@/i18n/i18n'

interface CalendarSummary {
  id: string
  name: string
  owner_username: string
}

const loading = signal(true)
const viewMode = signal<CalendarViewMode>('month')
const calendarId = signal<string>('')
const calendars = signal<CalendarSummary[]>([])
const currentDate = signal(new Date())
const selectedEvent = signal<CalendarEvent | null>(null)
const rsvpPending = signal<string | null>(null)

async function loadEvents() {
  if (!calendarId.value) return
  loading.value = true
  const { start, end } = dateRangeForMode(currentDate.value, viewMode.value)
  try {
    const rows = await api.get(
      `/api/calendars/${calendarId.value}/events`, { start, end },
    ) as CalendarEvent[]
    events.value = rows
  } catch {
    events.value = []
  }
  loading.value = false
}

function navigateDate(direction: number) {
  currentDate.value = advanceDate(currentDate.value, direction, viewMode.value)
}

/** Lazily ensure a default household calendar exists.
 *
 * A fresh household starts with zero calendars and the SPA has no
 * "create calendar" surface — so until this PR, the "+ New event"
 * button hid until something else (a backend bootstrap, an admin
 * action) seeded one. Now we just create one on demand the first
 * time the user clicks New event. Returns the calendar id, caches
 * it in the module-level signal. */
async function ensureHouseholdCalendar(): Promise<string> {
  if (calendarId.value) return calendarId.value
  const cal = await api.post('/api/calendars', { name: 'Calendar' }) as
    CalendarSummary
  calendarId.value = cal.id
  activeCalendarScope.value = cal.id
  // Splice the freshly-created calendar into the picker list so the
  // dropdown reflects it without a full re-fetch.
  if (!calendars.value.some(c => c.id === cal.id)) {
    calendars.value = [...calendars.value, cal]
  }
  return cal.id
}

export default function CalendarPage() {
  useTitle('Calendar')
  useEffect(() => {
    // Drop any rows the WS handler accreted while we were on a
    // different surface — we'll re-fetch the right ones below.
    events.value = []
    void loadHouseholdUsers()
    api.get('/api/calendars', { scope: 'household' })
      .then(async (cals: CalendarSummary[]) => {
        calendars.value = cals
        if (cals.length === 0) {
          loading.value = false
          return
        }
        // Default to the caller's first calendar if they have one;
        // otherwise the first household calendar.
        const me = currentUser.value?.username
        const mine = me ? cals.find(c => c.owner_username === me) : null
        const pick = mine ?? cals[0]
        calendarId.value = pick.id
        activeCalendarScope.value = pick.id
        await loadEvents()
        loading.value = false
      })
      .catch(() => { loading.value = false })
    return () => {
      // Stop accepting WS frames into the household ``events`` cache
      // once the user navigates away — without this, a per-space
      // calendar.* broadcast in the background would silently
      // re-pollute the cache between visits.
      activeCalendarScope.value = null
    }
  }, [])

  useEffect(() => {
    if (calendarId.value) {
      activeCalendarScope.value = calendarId.value
      loadEvents()
    }
  }, [viewMode.value, currentDate.value, calendarId.value])

  const handleNewEvent = async () => {
    try {
      const id = await ensureHouseholdCalendar()
      openEventDialog(id)
    } catch (e) {
      showToast(`Couldn't open new-event dialog: ${(e as Error).message}`, 'error')
    }
  }

  const handleRsvp = async (
    event: CalendarEvent,
    status: 'going' | 'maybe' | 'declined',
  ) => {
    const key = `${event.id}:${status}`
    rsvpPending.value = key
    try {
      // Phase A — RSVP lives at the event-level path (no calendar
      // segment); the server reads occurrence_at from the body for
      // recurring events.
      const body: Record<string, unknown> = { status }
      if (event.rrule) body.occurrence_at = event.start
      await api.post(`/api/calendars/events/${event.id}/rsvp`, body)
      // Optimistic local update — corrected by next WS frame.
      const isCapped = event.capacity != null
      const landing = isCapped && status === 'going' ? 'requested' : status
      myRsvpStatus.value = { ...myRsvpStatus.value, [event.id]: landing }
      showToast(t(`event.rsvp.${landing}_toast`), 'success')
    } catch (err) {
      const msg = (err as Error)?.message ?? t('event.rsvp.failed')
      showToast(msg, 'error')
    } finally {
      if (rsvpPending.value === key) rsvpPending.value = null
    }
  }

  const isEventEnded = (event: CalendarEvent): boolean => {
    const end = new Date(event.end).getTime()
    return end < Date.now()
  }

  const handleDelete = async (eventId: string) => {
    if (!confirm('Delete this event?')) return
    try {
      await api.delete(`/api/calendars/events/${eventId}`)
      showToast('Event deleted', 'success')
      selectedEvent.value = null
      await loadEvents()
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const handleEdit = async (evt: CalendarEvent) => {
    const newSummary = prompt('Edit event title:', evt.summary)
    if (newSummary == null) return
    const trimmed = newSummary.trim()
    if (!trimmed) {
      showToast('Title cannot be empty', 'error')
      return
    }
    try {
      await api.patch(`/api/calendars/events/${evt.id}`, { summary: trimmed })
      showToast('Event updated', 'success')
      selectedEvent.value = null
      await loadEvents()
    } catch (err: unknown) {
      showToast(`Update failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  if (loading.value) return <Spinner />

  const grouped = groupEventsByDay(events.value)
  const dayKeys = Object.keys(grouped).sort((a, b) => new Date(a).getTime() - new Date(b).getTime())

  return (
    <div class="sh-calendar">
      <div class="sh-page-header">
        <Button onClick={handleNewEvent}>+ New event</Button>
      </div>

      <div class="sh-calendar-controls">
        <div class="sh-calendar-nav">
          <Button variant="secondary"
                  aria-label={`Previous ${viewMode.value}`}
                  onClick={() => navigateDate(-1)}>&#8249;</Button>
          <span class="sh-calendar-heading">{formatRangeHeading(currentDate.value, viewMode.value)}</span>
          <Button variant="secondary"
                  aria-label={`Next ${viewMode.value}`}
                  onClick={() => navigateDate(1)}>&#8250;</Button>
          <Button variant="secondary" onClick={() => { currentDate.value = new Date() }}>Today</Button>
        </div>
        {calendars.value.length > 1 && (
          <label class="sh-calendar-picker">
            <span class="sh-muted">Calendar</span>
            <select
              value={calendarId.value}
              onChange={(ev) => {
                const id = (ev.target as HTMLSelectElement).value
                calendarId.value = id
                activeCalendarScope.value = id
              }}
            >
              {calendars.value.map(c => {
                // householdUsers is keyed by user_id; the calendar
                // carries the owner's username, so iterate to map back.
                let ownerLabel = c.owner_username
                for (const u of householdUsers.value.values()) {
                  if (u.username === c.owner_username) {
                    ownerLabel = u.display_name || u.username
                    break
                  }
                }
                const mine = c.owner_username === currentUser.value?.username
                return (
                  <option key={c.id} value={c.id}>
                    {mine ? c.name : `${ownerLabel} · ${c.name}`}
                  </option>
                )
              })}
            </select>
          </label>
        )}
        <div class="sh-calendar-views" role="tablist">
          {(['month', 'week', 'day'] as CalendarViewMode[]).map(mode => (
            <button
              key={mode}
              type="button"
              role="tab"
              aria-selected={viewMode.value === mode}
              class={viewMode.value === mode ? 'sh-tab sh-tab--active' : 'sh-tab'}
              onClick={() => { viewMode.value = mode }}
            >
              {mode.charAt(0).toUpperCase() + mode.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {events.value.length === 0 && (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2rem' }}>📅</div>
          <h3>No events in this {viewMode.value}</h3>
          <p>Click <strong>+ New event</strong> to schedule something.</p>
        </div>
      )}

      {dayKeys.map(dayKey => (
        <div key={dayKey} class="sh-calendar-day-group">
          <h3 class="sh-calendar-day-heading">{dayKey}</h3>
          {grouped[dayKey].map(e => (
            <div key={e.id} class="sh-event" onClick={() => { selectedEvent.value = selectedEvent.value?.id === e.id ? null : e }}>
              <div class="sh-event-header">
                <strong>{e.summary}</strong>
                <time>{new Date(e.start).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</time>
                {e.all_day && <span class="sh-badge">All day</span>}
              </div>
              {selectedEvent.value?.id === e.id && (
                <div class="sh-event-detail">
                  {e.description && <p>{e.description}</p>}
                  <div class="sh-event-times">
                    <span>{t('event.starts')} {new Date(e.start).toLocaleString()}</span>
                    <span>{t('event.ends')} {new Date(e.end).toLocaleString()}</span>
                  </div>

                  <CapacityStrip
                    counts={rsvpCounts.value[e.id]}
                    capacity={e.capacity}
                    myStatus={
                      (myRsvpStatus.value[e.id] ?? null) as
                        | 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist' | null
                    }
                  />

                  <div class="sh-event-rsvp" role="group" aria-label={t('event.rsvp.aria')}>
                    {(['going', 'maybe', 'declined'] as const).map((status) => {
                      const ended = isEventEnded(e)
                      const isCapped = e.capacity != null
                      const labelKey = isCapped && status === 'going'
                        ? 'event.rsvp.request_to_join'
                        : `event.rsvp.${status}`
                      const ariaTip = ended ? t('event.has_ended_tooltip') : ''
                      return (
                        <Button
                          key={status}
                          variant={status === 'going' ? 'primary' : 'secondary'}
                          loading={rsvpPending.value === `${e.id}:${status}`}
                          disabled={ended}
                          title={ariaTip || undefined}
                          onClick={() => handleRsvp(e, status)}
                        >
                          {t(labelKey)}
                        </Button>
                      )
                    })}
                  </div>

                  <ReminderPicker
                    eventId={e.id}
                    occurrenceAt={e.rrule ? e.start : null}
                  />

                  {e.capacity != null && (
                    e.created_by === currentUser.value?.user_id
                      || currentUser.value?.is_admin
                  ) && (
                    <HostApprovalQueue
                      eventId={e.id}
                      spaceId={null}
                      occurrenceAt={e.rrule ? e.start : null}
                    />
                  )}

                  <div class="sh-event-admin sh-row">
                    <a
                      class="sh-btn sh-btn--ghost"
                      href={`/api/calendars/events/${e.id}/export.ics`}
                      download
                    >
                      📥 {t('event.add_to_calendar')}
                    </a>
                    <Button variant="secondary" onClick={() => handleEdit(e)}>
                      {t('event.edit')}
                    </Button>
                    <Button variant="danger" onClick={() => handleDelete(e.id)}>
                      {t('event.delete')}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      ))}

      <CalendarEventDialog onCreated={() => loadEvents()} />
    </div>
  )
}
