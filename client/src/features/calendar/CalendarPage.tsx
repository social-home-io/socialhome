import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
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
import { CalendarImport, openCalendarImport } from '@/components/CalendarImport'
import { CapacityStrip } from '@/components/CapacityStrip'
import { CalendarGrid } from '@/components/CalendarGrid'
import { CalendarUpcoming } from '@/components/CalendarUpcoming'
import type { RowDecor } from '@/components/CalendarEventRow'
import { EventOverflowMenu } from '@/components/EventOverflowMenu'
import { LocationLink } from '@/components/LocationLink'
import { SegmentedSwitch } from '@/components/SegmentedSwitch'
import { HostApprovalQueue } from '@/components/HostApprovalQueue'
import { ReminderPicker } from '@/components/ReminderPicker'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { events, rsvpCounts, myRsvpStatus, activeCalendarScope } from '@/store/calendar'
import {
  advanceDate, calendarHue, formatEventBounds, formatRangeHeading, groupSharedEvents,
  resolveCalendarColor,
} from '@/utils/calendar'
import {
  CALENDAR_WIDE_QUERY, UPCOMING_WEEKS, gridRange, gridWeeks, upcomingRange, viewOnScreen,
  type CalendarView,
} from '@/utils/calendarViews'
import { currentWeekStart } from '@/utils/week'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { t } from '@/i18n/i18n'
import { confirmDialog } from '@/components/confirm'
import { TabHeader } from '@/components/TabHeader'
import { toggles } from '@/components/HouseholdToggles'
import TimetablePage from '@/features/timetable/TimetablePage'

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
/** Upcoming (default) · Month · Week (wide screens) — the same views as
 *  a space's Calendar (owner decision 9; the household page follows, no
 *  Day view). */
const viewMode = signal<CalendarView>('upcoming')
/** Calendar id used as the "write target" — the calendar the +New-
 *  event dialog writes to. Always the caller's own calendar; doesn't
 *  change when they overlay another member's calendar. */
const writeCalendarId = signal<string>('')
/** Set of calendar ids currently overlaid in the view. Members can
 *  toggle each other's calendars on/off via the picker chips. */
const visibleCalendarIds = signal<Set<string>>(new Set())
const calendars = signal<CalendarSummary[]>([])
const currentDate = signal(new Date())
const rsvpPending = signal<string | null>(null)

/** Is the screen wide enough for Week right now (outside a render)? */
function wideNow(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(CALENDAR_WIDE_QUERY).matches
}

/** The range a view reads: Upcoming is a rolling window from today;
 *  Month / Week the whole weeks the grid shows. */
function rangeFor(v: CalendarView): { start: string; end: string } {
  if (v === 'upcoming') return upcomingRange(new Date())
  return gridRange(gridWeeks(currentDate.value, v, currentWeekStart()))
}

async function loadEvents() {
  const ids = Array.from(visibleCalendarIds.value)
  if (ids.length === 0) {
    events.value = []
    loading.value = false
    return
  }
  loading.value = true
  const { start, end } = rangeFor(viewOnScreen(viewMode.value, wideNow()))
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

function navigateDate(direction: number, mode: 'month' | 'week') {
  currentDate.value = advanceDate(currentDate.value, direction, mode)
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

type CalendarTab = 'calendar' | 'timetable'

/** Tabs the household toggles allow, in display order. Toggles not
 *  loaded yet → everything on (no "tab appears" flash, like SideNav).
 *  With both features off the page still shows the calendar — it is
 *  only reachable by URL then. */
export function visibleCalendarTabs(
  t: { feat_calendar: boolean; feat_timetable: boolean } | null,
): CalendarTab[] {
  const tabs: CalendarTab[] = []
  if (!t || t.feat_calendar) tabs.push('calendar')
  if (!t || t.feat_timetable) tabs.push('timetable')
  return tabs.length > 0 ? tabs : ['calendar']
}

function tabFromUrl(url: string | undefined): CalendarTab {
  const q = (url ?? '').split('?')[1] ?? ''
  return new URLSearchParams(q).get('tab') === 'timetable' ? 'timetable' : 'calendar'
}

/**
 * CalendarPage — hosts the household calendar and, when the
 * ``feat_timetable`` toggle is on, the school Timetable as a second
 * tab (``?tab=timetable``; the default Calendar tab carries no query).
 * The tab strip only appears when the Timetable tab does, so a
 * household without timetables sees the calendar exactly as before.
 */
export default function CalendarPage() {
  const loc = useLocation()
  const tabs = visibleCalendarTabs(toggles.value)
  const wanted = tabFromUrl(loc.url)
  const active = tabs.includes(wanted) ? wanted : tabs[0]
  const onSelectTab = (tab: CalendarTab) => {
    const next = tab === 'calendar' ? '/calendar' : '/calendar?tab=timetable'
    if (loc.url !== next) loc.route?.(next, true)
  }
  const labels: Record<CalendarTab, string> = {
    calendar: t('nav.calendar'),
    timetable: t('nav.timetable'),
  }
  return (
    <div class="sh-calendar-host">
      {tabs.includes('timetable') && (
        <TabHeader<CalendarTab>
          activeTab={active}
          visibleTabs={tabs}
          labels={labels}
          ariaLabel={t('timetable.tabs_aria')}
          onSelectTab={onSelectTab}
        />
      )}
      {active === 'calendar' ? <CalendarAgenda /> : <TimetablePage />}
    </div>
  )
}

/** The calendar tab — household agenda, filters, event dialog. */
function CalendarAgenda() {
  useTitle(t('nav.calendar'))
  const wide = useMediaQuery(CALENDAR_WIDE_QUERY)
  // Week on a phone (a rotated tablet, a narrowed window) shows Month.
  const current = viewOnScreen(viewMode.value, wide)
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
  }, [current, currentDate.value])

  const handleNewEvent = async () => {
    try {
      const id = await ensureHouseholdCalendar()
      // Pass the full household-calendar list so the dialog can show
      // a "For:" selector and let the caller redirect the event onto
      // someone else's calendar (e.g. Maria can put a doctor's
      // appointment directly on Pascal's calendar).
      openEventDialog(id, calendars.value)
    } catch (e) {
      showToast(t('calendar.page.new_event_failed', { error: String((e as Error).message) }), 'error')
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
    if (!await confirmDialog(t('calendar.page.delete_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/calendars/events/${eventId}`)
      showToast(t('calendar.page.deleted'), 'success')
      await loadEvents()
    } catch (err: unknown) {
      showToast(t('calendar.page.delete_failed', { error: String((err as Error).message ?? err) }), 'error')
    }
  }

  const handleEdit = (evt: CalendarEvent) => {
    // Open the full event dialog in edit mode rather than the
    // single-field ``prompt()`` it used to be — every field becomes
    // editable, including attendees, the RSVP toggle, and the "For:"
    // target calendar (so an event can be moved between members'
    // calendars without delete-and-recreate).
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

  // After an import, make sure the target calendar is showing so the
  // new events are visible (it may be another member's calendar that
  // was toggled off in the strip), then refetch the range.
  const handleImported = (calendarId: string) => {
    if (!visibleCalendarIds.value.has(calendarId)) {
      const next = new Set(visibleCalendarIds.value)
      next.add(calendarId)
      setVisible(next)
      return
    }
    void loadEvents()
  }

  // "Show in calendar" after an import: Month view at the earliest
  // imported event — an .ics export usually covers other months.
  const showImported = (imported: { start: string }[]) => {
    const times = imported.map(e => Date.parse(e.start)).filter(t => !Number.isNaN(t))
    if (times.length > 0) {
      // Upcoming only looks ahead from today; Month shows the import.
      viewMode.value = 'month'
      currentDate.value = new Date(Math.min(...times))
    }
  }

  const setVisible = (next: Set<string>) => {
    visibleCalendarIds.value = next
    activeCalendarScope.value = next
    const uid = currentUser.value?.user_id
    if (uid) saveVisibilityPrefs(uid, next)
    void loadEvents()
  }

  // The import dialog sits first in a fragment that BOTH render paths
  // return, so it survives the loading skeleton an import's refetch
  // flips on (a remount would drop the result summary mid-read).
  const importHost = (
    <CalendarImport
      calendars={calendars.value}
      defaultCalendarId={writeCalendarId.value || null}
      ensureCalendar={ensureHouseholdCalendar}
      onImported={handleImported}
      onShow={showImported}
    />
  )

  if (loading.value) return <>{importHost}<CalendarSkeleton /></>

  const weekStart = currentWeekStart()
  const now = new Date()
  const me = currentUser.value?.username
  const myUid = currentUser.value?.user_id
  const calendarOf = (id: string) => calendars.value.find(c => c.id === id)

  /** Owner chips — one per DISTINCT owner of a copy, so a household
   *  dinner reads "YOU · BOB" instead of two rows; each in its own
   *  calendar's hue. The source is ``copies`` (the server's
   *  visibility-independent copy set); ``_grouped_*`` is the fallback for
   *  uuid-less legacy / ICS rows, where the server sends ``copies: []``. */
  const ownerChips = (e: CalendarEvent): { name: string; color: string }[] => {
    const groupedCalIds =
      (e.copies?.length ? e.copies.map(c => c.calendar_id) : null)
      ?? e._grouped_calendar_ids ?? [e.calendar_id]
    const copyOwners = new Map((e.copies ?? []).map(c => [c.calendar_id, c.owner_username]))
    const chips: { name: string; color: string }[] = []
    const seen = new Set<string>()
    for (const calId of groupedCalIds) {
      const cal = calendarOf(calId)
      const owner = cal?.owner_username ?? copyOwners.get(calId)
      if (!owner || seen.has(owner)) continue
      seen.add(owner)
      // Neutral ink when the calendar row isn't on hand — a hashed hue
      // would imply a colour the picker never shows.
      const color = cal ? resolveCalendarColor(cal) : 'var(--sh-text-muted)'
      if (owner === me) {
        chips.push({ name: t('calendar.page.owner_you'), color })
        continue
      }
      let name: string = owner
      for (const u of householdUsers.value.values()) {
        if (u.username === owner) { name = u.display_name || u.username; break }
      }
      chips.push({ name, color })
    }
    return chips
  }

  /** The row's rail takes its calendar's colour; the owner chips follow
   *  the title when they add information — more than one owner holds a
   *  copy (shared), or more than one calendar is overlaid (whose row this
   *  is). A lone "You" on your only calendar would be noise. */
  const decorate = (e: CalendarEvent): RowDecor => {
    const cal = calendarOf(e.calendar_id)
    const hue = cal ? resolveCalendarColor(cal) : calendarHue(e.calendar_id)
    const chips = ownerChips(e)
    const show = chips.length > 1 || visibleCalendarIds.value.size > 1
    if (!show || chips.length === 0) return { hue }
    return {
      hue,
      badges: (
        <span
          class="sh-event-owner-chips"
          aria-label={
            chips.length === 1
              ? t('calendar.page.on_calendar', { name: chips[0].name })
              : t('calendar.page.shared_with', { names: chips.map(c => c.name).join(', ') })
          }
        >
          {chips.map(chip => (
            <span key={chip.name} class="sh-event-owner"
                  style={{ '--cal-hue': chip.color } as Record<string, string>}>
              {chip.name}
            </span>
          ))}
        </span>
      ),
    }
  }

  /** What an opened row shows: where, the note, the times, RSVP,
   *  reminders, the host's approval queue, and Edit / Delete / ⋯. */
  const detail = (e: CalendarEvent) => {
    // RSVP only when explicitly enabled (``rsvp_enabled``) OR capped (the
    // legacy signal that an event needs confirmation) — and never for the
    // creator alone on their own one-attendee event.
    const others = (e.attendees ?? []).filter(uid => uid !== myUid)
    const isCapped = e.capacity != null
    const showRsvp = (e.rsvp_enabled || isCapped)
      && others.length > 0
      && (e.attendees ?? []).includes(myUid ?? '__none__')
    const bounds = formatEventBounds(e)
    return (
      <>
        {e.cover_url && (
          <img class="sh-event-cover-thumb" src={e.cover_url} alt="" loading="lazy" />
        )}
        {e.location && (
          <p class="sh-event-location-row">
            <LocationLink value={e.location} class="sh-event-location" />
          </p>
        )}
        {e.description && <p class="sh-cal-row__desc">{e.description}</p>}
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
              return (
                <Button
                  key={status}
                  variant={status === 'going' ? 'primary' : 'secondary'}
                  loading={rsvpPending.value === `${e.id}:${status}`}
                  disabled={ended}
                  title={ended ? t('event.has_ended_tooltip') : undefined}
                  onClick={() => handleRsvp(e, status)}
                >
                  {t(labelKey)}
                </Button>
              )
            })}
          </div>
        )}
        <ReminderPicker eventId={e.id} occurrenceAt={e.rrule ? e.start : null} />
        {e.capacity != null && (
          e.created_by === myUid || currentUser.value?.is_admin
        ) && (
          <HostApprovalQueue eventId={e.id} spaceId={null} occurrenceAt={e.rrule ? e.start : null} />
        )}
        <div class="sh-event-admin sh-row">
          <Button variant="secondary" onClick={() => handleEdit(e)}>{t('event.edit')}</Button>
          <Button variant="danger" onClick={() => handleDelete(e.id)}>{t('event.delete')}</Button>
          <EventOverflowMenu eventId={e.id} />
        </div>
      </>
    )
  }

  const views: CalendarView[] = wide ? ['upcoming', 'month', 'week'] : ['upcoming', 'month']

  return (
    <>{importHost}
    <div class="sh-calendar">
      <CalendarFilterStrip
        calendars={calendars.value}
        visibleCalendarIds={visibleCalendarIds.value}
        onChange={setVisible}
        onShowAll={showAllCalendars}
        onShowOnlyMine={showOnlyMine}
        primaryAction={
          <div class="sh-cal-strip-actions__buttons">
            <Button variant="secondary" onClick={openCalendarImport}>
              {t('calendar.page.import')}
            </Button>
            <Button onClick={handleNewEvent}>+ {t('calendar.new_event')}</Button>
          </div>
        }
      />

      <div class="sh-space-cal-bar">
        <SegmentedSwitch<CalendarView>
          class="sh-space-cal-bar__views"
          options={views}
          value={current}
          labels={{
            upcoming: t('calendar.upcoming'),
            month: t('calendar.month'),
            week: t('calendar.week'),
          }}
          ariaLabel={t('calendar.views_aria')}
          onChange={(v) => { viewMode.value = v }}
        />
      </div>

      {current !== 'upcoming' && (
        <div class="sh-space-cal-nav">
          {/* ‹ › are bidi-mirrored characters: RTL turns them by itself. */}
          <Button variant="secondary"
                  // Keys: calendar.prev_{month,week}
                  aria-label={t(`calendar.prev_${current}`)}
                  onClick={() => navigateDate(-1, current)}>
            <span aria-hidden="true">&#8249;</span>
          </Button>
          <h2 class="sh-space-cal-nav__heading">
            {formatRangeHeading(currentDate.value, current, weekStart)}
          </h2>
          <Button variant="secondary"
                  // Keys: calendar.next_{month,week}
                  aria-label={t(`calendar.next_${current}`)}
                  onClick={() => navigateDate(1, current)}>
            <span aria-hidden="true">&#8250;</span>
          </Button>
          <Button variant="secondary" onClick={() => { currentDate.value = new Date() }}>
            {t('calendar.today')}
          </Button>
        </div>
      )}

      {current === 'upcoming' ? (
        <CalendarUpcoming
          events={events.value}
          now={now}
          weekStart={weekStart}
          renderDetail={detail}
          decorate={decorate}
          empty={(
            <div class="sh-empty-state">
              <div aria-hidden="true">📅</div>
              <h2>{t('calendar.upcoming.empty', { weeks: String(UPCOMING_WEEKS) })}</h2>
              <p>{t('calendar.page.empty_body')}</p>
              <div class="sh-empty-state__cta-row">
                <Button onClick={handleNewEvent}>+ {t('calendar.page.first_event')}</Button>
                <Button variant="secondary" onClick={openCalendarImport}>
                  {t('calendar.page.import_file')}
                </Button>
              </div>
            </div>
          )}
          footer={events.value.length > 0
            ? t('calendar.upcoming.end', { weeks: String(UPCOMING_WEEKS) })
            : undefined}
        />
      ) : (
        <CalendarGrid
          view={current}
          cursor={currentDate.value}
          events={events.value}
          weekStart={weekStart}
          now={now}
          renderDetail={detail}
          decorate={decorate}
          maxLanes={wide ? 3 : 2}
        />
      )}

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
    </>
  )
}
