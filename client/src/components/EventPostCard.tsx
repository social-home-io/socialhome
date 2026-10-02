/**
 * EventPostCard — feed-card body for ``post.type === 'event'``.
 *
 * The :class:`CalendarFeedBridge` (Phase B) auto-creates one
 * ``PostType.EVENT`` post per calendar-event series. The post's body
 * is the event summary and the comment thread is the event's
 * discussion. This component renders the event-specific affordances
 * inside the existing :class:`PostCard` chrome:
 *
 * * Date/time row with all-day / "next on …" hints for recurring events.
 * * :class:`CapacityStrip` summary of RSVP counts (Phase C).
 * * Inline RSVP buttons (going / maybe / declined). On capped events
 *   the "going" button reads "Request to join". Past events disable
 *   the buttons with a tooltip. Hidden entirely when the server says
 *   the viewer can't RSVP (``can_rsvp: false`` — a read-only
 *   subscriber, or an archived space).
 * * Status pill — "You're going" / "Pending approval" / "On waitlist
 *   (#3)" — so the user always knows where they stand.
 * * "Add to my calendar" link to ``/api/calendars/events/{id}/export.ics``.
 *
 * The component is intentionally read-mostly: capacity edits, host
 * approval, and reminder configuration live in the calendar event
 * detail (CalendarPage) rather than the feed card.
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { CapacityStrip } from '@/components/CapacityStrip'
import { EventOverflowMenu } from '@/components/EventOverflowMenu'
import { LocationLink } from '@/components/LocationLink'
import { renderMarkdown } from '@/components/markdown'
import { openEditEventDialog } from '@/components/CalendarEventDialog'
import { showToast } from '@/components/Toast'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import {
  myRsvpStatus,
  rsvpCounts,
  type RsvpCounts,
} from '@/store/calendar'
import { lastInclusiveMoment, safeTimeZone } from '@/utils/calendar'
import {
  detectBrowserTz,
  formatEventTime as formatTimeInTz,
} from '@/utils/timezone'
import type { CalendarEvent } from '@/types'

export interface EventPostCardProps {
  /** ID of the linked calendar event (``post.linked_event_id``). When
   *  null the post is detached (event was deleted) and we fall back
   *  to a "(removed)" notice. */
  eventId: string | null
}

/** Per-event detail loaded lazily on first render. Keyed by event id
 *  so several cards on the same feed share the lookup. */
const eventCache = signal<Record<string, CalendarEvent | null>>({})
const loading = signal<Record<string, boolean>>({})

const RSVP_BUTTONS: Array<{
  status: 'going' | 'maybe' | 'declined'
  emoji: string
  i18nKey: string
}> = [
  { status: 'going', emoji: '✅', i18nKey: 'event.rsvp.going' },
  { status: 'maybe', emoji: '🤔', i18nKey: 'event.rsvp.maybe' },
  { status: 'declined', emoji: '❌', i18nKey: 'event.rsvp.declined' },
]

export function EventPostCard({ eventId }: EventPostCardProps) {
  useEffect(() => {
    if (!eventId) return
    if (eventId in eventCache.value) return
    if (loading.value[eventId]) return
    loading.value = { ...loading.value, [eventId]: true }
    api
      .get<CalendarEvent>(`/api/calendars/events/${eventId}`)
      .then((ev) => {
        eventCache.value = { ...eventCache.value, [eventId]: ev }
      })
      .catch(() => {
        // Event hard-deleted upstream; cache the null so we don't retry.
        eventCache.value = { ...eventCache.value, [eventId]: null }
      })
      .finally(() => {
        const next = { ...loading.value }
        delete next[eventId]
        loading.value = next
      })
  }, [eventId])

  if (!eventId) {
    return (
      <div class="sh-event-card sh-event-card--orphan">
        <em>{t('event.removed')}</em>
      </div>
    )
  }

  const event = eventCache.value[eventId]
  const isLoading = loading.value[eventId] ?? !(eventId in eventCache.value)

  if (isLoading) {
    return <div class="sh-event-card sh-event-card--loading" aria-busy="true" />
  }

  if (event === null || event === undefined) {
    return (
      <div class="sh-event-card sh-event-card--orphan">
        <em>{t('event.removed')}</em>
      </div>
    )
  }

  const counts = rsvpCounts.value[eventId] as RsvpCounts | undefined
  const myStatus = (myRsvpStatus.value[eventId] ?? null) as
    | 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist' | null

  const occurrence = nextOccurrenceFor(event)
  const ended = occurrence == null
  const occurrenceStart = occurrence?.start ?? null
  const occurrenceEnd = occurrenceStart
    ? new Date(
        new Date(occurrenceStart).getTime()
        + (new Date(event.end).getTime() - new Date(event.start).getTime()),
      ).toISOString()
    : null
  // ``safeTimeZone`` rather than a bare ``|| 'UTC'``: ``event.tz`` is
  // whatever the authoring household / an ICS import / a peer put on
  // the row, and every Intl call below throws ``RangeError`` on an
  // unknown zone — one bad row must cost this card its tz precision,
  // not blank it.
  const eventTz = safeTimeZone(event.tz)
  const viewerTz = detectBrowserTz()
  const showTzHint = eventTz !== viewerTz && !event.all_day

  const isCapped = event.capacity != null
  const isCreator = event.created_by === currentUser.value?.user_id
  // Read-only subscribers (and anyone in an archived space) can't RSVP —
  // the server answers 403 — so don't offer buttons that can only fail.
  // ``can_rsvp`` is absent on older servers: keep showing them then.
  const canRsvp = event.can_rsvp !== false

  // RSVP attendance line — populated only when we know at least one
  // response. Capped events lean on CapacityStrip instead; uncapped
  // events surface their own one-liner so members can tell who else
  // is coming without opening the dialog.
  const attendance = !isCapped && counts ? formatAttendance(counts) : null
  const cardClass = `sh-event-card${ended ? ' sh-event-card--past' : ''}`

  return (
    <div class={cardClass} data-event-id={event.id}>
      {event.cover_url && (
        <img
          class="sh-event-card-cover"
          src={event.cover_url}
          alt=""
          loading="lazy"
        />
      )}
      <h3 class="sh-event-card-title">{event.summary}</h3>
      <EventWhen
        occurrenceStart={occurrenceStart}
        occurrenceEnd={occurrenceEnd}
        allDay={event.all_day}
        eventTz={eventTz}
        viewerTz={viewerTz}
        showTzHint={showTzHint}
        rrule={event.rrule ?? null}
        ended={ended}
      />
      {event.location && (
        <div class="sh-event-card-location">
          <LocationLink
            value={event.location}
            className="sh-event-card-location-link"
          />
        </div>
      )}
      {event.description && (
        <div
          class="sh-event-card-description sh-post-body"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(event.description) }}
        />
      )}

      <CapacityStrip
        counts={counts}
        capacity={event.capacity}
        myStatus={myStatus}
        hideEmpty={!canRsvp}
      />
      {attendance && (
        <div class="sh-event-card-attendance">{attendance}</div>
      )}

      {/* Status pill is only useful when it carries information the
          RSVP buttons don't: "Pending approval" / "On waitlist #N".
          For plain going/maybe/declined the primary-variant button
          already states it. */}
      {myStatus === 'requested' || myStatus === 'waitlist' ? (
        <div class="sh-event-card-pill">
          <StatusPill status={myStatus} counts={counts} />
        </div>
      ) : null}

      <div class="sh-event-card-rsvp" role="group" aria-label={t('event.rsvp.aria')}>
        {!canRsvp && (
          <span class="sh-event-card-rsvp-note">{t('event.rsvp.read_only')}</span>
        )}
        {canRsvp && RSVP_BUTTONS.map((btn) => (
          <RsvpButton
            key={btn.status}
            event={event}
            occurrenceAt={occurrenceStart}
            status={btn.status}
            emoji={btn.emoji}
            i18nKey={btn.i18nKey}
            disabled={ended}
            isCapped={isCapped}
            mine={myStatus}
          />
        ))}
        {/* ``Add to my calendar`` promoted out of the kebab — for
            non-creators it's the most useful follow-up, so it earns a
            sibling slot next to the RSVP set. Uses a relative URL
            (no leading slash) so the browser resolves it against
            ``<base href>`` for the HA Supervisor ingress prefix. */}
        <a
          class="sh-event-card-ics"
          href={addBase(`/api/calendars/events/${event.id}/export.ics`)}
          download
          aria-label={t('event.add_to_calendar')}
          title={t('event.add_to_calendar')}
        >
          <span aria-hidden="true">📥</span>
          <span class="sh-event-card-ics-label">{t('event.add_to_calendar')}</span>
        </a>
        <EventOverflowMenu
          eventId={event.id}
          showIcsItem={false}
        >
          {isCreator && (
            <button
              type="button"
              role="menuitem"
              class="sh-post-menu-item"
              onClick={() => {
                // ``calendar_id`` on a space event is the space id (see
                // ``SpaceCalendarService.create_event``), so pass it as
                // the ``inSpaceId`` argument so the dialog PATCHes the
                // space-scoped route instead of the household route.
                openEditEventDialog(event, [], event.calendar_id)
              }}
            >
              {t('event.edit')}
            </button>
          )}
        </EventOverflowMenu>
      </div>

      {isCapped && isCreator && (
        <div class="sh-event-card-host-hint">
          {t('event.host_hint_capped')}
        </div>
      )}
    </div>
  )
}

/** Pretty-prints the date + time-range for a single occurrence,
 *  honouring the event's authored tz and surfacing a "≈ HH:MM your
 *  time" hint when the viewer's browser tz differs. */
function EventWhen({
  occurrenceStart,
  occurrenceEnd,
  allDay,
  eventTz,
  viewerTz,
  showTzHint,
  rrule,
  ended,
}: {
  occurrenceStart: string | null
  occurrenceEnd: string | null
  allDay: boolean
  eventTz: string
  viewerTz: string
  showTzHint: boolean
  rrule: string | null
  ended: boolean
}) {
  if (!occurrenceStart) {
    return (
      <div class="sh-event-card-when sh-event-card-when--past">
        <span class="sh-event-card-when-icon" aria-hidden="true">📅</span>
        <span class="sh-event-card-when-text">{t('event.has_ended')}</span>
      </div>
    )
  }

  const dateStr = formatDate(occurrenceStart, eventTz)
  let timeStr = ''
  let viewerHint: string | null = null
  if (allDay) {
    if (occurrenceEnd) {
      // The end bound is EXCLUSIVE at the boundary — an ICS-imported
      // all-day 10–12 Sep arrives as ``…-13T00:00:00Z``, and the
      // composer's own rows sit at 23:59. Same rule the agenda row and
      // the expanded detail apply (``utils/calendar``), so the three
      // surfaces can't name different last days.
      const endDate = formatDate(lastInclusiveMoment(occurrenceEnd), eventTz)
      if (endDate !== dateStr) {
        timeStr = `${dateStr} – ${endDate}`
      } else {
        timeStr = dateStr
      }
    } else {
      timeStr = dateStr
    }
  } else {
    const startFmt = formatTimeInTz(occurrenceStart, eventTz, viewerTz)
    const endFmt = occurrenceEnd
      ? formatTimeInTz(occurrenceEnd, eventTz, viewerTz)
      : null
    // An overnight event (e.g. 10 PM – 2 AM) otherwise reads as ending
    // earlier the same day — qualify the end time with its own date
    // whenever it lands on a different calendar day than the start.
    const endDateStr = occurrenceEnd
      ? formatDate(occurrenceEnd, eventTz)
      : null
    const endLabel =
      endFmt && endDateStr && endDateStr !== dateStr
        ? `${endDateStr}, ${endFmt.primary}`
        : endFmt?.primary
    timeStr = endFmt
      ? `${dateStr}, ${startFmt.primary} – ${endLabel}`
      : `${dateStr}, ${startFmt.primary}`
    if (showTzHint && endFmt) {
      viewerHint = `≈ ${endFmt.secondary?.replace(/^≈ /, '') ?? ''}`
      // ``secondary`` is non-null whenever ``showTzHint`` is true, but
      // be defensive — fall back to the start-only hint if the helper
      // ever returns a degenerate range.
      if (!endFmt.secondary && startFmt.secondary) viewerHint = startFmt.secondary
    } else if (showTzHint && startFmt.secondary) {
      viewerHint = startFmt.secondary
    }
  }

  return (
    <div class={`sh-event-card-when${ended ? ' sh-event-card-when--past' : ''}`}>
      <div class="sh-event-card-when-row">
        <span class="sh-event-card-when-icon" aria-hidden="true">📅</span>
        <span class="sh-event-card-when-text">{timeStr}</span>
        {rrule && (
          <span class="sh-event-card-recur">
            {' · '}
            {t('event.recurring_chip', {
              freq: rruleHumanFreq(rrule),
            })}
          </span>
        )}
      </div>
      {viewerHint && (
        <div class="sh-event-card-when-viewer">{viewerHint}</div>
      )}
    </div>
  )
}

/** Render a YYYY-MM-DD-ish date label for the card headline,
 *  honouring the event's authored tz so the wall clock matches what
 *  the host typed.
 *
 *  All-day rows are anchored on the event tz too — NOT on UTC. The
 *  composer stores them as 00:00 / 23:59 in the event's own tz (see
 *  ``CalendarEventDialog``'s submit handler), so a Zurich household's
 *  "1 May" is the instant ``2026-04-30T22:00Z``. Reading that in UTC
 *  printed the day before, and made a single all-day event look like
 *  a two-day range ("Apr 30 – May 1").
 *
 *  Takes an already-parsed ``Date`` too, so a caller that applied the
 *  exclusive-end rule doesn't have to round-trip through ISO. */
function formatDate(when: string | Date, tz: string): string {
  const d = when instanceof Date ? when : new Date(when)
  return d.toLocaleDateString(undefined, {
    timeZone: tz,
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })
}

/** "12 going · 3 maybe" — only counts non-zero buckets so a fresh
 *  event without responses doesn't show a depressing "0 going". */
function formatAttendance(counts: RsvpCounts): string | null {
  const parts: string[] = []
  if (counts.going > 0) {
    parts.push(t('event.attendance.going', { n: String(counts.going) }))
  }
  if (counts.maybe > 0) {
    parts.push(t('event.attendance.maybe', { n: String(counts.maybe) }))
  }
  if (counts.declined > 0) {
    parts.push(t('event.attendance.declined', { n: String(counts.declined) }))
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

function StatusPill({
  status,
  counts,
}: {
  status: 'going' | 'maybe' | 'declined' | 'requested' | 'waitlist'
  counts: RsvpCounts | undefined
}) {
  if (status === 'waitlist') {
    // Show 1-based position when the API wires it; for now show the raw
    // count of waitlist rows since #position isn't on the wire yet.
    const w = counts?.waitlist ?? 0
    return (
      <span class="sh-event-status-pill sh-event-status-pill--waitlist">
        {t('event.status.waitlist', { n: String(w) })}
      </span>
    )
  }
  return (
    <span class={`sh-event-status-pill sh-event-status-pill--${status}`}>
      {t(`event.status.${status}`)}
    </span>
  )
}

function RsvpButton({
  event,
  occurrenceAt,
  status,
  emoji,
  i18nKey,
  disabled,
  isCapped,
  mine,
}: {
  event: CalendarEvent
  occurrenceAt: string | null
  status: 'going' | 'maybe' | 'declined'
  emoji: string
  i18nKey: string
  disabled: boolean
  isCapped: boolean
  mine: string | null
}) {
  // "Request to join" copy when capped + going — softens the host-
  // approval expectation without hiding it from the user.
  const labelKey = isCapped && status === 'going'
    ? 'event.rsvp.request_to_join'
    : i18nKey
  const label = t(labelKey)
  const isMine = mine === status || (status === 'going' && (mine === 'requested' || mine === 'waitlist'))
  const variant = isMine ? 'primary' : 'secondary'
  const tooltip = disabled
    ? t('event.has_ended_tooltip')
    : isCapped && status === 'going'
      ? t('event.rsvp.request_to_join_tooltip')
      : ''

  // ``busy`` reflects the in-flight POST; surfaces as the spinner on
  // the Button so a slow connection doesn't read as a no-op tap.
  const [busy, setBusy] = useState(false)

  return (
    <Button
      variant={variant}
      disabled={disabled}
      loading={busy}
      title={tooltip || undefined}
      onClick={async () => {
        if (disabled || busy) return
        setBusy(true)
        try {
          await api.post(`/api/calendars/events/${event.id}/rsvp`, {
            status,
            occurrence_at: occurrenceAt ?? undefined,
          })
          // Optimistic local update — myStatus will get the canonical
          // value from the next ``calendar.rsvp_updated`` WS frame.
          // For capped + going, the server lands as ``requested``.
          const nextStatus = isCapped && status === 'going' ? 'requested' : status
          myRsvpStatus.value = {
            ...myRsvpStatus.value,
            [event.id]: nextStatus,
          }
          if (isCapped && status === 'going') {
            showToast(t('event.rsvp.requested_toast'), 'success')
          } else {
            showToast(t(`event.rsvp.${status}_toast`), 'success')
          }
        } catch (e) {
          const msg = (e as Error)?.message ?? t('event.rsvp.failed')
          showToast(msg, 'error')
        } finally {
          setBusy(false)
        }
      }}
    >
      <span aria-hidden="true">{emoji}</span> {label}
    </Button>
  )
}

/** Compute the next occurrence start for a one-off or recurring event.
 *  Returns ``null`` when the event has fully ended. For recurring
 *  events without an in-window expansion this still rounds up to a
 *  reasonable ``start`` by walking weekly steps from ``event.start``
 *  — sufficient for UI display; the server validates against the
 *  full rrule on RSVP. */
function nextOccurrenceFor(event: CalendarEvent): { start: string } | null {
  const now = new Date()
  const start = new Date(event.start)
  const end = new Date(event.end)
  const duration = end.getTime() - start.getTime()
  if (!event.rrule) {
    return end > now ? { start: event.start } : null
  }
  // Rough next-occurrence walk for the four FREQ values the server
  // expander supports. Step by the rule's natural interval; fall back
  // to event.start for unrecognised rules.
  const step = parseFreqStep(event.rrule)
  if (!step) {
    return end > now ? { start: event.start } : null
  }
  let cursor = new Date(start)
  for (let i = 0; i < 1000; i++) {
    const occEnd = new Date(cursor.getTime() + duration)
    if (occEnd > now) return { start: cursor.toISOString() }
    cursor = new Date(cursor.getTime() + step)
  }
  return null
}

function parseFreqStep(rrule: string): number | null {
  const m: Record<string, string> = {}
  for (const part of rrule.split(';')) {
    const [k, v] = part.split('=')
    if (k && v) m[k.toUpperCase()] = v.toUpperCase()
  }
  const interval = parseInt(m.INTERVAL ?? '1', 10) || 1
  const day = 24 * 60 * 60 * 1000
  switch (m.FREQ) {
    case 'DAILY':   return interval * day
    case 'WEEKLY':  return interval * 7 * day
    case 'MONTHLY': return interval * 30 * day  // approximate; UI hint only
    case 'YEARLY':  return interval * 365 * day // approximate; UI hint only
    default:        return null
  }
}

function rruleHumanFreq(rrule: string): string {
  const m = /FREQ=(\w+)/i.exec(rrule)
  if (!m) return ''
  const freq = m[1].toLowerCase()
  return t(`event.rrule.${freq}`) || freq
}
