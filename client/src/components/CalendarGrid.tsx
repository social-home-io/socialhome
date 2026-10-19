/**
 * CalendarGrid — the Month and Week views: a grid of days with each
 * event as a bar across the days it covers (a holiday week is one bar,
 * not six chips), and under the grid the picked day's events as
 * ordinary rows (``CalendarEventRow``) to open.
 *
 * - **Day cells are the controls.** One Tab stop for the grid (roving
 *   tabindex): the arrow keys move the picked day (mirrored in RTL),
 *   Home / End jump to the week's ends. Each cell's name says the date
 *   and how many events it holds. The bars are a picture of the same
 *   thing (``aria-hidden``) and let clicks through to the cell below.
 * - **Month** shows up to ``MONTH_LANES`` bars a week and a "+N" on a
 *   day with more; **Week** (wide screens) shows them all, with times.
 * - CSS grid columns follow the writing direction, so the bars mirror
 *   in RTL without any code.
 *
 * Presentational: the surface owns the cursor, the fetch
 * (``gridRange``) and what an opened row shows.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useRef, useState } from 'preact/hooks'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import { addDaysToKey, dayKeyOf, formatDayKey } from '@/utils/calendar'
import {
  entriesOnDay, gridWeeks, layoutWeekBars, spanEntries, type GridBar,
} from '@/utils/calendarViews'
import type { WeekStart } from '@/utils/week'
import { CalendarEventRow, type RowDecor } from './CalendarEventRow'

// For ``i18n:check``: t('calendar.grid.month_aria') t('calendar.grid.week_aria')
// t('calendar.grid.events_one') t('calendar.grid.events')

/** Bars shown per week row in Month on a wide screen; more → "+N" on
 *  the day. A phone passes fewer (``maxLanes``). */
export const MONTH_LANES = 3

interface Props {
  view: 'month' | 'week'
  /** Any day of the month / week to show. */
  cursor: Date
  events: CalendarEvent[]
  weekStart: WeekStart
  now: Date
  renderDetail: (event: CalendarEvent) => ComponentChildren
  /** Bars per week row in Month (Week shows them all). */
  maxLanes?: number
  /** Per-event colour / labels (the household's calendar hues). */
  decorate?: (event: CalendarEvent) => RowDecor | undefined
}

/** A bar's text: the title; in Week a timed event's start time first. */
function BarLabel({ bar, view }: { bar: GridBar; view: 'month' | 'week' }) {
  const e = bar.entry.event
  if (view === 'week' && !e.all_day && !bar.continuesBefore) {
    const at = new Date(e.start).toLocaleTimeString(formatLocale(), { hour: '2-digit', minute: '2-digit' })
    return <><span class="sh-cal-bar__time">{at}</span> {e.summary}</>
  }
  return <>{e.summary}</>
}

export function CalendarGrid({
  view, cursor, events, weekStart, now, renderDetail, maxLanes = MONTH_LANES, decorate,
}: Props) {
  const weeks = gridWeeks(cursor, view, weekStart)
  const allKeys = weeks.flat()
  const todayKey = dayKeyOf(now)
  const monthPrefix = dayKeyOf(cursor).slice(0, 7)
  const fallback = allKeys.includes(todayKey)
    ? todayKey
    : view === 'month' ? `${monthPrefix}-01` : allKeys[0]
  const [picked, setPicked] = useState<string>(fallback)
  const [openId, setOpenId] = useState<string | null>(null)
  const gridRef = useRef<HTMLDivElement>(null)
  const focusPending = useRef(false)
  // A new month / week picks today (or its first day) again.
  const day = allKeys.includes(picked) ? picked : fallback
  useEffect(() => {
    if (!focusPending.current) return
    focusPending.current = false
    gridRef.current?.querySelector<HTMLElement>(`[data-day="${day}"]`)?.focus()
  }, [day])

  const entries = spanEntries(events)
  const lanesCap = view === 'month' ? maxLanes : Infinity

  const pick = (key: string) => {
    if (key !== day) setOpenId(null)
    setPicked(key)
  }

  const onKeyDown = (ev: KeyboardEvent) => {
    const rtl = gridRef.current ? getComputedStyle(gridRef.current).direction === 'rtl' : false
    const idx = allKeys.indexOf(day)
    const col = idx % 7
    let next: string | null = null
    switch (ev.key) {
      case 'ArrowRight': next = addDaysToKey(day, rtl ? -1 : 1); break
      case 'ArrowLeft':  next = addDaysToKey(day, rtl ? 1 : -1); break
      case 'ArrowDown':  next = addDaysToKey(day, 7); break
      case 'ArrowUp':    next = addDaysToKey(day, -7); break
      case 'Home':       next = allKeys[idx - col]; break
      case 'End':        next = allKeys[idx - col + 6]; break
      default: return
    }
    ev.preventDefault()
    if (next && allKeys.includes(next)) {
      focusPending.current = true
      pick(next)
    }
  }

  const dayEntries = entriesOnDay(entries, day)
  const dayHeading = formatDayKey(day, { weekday: 'long', day: 'numeric', month: 'long' })

  return (
    <div class={`sh-cal-grid-view sh-cal-grid-view--${view}`}>
      <div class="sh-cal-grid" ref={gridRef} role="group"
           aria-label={t(view === 'month' ? 'calendar.grid.month_aria' : 'calendar.grid.week_aria')}
           onKeyDown={onKeyDown}>
        {/* Week cells name their weekday themselves. */}
        {view === 'month' && (
          <div class="sh-cal-grid__dow" aria-hidden="true">
            {weeks[0].map((key) => (
              <span key={key}>{formatDayKey(key, { weekday: 'short' })}</span>
            ))}
          </div>
        )}
        {weeks.map((week) => {
          const bars = layoutWeekBars(week, entries)
          const shown = bars.filter((b) => b.lane < lanesCap)
          const lanes = shown.reduce((n, b) => Math.max(n, b.lane + 1), 0)
          return (
            <div key={week[0]} class="sh-cal-grid__week"
                 style={{ '--sh-cal-lanes': String(Math.max(lanes, view === 'month' ? maxLanes : 1)) }}>
              {week.map((key, i) => {
                const count = entriesOnDay(entries, key).length
                const hidden = bars.filter((b) => b.lane >= lanesCap
                  && b.col <= i && i < b.col + b.span).length
                const outside = view === 'month' && key.slice(0, 7) !== monthPrefix
                const name = formatDayKey(key, { weekday: 'long', day: 'numeric', month: 'long' })
                const countText = count === 0
                  ? t('calendar.grid.no_events')
                  : t(isOne(count) ? 'calendar.grid.events_one' : 'calendar.grid.events', { count: String(count) })
                return (
                  <button
                    key={key}
                    type="button"
                    data-day={key}
                    class={'sh-cal-grid__day'
                      + (outside ? ' is-outside' : '')
                      + (key === todayKey ? ' is-today' : '')}
                    style={{ gridColumn: String(i + 1) }}
                    tabIndex={key === day ? 0 : -1}
                    aria-pressed={key === day}
                    aria-current={key === todayKey ? 'date' : undefined}
                    aria-label={`${name}, ${countText}`}
                    onClick={() => pick(key)}
                  >
                    <span class="sh-cal-grid__num">
                      {view === 'week'
                        ? `${formatDayKey(key, { weekday: 'short' })} ${formatDayKey(key, { day: 'numeric' })}`
                        : formatDayKey(key, { day: 'numeric' })}
                    </span>
                    {hidden > 0 && (
                      <span class="sh-cal-grid__more" aria-hidden="true">
                        {t('calendar.grid.more', { count: String(hidden) })}
                      </span>
                    )}
                  </button>
                )
              })}
              {shown.map((bar) => {
                const hue = decorate?.(bar.entry.event)?.hue
                return (
                  <span
                    key={bar.entry.event.id}
                    aria-hidden="true"
                    class={'sh-cal-bar'
                      + (bar.entry.dayCount > 1 ? ' sh-cal-bar--multi'
                        : bar.entry.event.all_day ? ' sh-cal-bar--allday' : ' sh-cal-bar--timed')
                      + (hue ? ' sh-cal-bar--hued' : '')
                      + (bar.continuesBefore ? ' is-cont-before' : '')
                      + (bar.continuesAfter ? ' is-cont-after' : '')}
                    style={{
                      gridColumn: `${bar.col + 1} / span ${bar.span}`,
                      gridRow: String(bar.lane + 2),
                      ...(hue ? { '--sh-cal-hue': hue } : {}),
                    }}
                  >
                    <BarLabel bar={bar} view={view} />
                  </span>
                )
              })}
            </div>
          )
        })}
      </div>

      <section class="sh-cal-day">
        <h2 class="sh-cal-week__heading">
          <span class="sh-cal-week__title">{dayHeading}</span>
        </h2>
        {dayEntries.length === 0
          ? <p class="sh-cal-day__empty">{t('calendar.grid.day_empty')}</p>
          : (
            <div class="sh-cal-list">
              {dayEntries.map((entry) => {
                const id = entry.event.id
                return (
                  <CalendarEventRow
                    key={id}
                    entry={entry}
                    open={openId === id}
                    onToggle={() => setOpenId(openId === id ? null : id)}
                    detailId={`sh-cal-detail-${day}-${id.replace(/[^\w-]/g, '_')}`}
                    decor={decorate?.(entry.event)}
                  >
                    {renderDetail(entry.event)}
                  </CalendarEventRow>
                )
              })}
            </div>
          )}
      </section>
    </div>
  )
}
