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
/** Calendar id used as the "write target" — the calendar the +New-
 *  event dialog writes to. Always the caller's own calendar; doesn't
 *  change when they overlay another member's calendar. */
const writeCalendarId = signal<string>('')
/** Set of calendar ids currently overlaid in the view. Members can
 *  toggle each other's calendars on/off via the picker chips. */
const visibleCalendarIds = signal<Set<string>>(new Set())
const calendars = signal<CalendarSummary[]>([])
const currentDate = signal(new Date())
const selectedEvent = signal<CalendarEvent | null>(null)
const rsvpPending = signal<string | null>(null)

/** Deterministic colour per calendar id — picks one of 8 hand-tuned
 *  earth-tone hues so two members never collide visually. The same
 *  id always lands on the same colour across reloads / sessions. */
const _CAL_HUES = [
  'var(--sh-primary)',                               // terracotta
  'var(--sh-success)',                               // moss
  'var(--sh-warning)',                               // honey
  'var(--sh-danger)',                                // brick
  '#7B5BA8',                                         // plum
  '#3F7B8C',                                         // dusty teal
  '#A89344',                                         // ochre
  '#5C7B5A',                                         // sage
] as const
function calendarHue(calId: string): string {
  // Tiny string-hash → pick a hue. djb2-flavoured.
  let h = 5381
  for (let i = 0; i < calId.length; i++) {
    h = ((h << 5) + h + calId.charCodeAt(i)) | 0
  }
  return _CAL_HUES[Math.abs(h) % _CAL_HUES.length]
}

async function loadEvents() {
  const ids = Array.from(visibleCalendarIds.value)
  if (ids.length === 0) {
    events.value = []
    loading.value = false
    return
  }
  loading.value = true
  const { start, end } = dateRangeForMode(currentDate.value, viewMode.value)
  try {
    const responses = await Promise.all(ids.map(id =>
      api.get(`/api/calendars/${id}/events`, { start, end })
        .catch(() => [] as CalendarEvent[]) as Promise<CalendarEvent[]>,
    ))
    events.value = responses.flat()
  } catch {
    events.value = []
  }
  loading.value = false
}

function navigateDate(direction: number) {
  currentDate.value = advanceDate(currentDate.value, direction, viewMode.value)
}

/** Lazily ensure a default household calendar exists for the caller.
 *  A fresh user starts with zero personal calendars and the SPA has
 *  no "create calendar" surface — without this, the "+ New event"
 *  button would have nowhere to write. Returns the caller's calendar
 *  id, caches it in ``writeCalendarId``. */
async function ensureHouseholdCalendar(): Promise<string> {
  if (writeCalendarId.value) return writeCalendarId.value
  const cal = await api.post('/api/calendars', { name: 'Calendar' }) as
    CalendarSummary
  writeCalendarId.value = cal.id
  // Splice the freshly-created calendar into the picker list and the
  // visible set so the new event lands on screen immediately.
  if (!calendars.value.some(c => c.id === cal.id)) {
    calendars.value = [...calendars.value, cal]
  }
  const next = new Set(visibleCalendarIds.value)
  next.add(cal.id)
  visibleCalendarIds.value = next
  activeCalendarScope.value = next
  return cal.id
}

function toggleCalendarVisible(calId: string) {
  const next = new Set(visibleCalendarIds.value)
  if (next.has(calId)) {
    if (next.size === 1) return  // never let the user hide everything
    next.delete(calId)
  } else {
    next.add(calId)
  }
  visibleCalendarIds.value = next
  activeCalendarScope.value = next
  void loadEvents()
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
        // Default visibility: the caller's own calendar(s) only.
        // Other members' calendars start hidden — the user opts in
        // by clicking their picker chip.
        const me = currentUser.value?.username
        const myCals = me ? cals.filter(c => c.owner_username === me) : []
        const initial = new Set(
          myCals.length > 0 ? myCals.map(c => c.id) : [cals[0].id],
        )
        // First own-calendar (alphabetical by name from the server)
        // is the write target for + New event.
        const writeTarget = myCals[0] ?? cals[0]
        writeCalendarId.value = writeTarget.id
        visibleCalendarIds.value = initial
        activeCalendarScope.value = initial
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
    if (visibleCalendarIds.value.size > 0) {
      activeCalendarScope.value = visibleCalendarIds.value
      loadEvents()
    }
  }, [viewMode.value, currentDate.value])

  const handleNewEvent = async () => {
    try {
      const id = await ensureHouseholdCalendar()
      // Pass the full household-calendar list so the dialog can show
      // a "For:" selector and let the caller redirect the event onto
      // someone else's calendar (e.g. Maria can put a doctor's
      // appointment directly on Pascal's calendar).
      openEventDialog(id, calendars.value)
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
          <div class="sh-calendar-picker"
               role="group"
               aria-label="Visible calendars">
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
              const checked = visibleCalendarIds.value.has(c.id)
              const hue = calendarHue(c.id)
              return (
                <button
                  key={c.id}
                  type="button"
                  class={
                    checked
                      ? 'sh-calendar-picker__chip sh-calendar-picker__chip--on'
                      : 'sh-calendar-picker__chip'
                  }
                  aria-pressed={checked}
                  aria-label={`${mine ? c.name : `${ownerLabel} · ${c.name}`}: ${checked ? 'visible' : 'hidden'}`}
                  style={{ '--cal-hue': hue } as Record<string, string>}
                  onClick={() => toggleCalendarVisible(c.id)}
                >
                  <span class="sh-calendar-picker__dot" aria-hidden="true" />
                  <span>{mine ? 'You' : ownerLabel}</span>
                </button>
              )
            })}
          </div>
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
            <div key={e.id} class="sh-event"
                 style={{ '--cal-hue': calendarHue(e.calendar_id) } as Record<string, string>}
                 onClick={() => { selectedEvent.value = selectedEvent.value?.id === e.id ? null : e }}>
              <div class="sh-event-header">
                <strong>{e.summary}</strong>
                <time>{new Date(e.start).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</time>
                {e.all_day && <span class="sh-badge">All day</span>}
              </div>
              {selectedEvent.value?.id === e.id && (() => {
                // RSVP visibility: only when explicitly enabled
                // (``rsvp_enabled``) OR when there's a capacity cap
                // (the legacy Phase C signal that an event needs
                // confirmation). And even then, hide the buttons for
                // the creator viewing their own one-attendee event —
                // there's nobody to ask.
                const myUid = currentUser.value?.user_id
                const others = (e.attendees ?? []).filter(uid => uid !== myUid)
                const hasOthers = others.length > 0
                const isCapped = e.capacity != null
                const showRsvp = (e.rsvp_enabled || isCapped)
                  && hasOthers
                  && (e.attendees ?? []).includes(myUid ?? '__none__')
                return (
                <div class="sh-event-detail">
                  {e.description && <p>{e.description}</p>}
                  <div class="sh-event-times">
                    <span>{t('event.starts')} {new Date(e.start).toLocaleString()}</span>
                    <span>{t('event.ends')} {new Date(e.end).toLocaleString()}</span>
                  </div>

                  {showRsvp && (
                    <CapacityStrip
                      counts={rsvpCounts.value[e.id]}
                      capacity={e.capacity}
                      myStatus={
                        (myRsvpStatus.value[e.id] ?? null) as
                          | 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist' | null
                      }
                    />
                  )}

                  {showRsvp && (
                    <div class="sh-event-rsvp" role="group" aria-label={t('event.rsvp.aria')}>
                      {(['going', 'maybe', 'declined'] as const).map((status) => {
                        const ended = isEventEnded(e)
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
                  )}

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
                )
              })()}
            </div>
          ))}
        </div>
      ))}

      <CalendarEventDialog onCreated={(targetId) => {
        // If the user created the event on a calendar that isn't
        // currently overlaid (typically: "For: Pascal" while only
        // Maria's chip was on), auto-toggle that calendar visible so
        // the new event lands on screen instead of seemingly
        // disappearing.
        if (targetId && !visibleCalendarIds.value.has(targetId)) {
          const next = new Set(visibleCalendarIds.value)
          next.add(targetId)
          visibleCalendarIds.value = next
          activeCalendarScope.value = next
        }
        void loadEvents()
      }} />
    </div>
  )
}
