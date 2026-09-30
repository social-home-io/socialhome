/**
 * TimetableDayView — the phone layout (< 640 px): one day at a time.
 *
 * Day chips (sticky, one per timetable day) pick the day — today by
 * default, else the next school day; swiping left / right on the list
 * steps through the days (pointer events, 50 px threshold, vertical
 * scrolling untouched via ``touch-action: pan-y``). Lessons read as a
 * vertical list — time on the left, block on the right — with breaks
 * as thin dividers and double lessons merged into one block (the same
 * ``canMerge`` rule as the Periods table).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { isoWeekday } from '@/utils/week'
import { LessonBlock } from './LessonBlock'
import { dayEntries, mergeRuns, nextStartFor, prefillAt, type EntryPrefill } from './layout'
import { nextDayFrom, orderedDays, toMinutes, weekdayName } from './time'

const SWIPE_PX = 50

interface Props {
  tt: Timetable
  picture: boolean
  onEdit: (entry: TimetableEntry) => void
  onAdd: (prefill: EntryPrefill) => void
  now?: Date
}

export function TimetableDayView({ tt, picture, onEdit, onAdd, now }: Props) {
  const days = orderedDays(tt.days, tt.week_start)
  const todayWd = isoWeekday(now ?? new Date())
  const [picked, setPicked] = useState<number | null>(null)
  // A day removed in Settings falls back to today / the next day.
  const day = picked !== null && days.includes(picked) ? picked : nextDayFrom(todayWd, days)
  const start = useRef<{ x: number; y: number } | null>(null)
  const swiped = useRef(false)

  const chipsRef = useRef<HTMLDivElement | null>(null)

  // Dock the sticky chips right under the Calendar | Timetable strip:
  // measure its height (it wraps with the font size / locale) instead of
  // guessing; the CSS token is the fallback where ResizeObserver is
  // missing (tests, old browsers).
  useEffect(() => {
    const strip = chipsRef.current?.closest('.sh-calendar-host')
      ?.querySelector<HTMLElement>(':scope > .sh-space-subheader')
    const chips = chipsRef.current
    if (!strip || !chips || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      chips.style.setProperty('--sh-tt-sticky-top',
        `calc(var(--sh-topbar-height) + ${strip.offsetHeight}px)`)
    })
    ro.observe(strip)
    return () => ro.disconnect()
  }, [])

  // Roving tabindex (WAI-ARIA tabs): arrows / Home / End move the
  // selection and focus together; only the selected chip is tabbable.
  const pickAndFocus = (d: number) => {
    setPicked(d)
    // Every chip is already rendered — focus the target right away.
    chipsRef.current?.querySelector<HTMLElement>(`[data-day="${d}"]`)?.focus()
  }
  const onChipKey = (e: KeyboardEvent) => {
    const i = days.indexOf(day)
    const to = e.key === 'ArrowRight' ? days[(i + 1) % days.length]
      : e.key === 'ArrowLeft' ? days[(i - 1 + days.length) % days.length]
        : e.key === 'Home' ? days[0]
          : e.key === 'End' ? days[days.length - 1]
            : null
    if (to === null) return
    e.preventDefault()
    pickAndFocus(to)
  }

  const step = (dir: 1 | -1) => {
    const i = days.indexOf(day) + dir
    if (i >= 0 && i < days.length) setPicked(days[i])
  }
  const onPointerDown = (e: PointerEvent) => {
    start.current = { x: e.clientX, y: e.clientY }
    swiped.current = false
  }
  const onPointerUp = (e: PointerEvent) => {
    const s = start.current
    start.current = null
    if (!s) return
    const dx = e.clientX - s.x
    const dy = e.clientY - s.y
    if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
      swiped.current = true
      step(dx < 0 ? 1 : -1)
    }
  }
  // A swipe that ends on a block must not also open it.
  const onClickCapture = (e: MouseEvent) => {
    if (swiped.current) {
      swiped.current = false
      e.stopPropagation()
      e.preventDefault()
    }
  }

  // Double lessons read as one block, exactly as in the Periods table.
  const runs = mergeRuns(dayEntries(tt, day), tt.defaults.gap_minutes)
  const panelId = `sh-tt-${tt.id}-day-panel`
  const isToday = (d: number) => d === todayWd && tt.valid_today !== false

  return (
    <div class="sh-timetable-day">
      <div ref={chipsRef} class="sh-timetable-day__chips" role="tablist"
           aria-label={t('timetable.day.days_aria')} onKeyDown={onChipKey}>
        {days.map(d => (
          <button
            key={d}
            type="button"
            role="tab"
            id={`sh-tt-${tt.id}-daytab-${d}`}
            aria-selected={d === day}
            tabIndex={d === day ? 0 : -1}
            data-day={d}
            aria-controls={panelId}
            aria-label={weekdayName(d, 'long') + (isToday(d) ? `, ${t('timetable.grid.today')}` : '')}
            class={`sh-timetable-day__chip${d === day ? ' is-on' : ''}${isToday(d) ? ' is-today' : ''}`}
            onClick={() => setPicked(d)}
          >
            {weekdayName(d, 'short')}
          </button>
        ))}
      </div>
      <div
        id={panelId}
        role="tabpanel"
        aria-labelledby={`sh-tt-${tt.id}-daytab-${day}`}
        class="sh-timetable-day__panel"
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerCancel={() => { start.current = null }}
        onClickCapture={onClickCapture}
      >
        {runs.length === 0 ? (
          <p class="sh-timetable-day__nothing">
            {t('timetable.day.nothing', { day: weekdayName(day, 'long') })}
          </p>
        ) : (
          <ol class="sh-timetable-day__list">
            {runs.map(({ entry: e, end, count }) => (
              <li key={e.id}
                  class={`sh-timetable-day__row sh-timetable-day__row--${e.kind}${count > 1 ? ' sh-timetable-day__row--double' : ''}`}>
                <span class="sh-timetable-day__time" aria-hidden="true">
                  <span>{e.start}</span>
                  {e.kind === 'lesson' && <span class="sh-timetable-day__end">{end}</span>}
                </span>
                <LessonBlock tt={tt} entry={e} variant="day" picture={picture}
                             spanEnd={count > 1 ? end : undefined} hideTime onOpen={onEdit} />
              </li>
            ))}
          </ol>
        )}
        <button
          type="button"
          class="sh-btn sh-btn--secondary sh-timetable-day__add"
          onClick={() => onAdd(prefillAt(tt, day, toMinutes(nextStartFor(tt, day))))}
        >
          + {t('timetable.grid.add_lesson')}
        </button>
      </div>
    </div>
  )
}
