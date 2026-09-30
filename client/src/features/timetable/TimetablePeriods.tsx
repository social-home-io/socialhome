/**
 * Periods layout — the classic school table: one row per slot ("1.",
 * "08:00–08:45"), one column per day, thin spanning break bands, and
 * double lessons merged with ``rowSpan``.
 */
import { useMemo } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { DayHeading } from './DayHeading'
import { LessonBlock } from './LessonBlock'
import { periodRows } from './layout'
import { formatRange, toMinutes, weekdayName } from './time'

interface Props {
  tt: Timetable
  days: number[]
  today: number | null
  picture: boolean
  onEdit: (entry: TimetableEntry, group?: string[]) => void
  onAddAt: (weekday: number, startMin: number) => void
  onAddDay: (weekday: number) => void
}

export function TimetablePeriods({ tt, days, today, picture, onEdit, onAddAt, onAddDay }: Props) {
  const daysKey = days.join(',')
  // Recomputed per timetable version (each version is a new object).
  const rows = useMemo(() => periodRows(tt, days), [tt, daysKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const headId = (d: number) => `sh-tt-${tt.id}-p-${d}`
  return (
    <div class="sh-timetable-periods-wrap">
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
                            onAdd={() => onAddDay(d)} />
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
                            onClick={() => first && onEdit(first, group.map(e => e.id))}>
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
                  if (cell === null) {
                    return (
                      <td key={d} class={`sh-timetable-periods__empty${d === today ? ' sh-timetable-periods__today' : ''}`}>
                        <button
                          type="button"
                          class="sh-timetable-cell-add"
                          aria-label={t('timetable.grid.add_at', {
                            day: weekdayName(d, 'long'), time: row.start,
                          })}
                          onClick={() => onAddAt(d, toMinutes(row.start))}
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
                                   picture={picture} spanEnd={cell.end} onOpen={onEdit} />
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
