import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { useTitle } from '@/store/pageTitle'
import type { CalendarEvent } from '@/types'
import { CalendarSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import {
  CalendarEventDialog,
  openEventDialog,
  openEditEventDialog,
} from '@/components/CalendarEventDialog'
import { CalendarFilterStrip } from '@/components/CalendarFilterStrip'
import { CapacityStrip } from '@/components/CapacityStrip'
import { EventOverflowMenu } from '@/components/EventOverflowMenu'
import { EventRowMeta } from '@/components/EventRowMeta'
import { LocationLink } from '@/components/LocationLink'
import { HostApprovalQueue } from '@/components/HostApprovalQueue'
import { ReminderPicker } from '@/components/ReminderPicker'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { events, rsvpCounts, myRsvpStatus, activeCalendarScope } from '@/store/calendar'
import {
  advanceDate, calendarHue, dateRangeForMode, formatDayLabel, formatEventBounds,
  formatRangeHeading, groupEventsByDay, groupSharedEvents, resolveCalendarColor,
  type CalendarViewMode,
} from '@/utils/calendar'
import { t } from '@/i18n/i18n'
import { confirmDialog } from '@/components/confirm'

interface CalendarSummary {
  id: string
  name: string
  owner_username: string
  color?: string
}

/** localStorage key for "which calendars does this user want visible
 *  by default". Scoped per user_id so two members on the same browser
 *  (rare, but possible) don't trample each other. */
function visibilityKey(userId: string): string {
  return `sh-cal-visible:${userId}`
}

function loadVisibilityPrefs(userId: string): Set<string> | null {
  try {
    const raw = localStorage.getItem(visibilityKey(userId))
    if (!raw) return null
    const ids = JSON.parse(raw) as string[]
    if (!Array.isArray(ids)) return null
    return new Set(ids.filter(s => typeof s === 'string'))
  } catch {
    return null
  }
}

function saveVisibilityPrefs(userId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(visibilityKey(userId), JSON.stringify(Array.from(ids)))
  } catch {
    // localStorage unavailable / full — silently degrade. The session
    // still works; defaults just re-apply on the next reload.
  }
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
/** Which agenda row is expanded, as ``"<dayKey>:<eventId>"``. Keyed
 *  by the DAY CARD, not the event: a multi-day event renders one row
 *  per covered day, so an event-id key would expand all of them at
 *  once when the user clicks Saturday. */
const selectedRow = signal<string | null>(null)
const rsvpPending = signal<string | null>(null)

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
    // Merge rows that the composer fanned out across multiple
    // calendars — same event, different rows. See
    // :func:`groupSharedEvents` for the group key and rationale.
    events.value = groupSharedEvents(responses.flat(), calendars.value)
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

function showAllCalendars() {
  const next = new Set(calendars.value.map(c => c.id))
  visibleCalendarIds.value = next
  activeCalendarScope.value = next
  const uid = currentUser.value?.user_id
  if (uid) saveVisibilityPrefs(uid, next)
  void loadEvents()
}

function showOnlyMine() {
  const me = currentUser.value?.username
  const next = new Set(
    me
      ? calendars.value.filter(c => c.owner_username === me).map(c => c.id)
      : [],
  )
  if (next.size === 0 && calendars.value.length > 0) {
    next.add(calendars.value[0].id)
  }
  visibleCalendarIds.value = next
  activeCalendarScope.value = next
  const uid = currentUser.value?.user_id
  if (uid) saveVisibilityPrefs(uid, next)
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
        // by clicking their picker chip. Previously-saved preferences
        // (per-user, in localStorage) override the default so an
        // overlay choice survives reloads.
        const me = currentUser.value?.username
        const uid = currentUser.value?.user_id
        const myCals = me ? cals.filter(c => c.owner_username === me) : []
        const calIdSet = new Set(cals.map(c => c.id))
        const saved = uid ? loadVisibilityPrefs(uid) : null
        let initial: Set<string>
        if (saved) {
          // Filter out stale ids (calendars that disappeared since the
          // pref was saved). Fall back to defaults if filtering left
          // nothing.
          const kept = new Set([...saved].filter(id => calIdSet.has(id)))
          initial = kept.size > 0
            ? kept
            : new Set(myCals.length > 0 ? myCals.map(c => c.id) : [cals[0].id])
        } else {
          initial = new Set(
            myCals.length > 0 ? myCals.map(c => c.id) : [cals[0].id],
          )
        }
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
    if (!await confirmDialog('Delete this event?', { destructive: true })) return
    try {
      await api.delete(`/api/calendars/events/${eventId}`)
      showToast('Event deleted', 'success')
      selectedRow.value = null
      await loadEvents()
    } catch (err: unknown) {
      showToast(`Delete failed: ${(err as Error).message ?? err}`, 'error')
    }
  }

  const handleEdit = (evt: CalendarEvent) => {
    // Open the full event dialog in edit mode rather than the
    // single-field ``prompt()`` it used to be — every field becomes
    // editable, including attendees, the RSVP toggle, and the "For:"
    // target calendar (so an event can be moved between members'
    // calendars without delete-and-recreate).
    selectedRow.value = null
    openEditEventDialog(
      {
        id: evt.id,
        calendar_id: evt.calendar_id,
        summary: evt.summary,
        description: evt.description,
        start: evt.start,
        end: evt.end,
        all_day: evt.all_day,
        attendees: evt.attendees,
        rsvp_enabled: evt.rsvp_enabled,
        location: evt.location,
        // Without ``cover_url`` the edit form would open with a blank
        // cover and full-sync would propagate the blank to every copy.
        cover_url: evt.cover_url,
        // Recurrence rule. Without it a newly-ticked member would get
        // a one-off on whichever occurrence was open instead of the
        // series, and the dialog couldn't warn that changing the date
        // of an expanded occurrence moves every occurrence.
        rrule: evt.rrule,
        // Anchor the form's date / time inputs to the event's tz.
        tz: evt.tz,
        // Group identity + the underlying copies, so the picker
        // pre-ticks every member who already holds one and full-sync
        // can PATCH / POST / DELETE the right rows.
        //
        // ``copies`` is the AUTHORITATIVE set: the server computes it
        // per event, independent of which calendars the viewer has
        // overlaid. The parallel ``_grouped_*`` arrays are a
        // render-time artifact of ``groupSharedEvents`` over the
        // VISIBLE calendars only — kept as the fallback for uuid-less
        // legacy / ICS rows, where the server returns ``copies: []``
        // and the agenda's content-key grouping is all we have.
        client_event_uuid: evt.client_event_uuid,
        copies: evt.copies,
        grouped_calendar_ids: evt._grouped_calendar_ids,
        grouped_event_ids: evt._grouped_event_ids,
      },
      calendars.value,
    )
  }

  if (loading.value) return <CalendarSkeleton />

  // Pass the visible range so the overlap-based server query (which
  // legitimately returns an event that started before the period) can't
  // file a stray out-of-range day card — an April card in a May view.
  const grouped = groupEventsByDay(
    events.value,
    dateRangeForMode(currentDate.value, viewMode.value),
  )
  // Keys are ``YYYY-MM-DD`` (see ``groupEventsByDay``) so a plain
  // lexicographic sort is chronological — no locale-fragile
  // ``new Date(key)`` round-trip required.
  const dayKeys = Object.keys(grouped).sort()

  const setVisible = (next: Set<string>) => {
    visibleCalendarIds.value = next
    activeCalendarScope.value = next
    const uid = currentUser.value?.user_id
    if (uid) saveVisibilityPrefs(uid, next)
    void loadEvents()
  }

  return (
    <div class="sh-calendar">
      <CalendarFilterStrip
        calendars={calendars.value}
        visibleCalendarIds={visibleCalendarIds.value}
        onChange={setVisible}
        onShowAll={showAllCalendars}
        onShowOnlyMine={showOnlyMine}
        primaryAction={
          <Button onClick={handleNewEvent}>+ New event</Button>
        }
      />

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
          <div aria-hidden="true">📅</div>
          <h3>No events in this {viewMode.value}</h3>
          <p>
            Birthdays, school runs, vet visits, the trip you're planning —
            anything the household needs to keep track of.
          </p>
          <Button onClick={handleNewEvent}>
            + Create your first event
          </Button>
        </div>
      )}

      {dayKeys.map(dayKey => {
        const friendly = formatDayLabel(dayKey)
        return (
        <div key={dayKey} class="sh-calendar-day-group">
          <h3
            class={
              friendly.isToday
                ? 'sh-calendar-day-heading sh-calendar-day-heading--today'
                : 'sh-calendar-day-heading'
            }
          >
            {friendly.long}
            {friendly.relative && (
              <span class="sh-calendar-day-heading__rel">{friendly.relative}</span>
            )}
          </h3>
          {grouped[dayKey].map(entry => {
            // ``e`` stays bound to the underlying event row so
            // everything below (owner chips, RSVP gating, edit /
            // delete, reminders) reads unchanged; ``entry`` only
            // carries this day card's place in the span.
            const e = entry.event
            const rowKey = `${dayKey}:${e.id}`
            // Disclosure wiring: the header is a native <button> and the
            // detail panel is its SIBLING (it holds Edit / Delete / RSVP /
            // ReminderPicker — interactive elements nested inside a button
            // would be invalid HTML and break keyboard traversal). The id
            // is derived from the same parts as ``rowKey`` but kept a clean
            // token (no ``:``) so it reads well in ``aria-controls``.
            const isOpen = selectedRow.value === rowKey
            const detailId = `sh-event-detail-${dayKey}-${e.id}`
            // Owner byline / chips — one chip per DISTINCT owner of a
            // copy, so a household dinner reads as one row "YOU · BOB"
            // instead of two stacked rows. Each chip carries its OWN
            // calendar's hue so the sharing reads as ``YOU
            // (terracotta) · BOB (moss)`` rather than two
            // same-coloured pills.
            //
            // The source is ``copies`` — the server's authoritative,
            // visibility-INDEPENDENT copy set. It used to be the
            // ``_grouped_*`` render artifact behind a "more than one
            // calendar visible" gate, which meant a shared event was
            // silently indistinguishable from a personal one whenever
            // the viewer had only their own calendar on. That hidden
            // sharing was half the "I tick the other members and
            // nothing happens" bug. ``_grouped_*`` stays as the
            // fallback for uuid-less legacy / ICS rows and space
            // events, where the server sends ``copies: []``.
            const groupedCalIds =
              (e.copies?.length ? e.copies.map(c => c.calendar_id) : null)
              ?? e._grouped_calendar_ids ?? [e.calendar_id]
            // Owner carried on the copy itself — the defensive fallback
            // for a calendar id missing from ``calendars`` (shouldn't
            // happen: /api/calendars returns the whole household).
            const copyOwners = new Map(
              (e.copies ?? []).map(c => [c.calendar_id, c.owner_username]),
            )
            const ownerChips: { name: string; color: string }[] = []
            const me = currentUser.value?.username
            const seen = new Set<string>()
            for (const calId of groupedCalIds) {
              const cal = calendars.value.find(c => c.id === calId)
              const owner = cal?.owner_username ?? copyOwners.get(calId)
              if (!owner) continue
              if (seen.has(owner)) continue
              seen.add(owner)
              // Neutral ink when the calendar row isn't on hand — a
              // hashed hue would imply a colour the picker never shows.
              const color = cal
                ? resolveCalendarColor(cal)
                : 'var(--sh-text-muted)'
              if (owner === me) {
                ownerChips.push({ name: 'You', color })
                continue
              }
              let name: string = owner
              for (const u of householdUsers.value.values()) {
                if (u.username === owner) {
                  name = u.display_name || u.username
                  break
                }
              }
              ownerChips.push({ name, color })
            }
            // Chips earn their place when they add information: more
            // than one owner holds a copy (the event is shared), or
            // more than one calendar is overlaid (the byline says whose
            // row this is — hue alone is a thin accent, two household
            // calendars can sit close together, and it's inaccessible
            // to anyone who can't discriminate them). With a single
            // owner AND a single calendar on screen the owner is
            // implied, so a lone "You" would be noise.
            const showOwnerChips =
              ownerChips.length > 1 || visibleCalendarIds.value.size > 1
            const shownChips = showOwnerChips ? ownerChips : []
            return (
            <div key={rowKey}
                 class={
                   'sh-event'
                   + (entry.isFirst ? '' : ' sh-event--continued')
                   + (entry.isLast ? '' : ' sh-event--continues')
                 }
                 style={{ '--cal-hue': (() => {
                   const cal = calendars.value.find(c => c.id === e.calendar_id)
                   return cal ? resolveCalendarColor(cal) : calendarHue(e.calendar_id)
                 })() } as Record<string, string>}>
              <button
                type="button"
                class="sh-event-header"
                aria-expanded={isOpen}
                aria-controls={detailId}
                onClick={() => {
                  selectedRow.value = isOpen ? null : rowKey
                }}
              >
                {e.cover_url && (
                  <img
                    class="sh-event-cover-thumb"
                    src={e.cover_url}
                    alt=""
                    loading="lazy"
                  />
                )}
                <strong>{e.summary}</strong>
                {shownChips.length > 0 && (
                  <span
                    class="sh-event-owner-chips"
                    aria-label={
                      shownChips.length === 1
                        ? `On ${shownChips[0].name}'s calendar`
                        : `Shared with ${shownChips.map(c => c.name).join(', ')}`
                    }
                  >
                    {shownChips.map(chip => (
                      <span
                        key={chip.name}
                        class="sh-event-owner"
                        style={{ '--cal-hue': chip.color } as Record<string, string>}
                      >
                        {chip.name}
                      </span>
                    ))}
                  </span>
                )}
                <EventRowMeta entry={entry}>
                  {e.location && (
                    // Compact "where" hint on the collapsed row. The full
                    // location + maps link only renders inside the expanded
                    // detail to keep the row visually quiet.
                    <span
                      class="sh-event-row-locpin"
                      aria-label={`Location: ${e.location}`}
                      title={e.location}
                    >
                      📍
                    </span>
                  )}
                </EventRowMeta>
              </button>
              {isOpen && (() => {
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
                // One call, two labels — the helper builds two Dates
                // and up to two Intl formatters per invocation.
                const bounds = formatEventBounds(e)
                return (
                <div class="sh-event-detail" id={detailId}>
                  {e.location && (
                    <p class="sh-event-location-row">
                      <LocationLink
                        value={e.location}
                        className="sh-event-location"
                      />
                    </p>
                  )}
                  {e.description && <p>{e.description}</p>}
                  <div class="sh-event-times">
                    <span>{t('event.starts')} {bounds.starts}</span>
                    <span>{t('event.ends')} {bounds.ends}</span>
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
                    <Button variant="secondary" onClick={() => handleEdit(e)}>
                      {t('event.edit')}
                    </Button>
                    <Button variant="danger" onClick={() => handleDelete(e.id)}>
                      {t('event.delete')}
                    </Button>
                    <EventOverflowMenu eventId={e.id} />
                  </div>
                </div>
                )
              })()}
            </div>
          )})}
        </div>
        )
      })}

      <CalendarEventDialog onCreated={(newCopyIds) => {
        // The dialog reports only the calendars that received a NEW
        // copy (``null`` for the space path, which has no household
        // visibility to manage). Those get switched on, so the new
        // copies land on screen instead of seemingly disappearing —
        // and the reveal is persisted, since it used to be in-memory
        // only and silently reverted on the next reload.
        //
        // Calendars that were merely PATCHed are deliberately NOT
        // revealed: a user who hid Bob's calendar shouldn't get it
        // switched back on — and saved — just because they re-saved a
        // shared event that happens to include Bob. An empty array
        // therefore leaves visibility (and the saved prefs) untouched.
        if (newCopyIds && newCopyIds.length > 0) {
          const next = new Set(visibleCalendarIds.value)
          const before = next.size
          for (const id of newCopyIds) next.add(id)
          if (next.size !== before) {
            visibleCalendarIds.value = next
            activeCalendarScope.value = next
            const uid = currentUser.value?.user_id
            if (uid) saveVisibilityPrefs(uid, next)
          }
        }
        void loadEvents()
      }} />
    </div>
  )
}
