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
import { Avatar } from './Avatar'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import { resolveCalendarColor } from '@/utils/calendar'
import {
  detectBrowserTz,
  localPartsToUtcIso,
  utcIsoToLocalParts,
} from '@/utils/timezone'
import {
  calendarInvitees,
  loadCalendarInvitees,
} from '@/store/calendarInvitees'

interface DialogCalendarSummary {
  id: string
  name: string
  owner_username: string
  color?: string | null
}

const open = signal(false)
/** Single-target calendar id — used in edit mode and as the fallback /
 *  default when ``targetCalendarIds`` is empty. Defaults to the
 *  caller's own calendar. */
const calendarId = signal('')
/** Multi-select target set used by the create flow. The first thing
 *  the dialog asks: "Which calendars should this land on?". Each
 *  picked id triggers a separate ``POST /api/calendars/{id}/events`` so
 *  one event can land on several household members' calendars at once
 *  (e.g. the dentist visit goes on both kids' calendars). The set is
 *  always seeded with the default ``calendarId`` so the simple "create
 *  on my calendar" path needs no extra clicks.
 *
 *  Edit mode keeps the single-calendar select (``targetCalendarIds``
 *  stays empty) — moving an event between calendars is a separate
 *  action from picking N targets at create time. */
const targetCalendarIds = signal<Set<string>>(new Set())
/** Full household calendar list, populated by ``openEventDialog``. The
 *  dialog renders the "For:" selector when this has 2+ entries. */
const householdCalendars = signal<DialogCalendarSummary[]>([])
const spaceId = signal<string | null>(null)
/** Set when editing an existing event — submit PATCHes instead of
 *  POSTing a new event. ``null`` for the create flow. */
const editingEventId = signal<string | null>(null)
/** Edit-mode full-sync state: the CURRENT copies of the event being
 *  edited, keyed ``calendar_id → event_id``. Built from the server's
 *  authoritative ``copies`` field (falling back to the agenda's
 *  visibility-scoped ``_grouped_*`` arrays, then to a single-entry
 *  map for an ungrouped event). On submit, ticked calendars already
 *  in the map are PATCHed, newly-ticked ones are POSTed, and map
 *  entries no longer ticked are DELETEd — keeping every member's
 *  copy in sync. */
const editGroup = signal<Map<string, string>>(new Map())
/** The shared ``client_event_uuid`` of the group under edit, or
 *  ``null`` for a legacy single event. When a legacy event is fanned
 *  out across >1 calendar a uuid is minted on submit and stamped on
 *  every copy so the agenda groups them. */
const editGroupUuid = signal<string | null>(null)
/** The ``rrule`` of the event under edit (``null`` for a one-off).
 *  Carried so full-sync can POST a newly-ticked member into the
 *  SERIES rather than a one-off on whichever occurrence the user
 *  happened to open, and so the series-move guard below knows the
 *  event repeats. */
const editingRrule = signal<string | null>(null)
/** The date / time / all-day values ``openEditEventDialog`` seeded the
 *  form with. Submit compares against them and OMITS ``start`` /
 *  ``end`` from the PATCH when nothing moved, so an untouched date
 *  input can never clobber the stored value.
 *
 *  This is what keeps a recurring event safe: the agenda hands us an
 *  expanded ``{stored_id}@{occurrence_iso}`` occurrence whose
 *  ``start`` is the OCCURRENCE's, while ``copies`` carries the STORED
 *  row ids. PATCHing those rows with the occurrence date would drag
 *  the whole series forward and drop every earlier occurrence —
 *  silently, with a success toast. */
const seededStartDate = signal('')
const seededStartTime = signal('')
const seededEndDate = signal('')
const seededEndTime = signal('')
const seededAllDay = signal(false)
const summary = signal('')
const startDate = signal('')
const startTime = signal('')
const endDate = signal('')
const endTime = signal('')
const allDay = signal(false)
const description = signal('')
const location = signal('')
const limitAttendance = signal(false)
const capacity = signal('')
/** Cover image (canonical ``/api/media/{filename}``). Submitted to
 *  the server. ``''`` (empty) means no cover. */
const coverUrl = signal('')
/** Browser-renderable preview URL (signed). Used only for the dialog
 *  preview ``<img>``; never sent on submit. */
const coverPreview = signal('')
const coverUploading = signal(false)
/** Cross-household invitees (paired-instance user_ids). Local
 *  household members never appear here — coordinating with a household
 *  member is done via the "For:" calendar selector. Spaces invite all
 *  members implicitly via the ``capacity`` / RSVP flow, so this stays
 *  empty for the space-event variant of the dialog. */
const attendees = signal<Set<string>>(new Set())
/** Whether to ask invitees to RSVP. Auto-derives from the attendee
 *  list (RSVP makes sense iff there's at least one cross-household
 *  invitee), but the user can still flip it off — e.g. an FYI invite
 *  that doesn't need a yes/no. */
const rsvpEnabled = signal(true)
// §23.15 — opt-in feed mirror for space events. Off by default: the
// event lives in the Calendar tab unless the creator announces it.
const announceInFeed = signal(false)
const submitting = signal(false)

/** Open the dialog for a personal calendar.
 *
 * @param calId   The caller's own calendar id — used as the default
 *                target so the simple "create on my calendar" path
 *                works without picking anything.
 * @param available  Optional list of household calendars to surface in
 *                   the "For:" selector. When 2+ are passed the user
 *                   can redirect the event onto another member's
 *                   calendar (so e.g. Maria can put a doctor's
 *                   appointment directly on Pascal's calendar without
 *                   it showing up on hers). Pass ``[]`` to hide the
 *                   selector entirely.
 */
export function openEventDialog(
  calId: string,
  available: DialogCalendarSummary[] = [],
) {
  reset()
  calendarId.value = calId
  // Seed the multi-select with the caller's own calendar so the simple
  // path is a one-click create. The chip row shows up at the top of
  // the form for the user to pick additional household members'
  // calendars before filling out the rest.
  targetCalendarIds.value = new Set([calId])
  householdCalendars.value = available
  spaceId.value = null
  open.value = true
  void loadCalendarInvitees()
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

/** Shape of the event passed to :func:`openEditEventDialog`. We pull
 *  only the fields we need to pre-populate; everything else stays at
 *  the dialog's defaults. */
interface EditableEvent {
  id: string
  calendar_id: string
  summary: string
  description?: string | null
  start: string
  end: string
  all_day: boolean
  attendees?: string[]
  rsvp_enabled?: boolean
  cover_url?: string | null
  location?: string | null
  /** Recurrence rule (``FREQ=WEEKLY`` …) or ``null`` / absent for a
   *  one-off. Needed on the edit path so a newly-ticked member joins
   *  the series, and so the dialog can warn before a date change
   *  moves every occurrence. */
  rrule?: string | null
  /** IANA timezone the event was authored in. Used to pre-fill the
   *  date / time inputs in the same wall clock the host saw at
   *  create time — without this the inputs render in the viewer's
   *  browser tz and the host can no longer tell what they actually
   *  scheduled. */
  tz?: string | null
  /** Shared group identity (issue #327). When several members hold a
   *  copy of the same event they share this uuid; the agenda's
   *  ``groupSharedEvents`` merges them into one card. ``null`` /
   *  absent for a legacy single event — full-sync mints one when the
   *  edit fans the event out across >1 calendar. */
  client_event_uuid?: string | null
  /** Authoritative, visibility-independent copy set of the event —
   *  every row sharing its ``client_event_uuid``, oldest first. The
   *  server builds it, so it covers members whose calendars the SPA
   *  never loaded. At most one entry per ``calendar_id``. This is
   *  what full-sync seeds from; ``[]`` (a legacy / ICS row with no
   *  uuid) falls through to the arrays below. */
  copies?: { event_id: string; calendar_id: string; owner_username: string }[]
  /** LEGACY FALLBACK. Parallel arrays (same order) carrying the
   *  underlying copies of a grouped event: ``grouped_calendar_ids[i]``
   *  holds the event whose id is ``grouped_event_ids[i]``. Sourced
   *  from the agenda's ``_grouped_*`` fields, which only cover the
   *  VISIBLE calendars — used only when ``copies`` is absent/empty.
   *  Absent for an ungrouped event — full-sync then falls back to the
   *  single ``{calendar_id: id}`` pair. */
  grouped_calendar_ids?: string[]
  grouped_event_ids?: string[]
}

/** True when ``id`` is an expanded recurrence occurrence rather than a
 *  stored row.
 *
 *  ``SqliteCalendarRepo._expand_window`` synthesises one id per
 *  occurrence as ``"{stored_id}@{occurrence_iso}"``. Stored ids are
 *  bare hex uuids, so the ``@`` is an unambiguous marker. Resolving
 *  such an id back to its stored row is exactly what makes a date edit
 *  dangerous — see ``seededStartDate`` — so the dialog needs to know
 *  which of the two it is holding. Exported for unit tests. */
export function isOccurrenceId(id: string): boolean {
  return id.includes('@')
}

/** True when the dialog is editing an EXPANDED OCCURRENCE of a
 *  recurring event — i.e. the user opened one card of a series. There
 *  is no per-occurrence override in this codebase, so any date change
 *  from here rewrites the whole series. Exported for unit tests. */
export function isEditingSeriesOccurrence(): boolean {
  const id = editingEventId.value
  return !!id && !!editingRrule.value && isOccurrenceId(id)
}

/** IANA tz the dialog uses for the date / time inputs. Defaulted to
 *  the viewer's browser zone for the new-event path so the form
 *  reflects what the user is typing; the edit / open-event paths
 *  override it from ``event.tz`` so the inputs stay anchored to the
 *  host's wall clock. */
const eventTz = signal<string>(detectBrowserTz())

/** Open the dialog in edit mode — pre-populate every field from
 *  ``ev`` and PATCH instead of POST on submit. The same "For:" picker
 *  appears so the user can move the event to another member's
 *  calendar at edit time.
 *
 *  ``inSpaceId`` selects the space-event submit path: when set, the
 *  dialog hides household-only chrome (invitees, "For:" picker) and
 *  the submit handler PATCHes
 *  ``/api/spaces/{id}/calendar/events/{eid}`` instead of the
 *  household route. Same field set on both — host wall clock,
 *  cover, location, capacity all round-trip identically. */
export function openEditEventDialog(
  ev: EditableEvent,
  available: DialogCalendarSummary[] = [],
  inSpaceId: string | null = null,
) {
  reset()
  editingEventId.value = ev.id
  calendarId.value = ev.calendar_id
  // Build the current-copies map, in precedence order:
  //
  //  1. ``ev.copies`` — the server's authoritative, visibility-
  //     independent set.
  //  2. the agenda's parallel ``grouped_*`` arrays — visibility-
  //     scoped, so only a fallback.
  //  3. the event's own ``{calendar_id: id}`` pair.
  //
  // An EMPTY ``copies`` array MUST fall through to (2)/(3): the
  // server legitimately returns ``[]`` for a uuid-less legacy / ICS
  // row, which still depends on the content-key grouping the agenda
  // computed.
  const group = new Map<string, string>()
  const calIds = ev.grouped_calendar_ids
  const evIds = ev.grouped_event_ids
  if (ev.copies && ev.copies.length > 0) {
    // One entry per calendar, first (= oldest) wins. The server
    // already guarantees at most one local copy per calendar via the
    // ``ux_calendar_events_fanout`` partial unique index (migration
    // 0047); first-wins is belt-and-braces for a DB restored from a
    // pre-0047 backup whose migrations haven't run yet.
    for (const c of ev.copies) {
      if (!group.has(c.calendar_id)) group.set(c.calendar_id, c.event_id)
    }
  } else if (
    calIds && evIds && calIds.length === evIds.length && calIds.length > 0
  ) {
    for (let i = 0; i < calIds.length; i++) group.set(calIds[i], evIds[i])
  } else {
    group.set(ev.calendar_id, ev.id)
  }
  editGroup.value = group
  editGroupUuid.value = ev.client_event_uuid ?? null
  // Seed the picker with the members who already hold a copy so the
  // chips start ticked — editing then add/removes from that baseline.
  targetCalendarIds.value = new Set(group.keys())
  householdCalendars.value = available
  spaceId.value = inSpaceId
  summary.value = ev.summary
  description.value = ev.description ?? ''
  location.value = ev.location ?? ''
  // Anchor the date / time inputs to the event's originating tz so
  // the host's wall clock survives the round-trip (UTC → form →
  // UTC). Falls back to the viewer's browser tz when the event row
  // pre-dates the tz column — same as the create-event default.
  const tz = ev.tz || detectBrowserTz()
  eventTz.value = tz
  const startParts = utcIsoToLocalParts(ev.start, tz)
  const endParts = utcIsoToLocalParts(ev.end, tz)
  startDate.value = startParts.date
  startTime.value = startParts.time
  endDate.value = endParts.date
  endTime.value = endParts.time
  allDay.value = ev.all_day
  // Remember exactly what we seeded so submit can tell "the user moved
  // the event" from "the user never touched the date inputs".
  seededStartDate.value = startParts.date
  seededStartTime.value = startParts.time
  seededEndDate.value = endParts.date
  seededEndTime.value = endParts.time
  seededAllDay.value = ev.all_day
  editingRrule.value = ev.rrule ?? null
  attendees.value = new Set(ev.attendees ?? [])
  rsvpEnabled.value = !!ev.rsvp_enabled
  coverUrl.value = ev.cover_url ?? ''
  // Same value works for both submit + preview — the media endpoint
  // signs at fetch time so the browser can load the canonical URL
  // directly via ``<img src>``.
  coverPreview.value = ev.cover_url ?? ''
  open.value = true
  void loadCalendarInvitees()
}

function reset() {
  editingEventId.value = null
  editGroup.value = new Map()
  editGroupUuid.value = null
  editingRrule.value = null
  seededStartDate.value = ''
  seededStartTime.value = ''
  seededEndDate.value = ''
  seededEndTime.value = ''
  seededAllDay.value = false
  summary.value = ''
  description.value = ''
  location.value = ''
  // New-event default: pre-fill in the viewer's browser tz. The
  // backend resolves to the household tz at create time when this
  // happens to match the household (the common case), so the user
  // sees what they're typing without an extra picker step.
  const tz = detectBrowserTz()
  eventTz.value = tz
  const now = new Date()
  const startParts = utcIsoToLocalParts(now.toISOString(), tz)
  const endParts = utcIsoToLocalParts(
    new Date(now.getTime() + 3600000).toISOString(),
    tz,
  )
  startDate.value = startParts.date
  startTime.value = startParts.time
  endDate.value = endParts.date
  endTime.value = endParts.time
  allDay.value = false
  limitAttendance.value = false
  capacity.value = ''
  attendees.value = new Set()
  rsvpEnabled.value = false
  announceInFeed.value = false
  coverUrl.value = ''
  coverPreview.value = ''
  coverUploading.value = false
  targetCalendarIds.value = new Set()
}

export function CalendarEventDialog({ onCreated }: {
  /** Fired after a successful create OR edit with the calendars that
   *  received a BRAND-NEW copy — every target on a create, only the
   *  newly-POSTed ones on an edit (``null`` for space events, which
   *  have no household visibility to manage). The page reveals and
   *  persists exactly these, so an event that lands on a calendar the
   *  user has hidden doesn't appear to vanish.
   *
   *  An empty array means "nothing was added" and MUST leave
   *  visibility untouched: reporting every target used to un-hide —
   *  and save — a calendar the user had deliberately switched off,
   *  merely because they re-saved a shared event that included that
   *  member. */
  onCreated?: (calendarIds: string[] | null) => void
}) {
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
      // Translate the form's wall-clock inputs into UTC ISO using the
      // active tz anchor. ``localPartsToUtcIso`` handles DST via the
      // IANA database, so e.g. "19:00 on Mar 30 Berlin" emits
      // 17:00Z (CEST is in effect), not the 18:00Z the legacy
      // ``${time}:00Z`` would have produced. All-day events keep
      // their conventional 00:00 / 23:59:59 day bounds in the
      // event's tz.
      const tz = eventTz.value
      const start = allDay.value
        ? localPartsToUtcIso(startDate.value, '00:00', tz)
        : localPartsToUtcIso(startDate.value, startTime.value, tz)
      const end = allDay.value
        ? localPartsToUtcIso(endDate.value, '23:59', tz)
        : localPartsToUtcIso(endDate.value, endTime.value, tz)
      // Did the user actually touch the date / time inputs? Compared
      // against what ``openEditEventDialog`` seeded, NOT against the
      // stored row — an expanded occurrence seeds the OCCURRENCE's
      // date, so "unchanged" here means "leave the stored value
      // alone", which is the only safe thing to do to a series.
      const datesChanged
        = startDate.value !== seededStartDate.value
        || startTime.value !== seededStartTime.value
        || endDate.value !== seededEndDate.value
        || endTime.value !== seededEndTime.value
        || allDay.value !== seededAllDay.value
      // A create always carries the dates; an edit only when they moved.
      const sendDates = !editingEventId.value || datesChanged
      // There is no per-occurrence override in this codebase: a PATCH
      // to a recurring row's start rewrites the series' anchor, so
      // every occurrence — past ones included — shifts with it. Name
      // that consequence before writing anything. Cancel = no writes.
      if (isEditingSeriesOccurrence() && datesChanged) {
        const ok = await confirmDialog(
          'This repeats. Changing the date moves the whole series, '
          + 'including past occurrences.',
          {
            title: 'Move the whole series?',
            confirmLabel: 'Move the series',
            destructive: true,
          },
        )
        if (!ok) return
      }
      const body: Record<string, unknown> = {
        summary: summary.value,
        // IANA tz the form was anchored to. The backend stamps this
        // onto the event so a viewer in a different zone still sees
        // the host's intended wall clock with a "≈ HH:MM your time"
        // hint via ``formatEventTime``.
        tz,
        all_day: allDay.value,
        description: description.value || undefined,
      }
      if (sendDates) {
        body.start = start
        body.end = end
      }
      // Cover field is tri-state on edit (omit = leave alone, null =
      // clear, string = set). Create only sends a value when the
      // user actually picked one — the server defaults to NULL.
      if (editingEventId.value) {
        body.cover_url = coverUrl.value || null
      } else if (coverUrl.value) {
        body.cover_url = coverUrl.value
      }
      // Location follows the same tri-state on edit. The trimmed
      // empty string is treated as "clear" so wiping the input clears
      // the field on the server.
      const trimmedLocation = location.value.trim()
      if (editingEventId.value) {
        body.location = trimmedLocation || null
      } else if (trimmedLocation) {
        body.location = trimmedLocation
      }
      if (limitAttendance.value && capacity.value) {
        body.capacity = parseInt(capacity.value, 10)
      }
      // §23.15 — opt-in feed mirror, space events only, at create time.
      if (isSpace && !editingEventId.value) {
        body.announce_in_feed = announceInFeed.value
      }
      // Household-event invitees. Spaces broadcast to the membership
      // implicitly so this field is unused on the space variant.
      if (!isSpace) {
        // On edit we always send the attendees list (even when empty)
        // so the user can clear invitees; on create we omit the field
        // so the server's default takes effect.
        if (editingEventId.value || attendees.value.size > 0) {
          body.attendees = Array.from(attendees.value)
        }
        // RSVP is opt-in. Only meaningful when there's an attendee
        // other than the creator — otherwise nobody to ask. On edit
        // we always send the boolean so toggling off is honoured.
        if (editingEventId.value) {
          body.rsvp_enabled = rsvpEnabled.value && attendees.value.size > 0
        } else if (rsvpEnabled.value && attendees.value.size > 0) {
          body.rsvp_enabled = true
        }
      }
      // Calendars that received a BRAND-NEW copy. Only these are
      // reported to ``onCreated`` — see the prop's doc comment.
      let newCopyCalendarIds: string[] = []
      if (editingEventId.value && !isSpace) {
        // Full sync: PATCH every still-ticked copy with the edited
        // details, POST a copy for newly-ticked members, DELETE the
        // copy for unticked members — all under one
        // ``client_event_uuid`` so the agenda keeps grouping them.
        const selected = new Set(targetCalendarIds.value)
        const group = editGroup.value
        let uuid = editGroupUuid.value
        // Promote a legacy single event into a group the moment the
        // edit spans >1 calendar and there's no uuid yet.
        if (!uuid && selected.size > 1) uuid = _mintEventUuid()
        const withUuid = uuid ? { ...body, client_event_uuid: uuid } : body
        // A newly-ticked member gets a CREATE, which always needs the
        // dates (the PATCH body may legitimately omit them) and the
        // ``rrule`` — without it the new member lands a one-off on
        // whichever occurrence the editor happened to open while
        // everyone else keeps the series.
        const createBody: Record<string, unknown> = { ...withUuid, start, end }
        if (editingRrule.value) createBody.rrule = editingRrule.value

        const ops: Promise<unknown>[] = []
        const addedIds: string[] = []
        let removed = 0
        for (const calId of selected) {
          const existingId = group.get(calId)
          if (existingId) {
            ops.push(api.patch(`/api/calendars/events/${existingId}`, withUuid))
          } else {
            ops.push(api.post(`/api/calendars/${calId}/events`, createBody))
            addedIds.push(calId)
          }
        }
        for (const [calId, evId] of group) {
          if (!selected.has(calId)) {
            ops.push(api.delete(`/api/calendars/events/${evId}`))
            removed++
          }
        }
        const results = await Promise.allSettled(ops)
        const ok = results.filter(r => r.status === 'fulfilled').length
        const failed = results.length - ok
        if (failed > 0 && ok === 0) {
          throw new Error(
            (results.find(r => r.status === 'rejected') as PromiseRejectedResult)
              .reason?.message
              ?? t('event.dialog.update_failed'),
          )
        }
        if (failed > 0) {
          showToast(
            t('event.dialog.updated_partial', { count: String(failed) }),
            'error',
          )
        } else if (addedIds.length > 0 || removed > 0) {
          let msg = t('event.dialog.updated')
          if (addedIds.length > 0) {
            msg += t('event.dialog.added_suffix',
              { count: String(addedIds.length) })
          }
          if (removed > 0) {
            msg += t('event.dialog.removed_suffix', { count: String(removed) })
          }
          showToast(msg, 'success')
        } else {
          showToast(t('event.dialog.updated'), 'success')
        }
        newCopyCalendarIds = addedIds
      } else if (isSpace && editingEventId.value) {
        // Space-event edit: PATCH the space-scoped route so the
        // ``SpaceCalendarService.update_event`` path runs (membership
        // gate, ``feat_calendar`` check, ``SPACE_CALENDAR_EVENT_UPDATED``
        // federation). Without this branch the dialog used to POST a
        // duplicate event whenever the user tapped Save on a space
        // event — visible as Pascal's "no option to edit" report.
        await api.patch(
          `/api/spaces/${spaceId.value}/calendar/events/${editingEventId.value}`,
          body,
        )
        showToast('Event updated', 'success')
      } else if (isSpace) {
        await api.post(
          `/api/spaces/${spaceId.value}/calendar/events`,
          body,
        )
        showToast(t('event.dialog.created'), 'success')
      } else {
        // Multi-target create: fan out one POST per picked calendar.
        // Empty set (e.g. when the dialog opened without a list) falls
        // back to the single ``calendarId`` for back-compat. Cap to 1
        // implicit target when nothing's selected so the user still
        // gets an event.
        const targets = targetCalendarIds.value.size > 0
          ? Array.from(targetCalendarIds.value)
          : [calendarId.value]
        // Stamp one ``client_event_uuid`` across every POST in the
        // batch so the agenda's ``groupSharedEvents`` can merge the
        // resulting rows by intent — see issue #327. The same uuid
        // also rides the federation envelope so cross-household
        // mirrors group with the host's row. Only stamped when
        // there's actually more than one target — a single-target
        // event doesn't need it (and would just waste bytes).
        const fanoutBody = targets.length > 1
          ? { ...body, client_event_uuid: _mintEventUuid() }
          : body
        // POST in parallel so a four-kid drop-on-everyone is one
        // round-trip wall-clock. Failures are individually toasted so
        // a partial success still surfaces useful state.
        const results = await Promise.allSettled(
          targets.map(id => api.post(
            `/api/calendars/${id}/events`,
            fanoutBody,
          )),
        )
        const ok = results.filter(r => r.status === 'fulfilled').length
        const failed = results.length - ok
        if (failed > 0 && ok === 0) {
          throw new Error(
            (results.find(r => r.status === 'rejected') as PromiseRejectedResult)
              .reason?.message
              ?? t('event.dialog.failed'),
          )
        }
        if (failed > 0) {
          showToast(
            `Created on ${ok} calendar${ok === 1 ? '' : 's'}, ${failed} failed`,
            'error',
          )
        } else if (ok > 1) {
          showToast(`Event created on ${ok} calendars`, 'success')
        } else {
          showToast(t('event.dialog.created'), 'success')
        }
        // Every target of a create is, by definition, a new copy.
        newCopyCalendarIds = targets
      }
      open.value = false
      // Report ONLY the calendars that gained a new copy, so the page
      // reveals (and persists) exactly those. Reporting every target
      // un-hid a calendar the user had deliberately switched off,
      // merely because a shared event they re-saved happened to
      // include that member — a visibility change nobody asked for,
      // written straight to their saved prefs.
      onCreated?.(isSpace ? null : newCopyCalendarIds)
    } catch (e) {
      const msg = (e as Error)?.message || t('event.dialog.failed')
      showToast(msg, 'error')
    } finally {
      submitting.value = false
    }
  }

  return (
    <Modal open={open.value} onClose={() => (open.value = false)}
           title={editingEventId.value ? 'Edit event' : t('event.dialog.title')}>
      <div class="sh-form">
        {!isSpace && householdCalendars.value.length > 1 && (
          <CreateCalendarPicker />
        )}
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
            onInput={(e) => {
              startDate.value = (e.target as HTMLInputElement).value
              syncEndToStart()
            }}
          />
        </label>
        {!allDay.value && (
          <label>
            {t('event.dialog.start_time')}
            <input
              type="time"
              value={startTime.value}
              onInput={(e) => {
                startTime.value = (e.target as HTMLInputElement).value
                syncEndToStart()
              }}
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
          {t('event.dialog.location')}
          <input
            type="text"
            value={location.value}
            placeholder={t('event.dialog.location_placeholder')}
            onInput={(e) =>
              (location.value = (e.target as HTMLInputElement).value)
            }
            maxLength={500}
          />
        </label>
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

        <CoverPicker />

        {!isSpace && (() => {
          const instances = calendarInvitees.value
          const toggle = (uid: string) => {
            const next = new Set(attendees.value)
            if (next.has(uid)) next.delete(uid)
            else next.add(uid)
            attendees.value = next
          }
          return (
            <div class="sh-attendee-block">
              <span class="sh-form-label">Invite from connected households</span>
              {instances.length === 0 ? (
                <p class="sh-form-help">
                  No paired households yet. Pair a household from{' '}
                  <a href="/settings/connections">Settings → Connections</a>{' '}
                  to invite friends from another home. (Household members
                  don't need invites — drop the event on their calendar
                  via the picker above.)
                </p>
              ) : (
                instances.map(inst => (
                  <fieldset key={inst.instance_id} class="sh-attendee-group">
                    <legend>{inst.instance_name}</legend>
                    <div class="sh-attendee-picker">
                      {inst.members.map(m => {
                        const picked = attendees.value.has(m.user_id)
                        const label = m.display_name || m.remote_username
                        return (
                          <button
                            key={m.user_id}
                            type="button"
                            class={
                              picked
                                ? 'sh-attendee-chip sh-attendee-chip--picked'
                                : 'sh-attendee-chip'
                            }
                            aria-pressed={picked}
                            onClick={() => toggle(m.user_id)}
                          >
                            <Avatar src={m.picture_url ?? null}
                                    name={label}
                                    size={20} />
                            <span>{label}</span>
                          </button>
                        )
                      })}
                    </div>
                  </fieldset>
                ))
              )}
              {attendees.value.size > 0 && (
                <label class="sh-form-row-cap" style={{ marginTop: 'var(--sh-space-xs)' }}>
                  <input
                    type="checkbox"
                    checked={rsvpEnabled.value}
                    onChange={() => (rsvpEnabled.value = !rsvpEnabled.value)}
                  />{' '}
                  Ask invitees to RSVP
                  <small class="sh-form-help" style={{ display: 'block', marginLeft: 24 }}>
                    On by default for cross-household invites. Turn off to
                    send the event without asking for a yes/no.
                  </small>
                </label>
              )}
            </div>
          )
        })()}

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
            {!editingEventId.value && (
              <label class="sh-form-row-cap">
                <input
                  type="checkbox"
                  checked={announceInFeed.value}
                  onChange={() =>
                    (announceInFeed.value = !announceInFeed.value)}
                />{' '}
                Also announce this event in the space feed
              </label>
            )}
          </>
        )}

        <div class="sh-form-actions">
          <Button
            variant="secondary"
            onClick={() => (open.value = false)}
          >
            {t('common.cancel')}
          </Button>
          <Button
            onClick={submit}
            loading={submitting.value}
            disabled={!summary.value.trim()}
          >
            {editingEventId.value
              ? 'Save changes'
              : t('event.dialog.create')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

/** Resolve a calendar owner's friendly display name via the cached
 *  household user map. Falls back to the bare username when the cache
 *  hasn't loaded yet (rare — the page kicks off ``loadHouseholdUsers``
 *  on mount). */
/** Mint a v4 UUID shared across a multi-target event fan-out. The
 *  same uuid lands on every resulting row + every federation envelope
 *  so the agenda's :func:`groupSharedEvents` can merge them by
 *  intent (see issue #327). Falls back to a timestamp+random hex
 *  string in environments without ``crypto.randomUUID`` (older Edge,
 *  test runners). The fallback shape still matches the
 *  ``_clean_client_event_uuid`` server-side accept-list (32 hex
 *  chars, no dashes). */
function _mintEventUuid(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (c?.randomUUID) return c.randomUUID()
  // Two 16-hex-char chunks → 32-char accept-listed shape.
  const chunk = () =>
    Math.floor(Math.random() * 0x1_0000_0000_0000).toString(16).padStart(13, '0')
  return (chunk() + chunk() + chunk()).slice(0, 32)
}

/** Keep ``end ≥ start`` whenever the start fields change. Picking a
 *  start date past the current end (e.g. opening the dialog at default
 *  ``today + 1h`` and then jumping the start to next month) would
 *  otherwise leave the end input "in the past" relative to the new
 *  start and force the user into a second pick. Same logic guards
 *  the same-day case where the user nudges start_time past end_time.
 *
 *  Lexicographic compares are safe: ``YYYY-MM-DD`` and ``HH:MM``
 *  strings both sort chronologically. Exported for unit tests — the
 *  module-level signals are kept private. */
export function syncEndToStart(): void {
  if (!startDate.value) return
  if (endDate.value && endDate.value < startDate.value) {
    endDate.value = startDate.value
  }
  if (
    endDate.value === startDate.value
    && startTime.value
    && endTime.value
    && endTime.value < startTime.value
  ) {
    endTime.value = startTime.value
  }
}

function ownerDisplayName(owner_username: string): string {
  for (const u of householdUsers.value.values()) {
    if (u.username === owner_username) {
      return u.display_name || u.username
    }
  }
  return owner_username
}

/** Owner avatar URL — same lookup table as the display name resolver
 *  above. ``null`` if the user isn't in the cache or has no picture. */
function ownerPictureUrl(owner_username: string): string | null {
  for (const u of householdUsers.value.values()) {
    if (u.username === owner_username) {
      return u.picture_url ?? null
    }
  }
  return null
}

/** Create-mode multi-target calendar picker — the very first question
 *  the dialog asks: "Whose calendar(s) does this land on?". Renders one
 *  avatar chip per household calendar; tap toggles inclusion. The chip
 *  for the caller's own calendar starts pre-selected so a fast
 *  "create on my calendar" flow needs zero extra clicks.
 *
 *  Multi-select intentionally allows a single chore-of-the-week event
 *  to drop onto each kid's calendar in one go — the alternative was
 *  three trips through the dialog and three deletes if the user
 *  changes their mind.
 */
function CreateCalendarPicker() {
  const me = currentUser.value?.username
  const picked = targetCalendarIds.value
  // Sort: own calendars first, then alphabetic. Matches the strip on
  // the parent page so identity reads consistently.
  const sorted = [...householdCalendars.value].sort((a, b) => {
    const am = a.owner_username === me ? 0 : 1
    const bm = b.owner_username === me ? 0 : 1
    if (am !== bm) return am - bm
    return ownerDisplayName(a.owner_username)
      .localeCompare(ownerDisplayName(b.owner_username))
  })
  const ownerCount = new Map<string, number>()
  for (const c of sorted) {
    ownerCount.set(c.owner_username,
      (ownerCount.get(c.owner_username) ?? 0) + 1)
  }
  const toggle = (id: string) => {
    const next = new Set(targetCalendarIds.value)
    if (next.has(id)) {
      // Never let the user clear every chip — at least one target
      // must stay picked or the submit button has nowhere to write.
      if (next.size === 1) return
      next.delete(id)
    } else {
      next.add(id)
    }
    targetCalendarIds.value = next
  }
  const pickedCount = picked.size
  return (
    <div>
      <span class="sh-form-label">Add to calendar</span>
      <p class="sh-cal-target-help">
        {pickedCount > 1
          ? `Lands on ${pickedCount} calendars — tap a chip to deselect.`
          : 'Tap another household member to drop the event on their calendar too.'}
      </p>
      <div class="sh-cal-target-picker">
        {sorted.map(c => {
          const mine = c.owner_username === me
          const ambiguous = (ownerCount.get(c.owner_username) ?? 1) > 1
          const ownerLabel = ownerDisplayName(c.owner_username)
          const headline = mine ? 'You' : ownerLabel
          const isPicked = picked.has(c.id)
          const hue = resolveCalendarColor(c)
          return (
            <button
              key={c.id}
              type="button"
              class={
                isPicked
                  ? 'sh-cal-target-chip sh-cal-target-chip--picked'
                  : 'sh-cal-target-chip'
              }
              aria-pressed={isPicked}
              style={{ '--cal-hue': hue } as Record<string, string>}
              onClick={() => toggle(c.id)}
            >
              <Avatar
                name={ownerLabel}
                src={ownerPictureUrl(c.owner_username)}
                size={28}
              />
              <span class="sh-cal-target-chip__name">
                <span>{headline}</span>
                {ambiguous && (
                  <span class="sh-cal-target-chip__sub">{c.name}</span>
                )}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** Cover image picker — file input + live preview + remove button.
 *
 * On file pick we POST to ``/api/media/upload`` (the same endpoint
 * the post composer + gallery use), then store the canonical URL in
 * ``coverUrl`` (sent to the server) and the signed URL in
 * ``coverPreview`` (rendered inline). On "Remove cover" both
 * signals clear; the next save sends ``cover_url: null`` which the
 * route's tri-state cover handling translates to "drop the column". */
function CoverPicker() {
  const onPick = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    coverUploading.value = true
    try {
      const fd = new FormData()
      fd.append('file', file)
      // ``api.upload`` handles base-href + bearer auth uniformly.
      // The previous raw ``fetch('/api/media/upload', …)`` bypassed
      // ``<base href>`` and 404'd under HAOS ingress (#303).
      const data = await api.upload<{ url: string; signed_url?: string }>(
        '/api/media/upload',
        fd,
      )
      coverUrl.value = data.url
      coverPreview.value = data.signed_url || data.url
    } catch (err) {
      showToast(`Cover upload failed: ${(err as Error).message}`, 'error')
    } finally {
      coverUploading.value = false
      input.value = ''
    }
  }

  const removeCover = () => {
    coverUrl.value = ''
    coverPreview.value = ''
  }

  return (
    <div class="sh-event-cover-picker">
      <span class="sh-form-label">Cover image (optional)</span>
      {coverPreview.value ? (
        <div class="sh-event-cover-picker-preview-wrap">
          <img
            class="sh-event-cover-preview"
            src={coverPreview.value}
            alt=""
          />
          <button
            type="button"
            class="sh-event-cover-remove"
            aria-label="Remove cover"
            onClick={removeCover}
          >×</button>
        </div>
      ) : (
        <label class="sh-event-cover-picker-empty">
          <input
            type="file"
            accept="image/*"
            onChange={onPick}
            class="sr-only"
          />
          <span>
            {coverUploading.value
              ? 'Uploading…'
              : '📷 Choose cover image'}
          </span>
        </label>
      )}
    </div>
  )
}
