/**
 * List view — a plain semantic table per day (time · lesson · room ·
 * teacher). The accessible / print-friendly rendering of the same
 * data; always available from the grid toolbar and printed instead of
 * the visual grid.
 */
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { displayTitle } from './labels'
import { dayEntries } from './layout'
import { formatRange, weekdayName } from './time'

interface Props {
  tt: Timetable
  days: number[]
  onEdit?: (entry: TimetableEntry) => void
  /** The print fallback under the visual grid: hidden on screen (and
   *  from assistive tech, which already has the grid). */
  printOnly?: boolean
}

export function TimetableList({ tt, days, onEdit, printOnly = false }: Props) {
  return (
    <div
      class={`sh-timetable-list${printOnly ? ' sh-timetable-print-only' : ''}`}
      aria-hidden={printOnly ? 'true' : undefined}
    >
      {days.map(d => {
        const entries = dayEntries(tt, d)
        return (
          <table key={d} class="sh-timetable-list__day">
            <caption>{weekdayName(d, 'long')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('timetable.list.time')}</th>
                <th scope="col">{t('timetable.list.lesson')}</th>
                <th scope="col">{t('timetable.list.room')}</th>
                <th scope="col">{t('timetable.list.teacher')}</th>
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr><td colSpan={4} class="sh-muted">{t('timetable.list.empty_day')}</td></tr>
              )}
              {entries.map(e => (
                <tr key={e.id} class={e.kind === 'break' ? 'sh-timetable-list__break' : undefined}>
                  <th scope="row">{formatRange(e.start, e.end)}</th>
                  <td>
                    {e.icon && <span aria-hidden="true">{e.icon} </span>}
                    {onEdit ? (
                      <button type="button" class="sh-link" onClick={() => onEdit(e)}>
                        {displayTitle(e) || t('timetable.aria.empty_slot')}
                      </button>
                    ) : (displayTitle(e) || t('timetable.aria.empty_slot'))}
                  </td>
                  <td>{e.room ?? ''}</td>
                  <td>{e.teacher ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      })}
    </div>
  )
}
