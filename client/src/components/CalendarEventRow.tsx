/**
 * CalendarEventRow — one event in a calendar list (Upcoming, or a day
 * picked in the Month / Week grid).
 *
 * Every event has the same anatomy, multi-day ones included: a date
 * column ("Mon 19 / 08:30", "Thu 29 / All day", "Mon 26 – Sat 31 /
 * 6 days"), a colour rail (timed · all-day · several days) and the
 * title with one meta line (until when, where). A multi-day event is
 * ONE row — never a card per day.
 *
 * The row header is a disclosure button; the detail (``children``)
 * renders as its sibling while open, so a surface puts its own actions
 * (Delete, Report, RSVP) there without nesting controls in a button.
 */
import type { ComponentChildren } from 'preact'
import { formatLocale, t } from '@/i18n/i18n'
import { lastInclusiveMoment } from '@/utils/calendar'
import { shortDay, spanEnds, type SpanEntry } from '@/utils/calendarViews'

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString(formatLocale(), { hour: '2-digit', minute: '2-digit' })
}

/** The lines of the date column: day + time / "All day"; a multi-day
 *  event "Mon 26 –" / "Sat 31" / "6 days" (the span breaks after the
 *  dash, never inside a day). */
export function rowWhen(entry: SpanEntry): string[] {
  const { event, startKey, endKey, dayCount } = entry
  if (dayCount > 1) {
    const [from, to] = spanEnds(startKey, endKey)
    return [`${from} –`, to, t('calendar.span_days', { count: String(dayCount) })]
  }
  return [shortDay(startKey), event.all_day ? t('calendar.all_day') : clock(event.start)]
}

/** The meta line under the title: until when (timed), where, else the
 *  first line of the description. */
export function rowMeta(entry: SpanEntry): string {
  const { event, dayCount } = entry
  const parts: string[] = []
  if (!event.all_day && event.end && new Date(event.end).getTime() > new Date(event.start).getTime()) {
    const until = dayCount > 1
      ? `${shortDay(entry.endKey)} ${clock(lastInclusiveMoment(event.end).toISOString())}`
      : clock(event.end)
    parts.push(t('event.span.until', { time: until }))
  }
  const where = event.location?.trim()
  if (where) parts.push(where)
  if (parts.length === 0) {
    const first = event.description?.trim().split('\n')[0]
    if (first) parts.push(first)
  }
  return parts.join(' · ')
}

/** Rail colour: timed, all-day, or several days. */
function railKind(entry: SpanEntry): 'timed' | 'allday' | 'multi' {
  if (entry.dayCount > 1) return 'multi'
  return entry.event.all_day ? 'allday' : 'timed'
}

interface Props {
  entry: SpanEntry
  open: boolean
  onToggle: () => void
  /** A clean id for ``aria-controls`` (unique on the page). */
  detailId: string
  /** The expanded detail; rendered only while ``open``. */
  children?: ComponentChildren
}

export function CalendarEventRow({ entry, open, onToggle, detailId, children }: Props) {
  const when = rowWhen(entry)
  const meta = rowMeta(entry)
  return (
    <div class={`sh-cal-row sh-cal-row--${railKind(entry)}${open ? ' is-open' : ''}`}>
      <button type="button" class="sh-cal-row__head"
              aria-expanded={open} aria-controls={detailId} onClick={onToggle}>
        <span class="sh-cal-row__when">
          {/* The trailing space keeps the lines apart in the button's
           *  accessible name ("Mon 26 – Sat 31 6 days"). */}
          {when.map((line, i) => <span key={i} dir="auto">{line} </span>)}
        </span>
        <span class="sh-cal-row__rail" aria-hidden="true" />
        <span class="sh-cal-row__body">
          <strong class="sh-cal-row__title">{entry.event.summary}</strong>{' '}
          {meta && <span class="sh-cal-row__meta">{meta}</span>}
        </span>
      </button>
      {open && <div class="sh-cal-row__detail" id={detailId}>{children}</div>}
    </div>
  )
}
