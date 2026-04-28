/**
 * Calendar store — keeps event lists + RSVP counts in sync with WS
 * frames (``calendar.created`` / ``calendar.updated`` / ``calendar.deleted``
 * / ``calendar.rsvp_updated``).
 *
 * Pages own their fetching; this store gives them a place to merge
 * remote-driven changes so a different family member's edit shows up
 * without a refresh.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'
import type { CalendarEvent } from '@/types'

/** Re-export under the historical ``CalendarEventLite`` name so old
 * imports (``store/calendar``) still resolve. */
export type { CalendarEvent }
export type CalendarEventLite = CalendarEvent

export interface RsvpCounts {
  going:    number
  maybe:    number
  declined: number
  /** Phase C — pending host approval on capacity-limited events. */
  requested?: number
  /** Phase C — overflow on capacity-limited events; auto-promotes when seats free. */
  waitlist?: number
}

export const events = signal<CalendarEvent[]>([])
/** Map of event_id → live RSVP counts (backfilled by calendar.rsvp_updated). */
export const rsvpCounts = signal<Record<string, RsvpCounts>>({})

/** Map of event_id → current user's RSVP status for the next occurrence,
 *  used by the EventPostCard / status pill. Backfilled lazily as the
 *  user RSVPs and from inbound calendar.rsvp_updated frames that include
 *  ``user_status``. */
export const myRsvpStatus = signal<Record<string, RsvpCounts['going'] | string | null>>({})

/** Idempotent: subscribes once. */
export function wireCalendarWs(): void {
  ws.on('calendar.created', (e) => {
    const ev = (e.data as { event: CalendarEvent }).event
    if (!ev) return
    if (!events.value.some((x) => x.id === ev.id)) {
      events.value = [...events.value, ev]
    }
  })
  ws.on('calendar.updated', (e) => {
    const ev = (e.data as { event: CalendarEvent }).event
    if (!ev) return
    events.value = events.value.map((x) => (x.id === ev.id ? ev : x))
  })
  ws.on('calendar.deleted', (e) => {
    const { event_id } = e.data as { event_id: string }
    if (!event_id) return
    events.value = events.value.filter((x) => x.id !== event_id)
  })
  ws.on('calendar.rsvp_updated', (e) => {
    const { event_id, counts } = e.data as {
      event_id: string
      counts: RsvpCounts
    }
    if (!event_id || !counts) return
    rsvpCounts.value = { ...rsvpCounts.value, [event_id]: counts }
  })
}
