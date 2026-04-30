/**
 * Calendar formatting / grouping helpers shared by the household
 * calendar (`features/calendar/CalendarPage.tsx`) and the per-space
 * calendar tab (`features/spaces/SpaceFeedPage.tsx`). Keeping the
 * grouping rule in one place means the two surfaces always render the
 * same day buckets — no drift between the household and a space.
 */
import type { CalendarEvent } from '@/types'

/** Group events into ``{ "M/D/YYYY" → events }`` buckets, locale-aware. */
export function groupEventsByDay(
  evts: CalendarEvent[],
): Record<string, CalendarEvent[]> {
  const groups: Record<string, CalendarEvent[]> = {}
  for (const e of evts) {
    const key = new Date(e.start).toLocaleDateString()
    if (!groups[key]) groups[key] = []
    groups[key].push(e)
  }
  return groups
}

/** Heading for a month-view month strip — "April 2026", localised. */
export function formatMonthHeading(date: Date): string {
  return date.toLocaleDateString(undefined, {
    month: 'long',
    year: 'numeric',
  })
}

/** ISO bounds for the calendar month containing ``date``. */
export function monthRange(date: Date): { start: string; end: string } {
  const start = new Date(date.getFullYear(), date.getMonth(), 1)
  const end = new Date(date.getFullYear(), date.getMonth() + 1, 0, 23, 59, 59)
  return { start: start.toISOString(), end: end.toISOString() }
}
