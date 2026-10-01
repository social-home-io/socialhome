/**
 * Periods layout — the classic school table: one row per slot ("1.",
 * "08:00–08:45"), one column per day, thin spanning break bands, and
 * double lessons merged with ``rowSpan``. One Tab stop, arrow keys move
 * between blocks (``useGridNav``). ``printCopy`` renders the
 * non-interactive twin the print stylesheet shows.
 */
import { useContext, useMemo } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { DayHeading } from './DayHeading'
import { brushOn } from './brush'
import { LessonBlock } from './LessonBlock'
import { isUntitledLesson } from './labels'
import { periodRows } from './layout'
import { useTimetableScope } from './scope'
import type { MenuItem } from './OverflowMenu'
import { formatRange, toMinutes, weekdayName } from './time'
import { useGridNav, type GridNavHandlers } from './useGridNav'
import { WeekContext, addBlockedReason, blockedLabel } from './weekView'

interface Props {
  tt: Timetable
  days: number[]
  today: number | null
  picture: boolean
  onEdit: (entry: TimetableEntry, group?: string[], run?: string[]) => void
  onAddAt: (weekday: number, startMin: number) => void
  onAddDay: (weekday: number) => void
  dayMenu?: (weekday: number) => MenuItem[]
  nav?: GridNavHandlers
  printCopy?: boolean
}

export function TimetablePeriods({
  tt, days, today, picture, onEdit, onAddAt, onAddDay, dayMenu, nav, printCopy = false,
}: Props) {
  const daysKey = days.join(',')
  const week = useContext(WeekContext)
  const { editable } = useTimetableScope()
  // Recomputed per timetable version (each version is a new object).
  const all = useMemo(() => periodRows(tt, days), [tt, daysKey]) // eslint-disable-line react-hooks/exhaustive-deps
  // View only: an untitled slot is no lesson — its cell stays blank and a
  // row with nothing else in it goes.
  const blank = (c: (typeof all)[number]['cells'][number]) =>
    c === null || (c !== 'merged' && c.rowSpan === 1 && isUntitledLesson(c.entry))
  const rows = editable ? all : all.filter(r => r.band || !days.every(d => blank(r.cells[d])))
  const headId = (d: number) => `sh-tt-${tt.id}-p${printCopy ? 'x' : ''}-${d}`
  const grid = useGridNav(days, nav)
  // A band edits the break on every day — not in week mode (one week's
  // change is per day) nor in brush mode (breaks are never brushed).
  const bandInert = !!week || brushOn(tt.id)
  // Brush mode paints; nothing opens the EntryDialog.
  const addBlocked = (d: number) => week ? addBlockedReason(week, d)
    : brushOn(tt.id) ? t('timetable.brush.no_add') : null
  return (
    <div class="sh-timetable-periods-wrap" ref={printCopy ? undefined : grid.ref}
         onKeyDown={printCopy ? undefined : grid.onKeyDown}
         onFocusIn={printCopy ? undefined : grid.onFocusIn}>
      <table class="sh-timetable-periods" style={{ '--tt-cols': String(days.length) }}>
        <caption class="sr-only">{tt.name}</caption>
        <thead>
          <tr>
            <th scope="col" class="sh-timetable-periods__corner">
              <span class="sr-only">{t('timetable.grid.period')}</span>
            </th>
            {days.map(d => (
              <th key={d} scope="col"
                  class={d === today ? 'sh-timetable-periods__today' : undefined}>
                <DayHeading weekday={d} id={headId(d)} today={d === today}
                            onAdd={() => onAddDay(d)} menu={dayMenu?.(d)} blocked={addBlocked(d)} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(row => {
            if (row.band) {
              // The same break (start / end / kind) on every visible day —
              // edited together so the days stay aligned.
              const group = days.map(d => tt.entries.find(e =>
                e.weekday === d && e.start === row.start && e.end === row.end
                && e.kind === 'break')).filter((e): e is TimetableEntry => !!e)
              const first = group[0]
              return (
                <tr key={row.key} class="sh-timetable-periods__band">
                  <th scope="row">{formatRange(row.start, row.end)}</th>
                  <td colSpan={days.length}>
                    <button type="button" class="sh-timetable-band"
                            data-tt-nav="" data-day="all" data-start={toMinutes(row.start)} data-kind="band"
                            aria-disabled={bandInert ? 'true' : undefined}
                            onClick={() => { if (first && !bandInert) onEdit(first, group.map(e => e.id)) }}>
                      {first?.icon && <span aria-hidden="true">{first.icon} </span>}
                      {row.title || t('timetable.kind.break')}
                    </button>
                  </td>
                </tr>
              )
            }
            return (
              <tr key={row.key}>
                <th scope="row" class="sh-timetable-periods__slot">
                  {row.label && <span class="sh-timetable-periods__label">{row.label}</span>}
                  <span class="sh-timetable-periods__time">{formatRange(row.start, row.end)}</span>
                </th>
                {days.map(d => {
                  const cell = row.cells[d]
                  if (cell === 'merged') return null
                  if (!editable && cell !== null && isUntitledLesson(cell.entry)) {
                    return (
                      <td key={d} rowSpan={cell.rowSpan > 1 ? cell.rowSpan : undefined}
                          class={`sh-timetable-periods__empty${d === today ? ' sh-timetable-periods__today' : ''}`} />
                    )
                  }
                  if (cell === null && !editable) {
                    return (
                      <td key={d} class={`sh-timetable-periods__empty${d === today ? ' sh-timetable-periods__today' : ''}`} />
                    )
                  }
                  if (cell === null) {
                    const blocked = addBlocked(d)
                    return (
                      <td key={d} class={`sh-timetable-periods__empty${d === today ? ' sh-timetable-periods__today' : ''}`}>
                        <button
                          type="button"
                          class="sh-timetable-cell-add"
                          data-tt-nav="" data-day={d} data-start={toMinutes(row.start)} data-kind="empty"
                          aria-label={blockedLabel(t(week ? 'timetable.week.add_at' : 'timetable.grid.add_at', {
                            day: weekdayName(d, 'long'), time: row.start,
                          }), blocked)}
                          aria-disabled={blocked ? 'true' : undefined}
                          title={blocked ?? undefined}
                          onClick={() => { if (!blocked) onAddAt(d, toMinutes(row.start)) }}
                        >
                          <span aria-hidden="true">+</span>
                        </button>
                      </td>
                    )
                  }
                  return (
                    <td key={d} rowSpan={cell.rowSpan > 1 ? cell.rowSpan : undefined}
                        class={d === today ? 'sh-timetable-periods__today' : undefined}>
                      <LessonBlock tt={tt} entry={cell.entry} variant="cell"
                                   picture={picture} spanEnd={cell.end} runIds={cell.ids}
                                   onOpen={(e, run) => (run ? onEdit(e, undefined, run) : onEdit(e))} />
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
