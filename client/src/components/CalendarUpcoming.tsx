/**
 * CalendarUpcoming — the calendar's default view: a rolling agenda of
 * the next weeks, grouped by week ("This week · Oct 19 – 25", "Next
 * week", then the dates), each event ONCE (a holiday week is one row,
 * not six). No month navigator: it always starts today.
 *
 * Presentational; the surface owns the fetch (``upcomingRange``), the
 * detail of an opened row (``renderDetail``) and the end line
 * (``footer`` — "Nothing else in the next 4 weeks · Subscribe…").
 */
import type { ComponentChildren } from 'preact'
import { useState } from 'preact/hooks'
import type { CalendarEvent } from '@/types'
import {
  groupUpcomingByWeek, upcomingWeekTitle, weekRangeLabel,
} from '@/utils/calendarViews'
import type { WeekStart } from '@/utils/week'
import { CalendarEventRow, type RowDecor } from './CalendarEventRow'

interface Props {
  events: CalendarEvent[]
  now: Date
  weekStart: WeekStart
  /** What an opened row shows (description, actions). */
  renderDetail: (event: CalendarEvent) => ComponentChildren
  /** Shown when nothing is coming up. */
  empty: ComponentChildren
  /** The end line under the list. */
  footer?: ComponentChildren
  /** Per-event rail colour / labels (the household's calendar hues). */
  decorate?: (event: CalendarEvent) => RowDecor | undefined
}

export function CalendarUpcoming({ events, now, weekStart, renderDetail, empty, footer, decorate }: Props) {
  const [openId, setOpenId] = useState<string | null>(null)
  const weeks = groupUpcomingByWeek(events, now, weekStart)
  return (
    <div class="sh-cal-upcoming">
      {weeks.length === 0 && empty}
      {weeks.map((week) => {
        const title = upcomingWeekTitle(week)
        const range = weekRangeLabel(week.startKey, week.endKey)
        return (
          <section key={week.startKey} class="sh-cal-week" aria-label={title ? `${title}, ${range}` : range}>
            <h2 class="sh-cal-week__heading">
              {title && <span class="sh-cal-week__title">{title}</span>}
              <span class="sh-cal-week__range">{range}</span>
            </h2>
            <div class="sh-cal-list">
              {week.entries.map((entry) => {
                const id = entry.event.id
                return (
                  <CalendarEventRow
                    key={id}
                    entry={entry}
                    open={openId === id}
                    onToggle={() => setOpenId(openId === id ? null : id)}
                    detailId={`sh-cal-detail-${week.startKey}-${id.replace(/[^\w-]/g, '_')}`}
                    decor={decorate?.(entry.event)}
                  >
                    {renderDetail(entry.event)}
                  </CalendarEventRow>
                )
              })}
            </div>
          </section>
        )
      })}
      {footer && <p class="sh-cal-upcoming__end">{footer}</p>}
    </div>
  )
}
