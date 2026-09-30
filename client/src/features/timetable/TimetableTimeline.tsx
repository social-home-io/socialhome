/**
 * Timeline layout — blocks placed on a shared minute axis, so days
 * with different start times line up truthfully. Each day column is a
 * ``role="list"`` labelled by its heading; clicking empty space in a
 * column opens the EntryDialog at that time (snapped to 5 minutes).
 * Long empty stretches collapse into a "⋯" band; the body scrolls
 * inside the component when the day is tall.
 */
import type { JSX } from 'preact'
import { useContext, useMemo } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { brushOn } from './brush'
import { DayHeading } from './DayHeading'
import { LessonBlock } from './LessonBlock'
import { blockBox, dayEntries, timelineGeometry } from './layout'
import type { MenuItem } from './OverflowMenu'
import { fromMinutes, snapTo5, weekdayName } from './time'
import { useGridNav, type GridNavHandlers } from './useGridNav'
import { WeekContext, addBlockedReason } from './weekView'

/** Picture view needs room for a big icon + caption. */
const PICTURE_MIN_BLOCK = 76

interface Props {
  tt: Timetable
  days: number[]
  today: number | null
  picture: boolean
  onEdit: (entry: TimetableEntry) => void
  onAddAt: (weekday: number, startMin: number) => void
  onAddDay: (weekday: number) => void
  dayMenu?: (weekday: number) => MenuItem[]
  /** An empty day's "Set up Monday" call to action (regular mode). */
  onSetupDay?: (weekday: number) => void
  nav?: GridNavHandlers
}

export function TimetableTimeline({
  tt, days, today, picture, onEdit, onAddAt, onAddDay, dayMenu, onSetupDay, nav,
}: Props) {
  const week = useContext(WeekContext)
  const grid = useGridNav(days, nav)
  const addBlocked = (d: number) => week ? addBlockedReason(week, d)
    : brushOn(tt.id) ? t('timetable.brush.no_add') : null
  const daysKey = days.join(',')
  // Recomputed per timetable version (each version is a new object).
  const geo = useMemo(
    () => timelineGeometry(tt, days, picture ? { minBlock: PICTURE_MIN_BLOCK } : {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tt, daysKey, picture],
  )
  const headId = (d: number) => `sh-tt-${tt.id}-t-${d}`
  const cols = { '--tt-cols': String(days.length) } as JSX.CSSProperties

  const onColumnClick = (weekday: number) => (ev: MouseEvent) => {
    // Brush mode paints; a past week-mode day can't take changes.
    if (addBlocked(weekday)) return
    const col = ev.currentTarget as HTMLElement
    const y = ev.clientY - col.getBoundingClientRect().top
    onAddAt(weekday, snapTo5(geo.minuteAt(y)))
  }

  return (
    <div class="sh-timetable-timeline" style={cols} ref={grid.ref}
         onKeyDown={grid.onKeyDown} onFocusIn={grid.onFocusIn}>
      <div class="sh-timetable-timeline__head">
        <div class="sh-timetable-timeline__corner" aria-hidden="true" />
        {days.map(d => (
          <DayHeading key={d} weekday={d} id={headId(d)} today={d === today}
                      onAdd={() => onAddDay(d)} menu={dayMenu?.(d)} blocked={addBlocked(d)} />
        ))}
      </div>
      <div class="sh-timetable-timeline__body" style={{ height: `${geo.height}px` }}>
        <div class="sh-timetable-timeline__axis" aria-hidden="true">
          {geo.ticks.filter(tk => tk.major).map(tk => (
            <span key={tk.minute} class="sh-timetable-timeline__tick" style={{ top: `${tk.y}px` }}>
              {fromMinutes(tk.minute)}
            </span>
          ))}
        </div>
        <div class="sh-timetable-timeline__lines" aria-hidden="true">
          {geo.ticks.map(tk => (
            <span key={tk.minute}
                  class={`sh-timetable-timeline__line${tk.major ? '' : ' sh-timetable-timeline__line--minor'}`}
                  style={{ top: `${tk.y}px` }} />
          ))}
          {geo.segments.filter(s => s.compressed).map(s => (
            <span key={s.from} class="sh-timetable-timeline__gap"
                  style={{ top: `${geo.y(s.from)}px` }}>⋯</span>
          ))}
        </div>
        {days.map(d => (
          <div
            key={d}
            role="list"
            aria-labelledby={headId(d)}
            class={`sh-timetable-timeline__col${d === today ? ' sh-timetable-timeline__col--today' : ''}`}
            onClick={onColumnClick(d)}
          >
            {onSetupDay && dayEntries(tt, d).length === 0 && (
              <div role="listitem" class="sh-timetable-timeline__setup">
                <button type="button" class="sh-btn sh-btn--secondary sh-timetable-setup"
                        data-tt-nav="" data-day={d} data-start={0} data-kind="setup"
                        aria-label={t('timetable.day.setup_cta', { day: weekdayName(d, 'long') })}
                        onClick={(e) => { e.stopPropagation(); onSetupDay(d) }}>
                  {t('timetable.day.setup_cta', { day: weekdayName(d, 'short') })}
                </button>
              </div>
            )}
            {dayEntries(tt, d).map(e => {
              const box = blockBox(geo, e)
              return (
                <div key={e.id} role="listitem" class="sh-timetable-timeline__slot"
                     style={{ top: `${box.top}px`, height: `${box.height}px` }}>
                  <LessonBlock
                    tt={tt} entry={e} variant="timeline" picture={picture}
                    hideTime={box.height < 50}
                    hideMeta={box.height < 64}
                    onOpen={onEdit}
                  />
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}
