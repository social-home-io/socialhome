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
 *   the buttons with a tooltip.
 * * Status pill — "You're going" / "Pending approval" / "On waitlist
 *   (#3)" — so the user always knows where they stand.
 * * "Add to my calendar" link to ``/api/calendars/events/{id}/export.ics``.
 *
 * The component is intentionally read-mostly: capacity edits, host
 * approval, and reminder configuration live in the calendar event
 * detail (CalendarPage) rather than the feed card.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { CapacityStrip } from '@/components/CapacityStrip'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import {
  myRsvpStatus,
  rsvpCounts,
  type RsvpCounts,
} from '@/store/calendar'
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
  const startStr = occurrence
    ? formatEventTime(occurrence.start, event.all_day)
    : t('event.has_ended')

  const isCapped = event.capacity != null
  const isCreator = event.created_by === currentUser.value?.user_id

  return (
    <div class="sh-event-card" data-event-id={event.id}>
      <div class="sh-event-card-when">
        <span class="sh-event-card-when-icon" aria-hidden="true">📅</span>
        <span class="sh-event-card-when-text">{startStr}</span>
        {event.rrule && (
          <span class="sh-event-card-recur">
            {' · '}
            {t('event.recurring_chip', {
              freq: rruleHumanFreq(event.rrule),
            })}
          </span>
        )}
      </div>

      <CapacityStrip counts={counts} capacity={event.capacity} myStatus={myStatus} />

      {myStatus && (
        <div class="sh-event-card-pill">
          <StatusPill status={myStatus} counts={counts} />
        </div>
      )}

      <div class="sh-event-card-rsvp" role="group" aria-label={t('event.rsvp.aria')}>
        {RSVP_BUTTONS.map((btn) => (
          <RsvpButton
            key={btn.status}
            event={event}
            occurrenceAt={occurrence?.start ?? null}
            status={btn.status}
            emoji={btn.emoji}
            i18nKey={btn.i18nKey}
            disabled={ended}
            isCapped={isCapped}
            mine={myStatus}
          />
        ))}
        <a
          class="sh-event-card-ics-btn"
          href={`/api/calendars/events/${event.id}/export.ics`}
          download
          aria-label={t('event.add_to_calendar')}
          title={t('event.add_to_calendar')}
        >
          <span aria-hidden="true">📥</span>
          <span class="sh-vh">{t('event.add_to_calendar')}</span>
        </a>
      </div>

      {isCapped && isCreator && (
        <div class="sh-event-card-host-hint">
          {t('event.host_hint_capped')}
        </div>
      )}
    </div>
  )
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

  return (
    <Button
      variant={variant}
      disabled={disabled}
      title={tooltip || undefined}
      onClick={async () => {
        if (disabled) return
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

function formatEventTime(iso: string, allDay: boolean): string {
  const d = new Date(iso)
  if (allDay) {
    return d.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    })
  }
  return d.toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}
