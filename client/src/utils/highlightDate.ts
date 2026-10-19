/**
 * A highlight's day as words: "19 October 2026" / "19. Oktober 2026" in
 * the UI locale. ``highlight_date`` is a UTC ``YYYY-MM-DD`` key stamped
 * server-side, so it is labelled in UTC (``formatDayKey``) — a viewer west
 * of UTC never sees the day before. Anything that isn't such a key is
 * shown as it came.
 */
import { formatDayKey } from './calendar'

const DAY_KEY = /^\d{4}-\d{2}-\d{2}$/

export function highlightDateLabel(dayKey: string): string {
  if (!DAY_KEY.test(dayKey)) return dayKey
  const label = formatDayKey(dayKey, { day: 'numeric', month: 'long', year: 'numeric' })
  return label === 'Invalid Date' ? dayKey : label
}
