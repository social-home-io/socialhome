/**
 * List view — a plain semantic table per day (time · lesson · room ·
 * teacher). The accessible / print-friendly rendering of the same
 * data; always available from the grid toolbar and printed instead of
 * the visual grid.
 */
import { useContext } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { weekdayDate } from './dates'
import { displayTitle, isUntitledLesson } from './labels'
import { useTimetableScope } from './scope'
import { dayEntries } from './layout'
import { formatRange, weekdayName } from './time'
import { WeekContext, changesOf } from './weekView'

interface Props {
  tt: Timetable
  days: number[]
  onEdit?: (entry: TimetableEntry) => void
}

export function TimetableList({ tt, days, onEdit }: Props) {
  const week = useContext(WeekContext)
  const { editable } = useTimetableScope()
  return (
    <div
      class="sh-timetable-list"
    >
      {days.map(d => {
        const entries = dayEntries(tt, d).filter(e => editable || !isUntitledLesson(e))
        // Columns nobody fills only squeeze the others (narrow print
        // columns broke words mid-way).
        const hasRoom = entries.some(e => e.room)
        const hasTeacher = entries.some(e => e.teacher)
        const cols = 2 + (hasRoom ? 1 : 0) + (hasTeacher ? 1 : 0)
        return (
          <table key={d} class="sh-timetable-list__day">
            <caption>{week?.dates[d] ? weekdayDate(week.dates[d]) : weekdayName(d, 'long')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('timetable.list.time')}</th>
                <th scope="col">{t('timetable.list.lesson')}</th>
                {hasRoom && <th scope="col">{t('timetable.list.room')}</th>}
                {hasTeacher && <th scope="col">{t('timetable.list.teacher')}</th>}
              </tr>
            </thead>
            <tbody>
              {entries.length === 0 && (
                <tr><td colSpan={cols} class="sh-muted">{t('timetable.list.empty_day')}</td></tr>
              )}
              {entries.map(e => {
                const lesson = week?.lessons.get(e.id)
                const status = lesson?.status ?? 'normal'
                const name = displayTitle(e) || t('timetable.aria.empty_slot')
                const changes = lesson ? changesOf(lesson) : []
                return (
                <tr key={e.id} class={[
                  e.kind === 'break' ? 'sh-timetable-list__break' : '',
                  status !== 'normal' ? `sh-timetable-list__row--${status}` : '',
                ].filter(Boolean).join(' ') || undefined}>
                  <th scope="row">{formatRange(e.start, e.end)}</th>
                  <td>
                    {e.icon && <span aria-hidden="true">{e.icon} </span>}
                    {onEdit ? (
                      <button type="button" class="sh-link" onClick={() => onEdit(e)}>
                        {status === 'cancelled' ? <s>{name}</s> : name}
                      </button>
                    ) : (status === 'cancelled' ? <s>{name}</s> : name)}
                    {status === 'cancelled' && (
                      <span class="sh-timetable-badge sh-timetable-badge--cancelled sh-timetable-list__badge">
                        {t('timetable.week.cancelled_badge')}
                      </span>
                    )}
                    {status === 'added' && (
                      <span class="sh-timetable-badge sh-timetable-badge--added sh-timetable-list__badge">
                        {t('timetable.week.extra')}
                      </span>
                    )}
                    {changes.length > 0 && (
                      <span class="sh-timetable-list__change">{changes.join(' · ')}</span>
                    )}
                  </td>
                  {hasRoom && <td>{e.room ?? ''}</td>}
                  {hasTeacher && <td>{e.teacher ?? ''}</td>}
                </tr>
                )
              })}
            </tbody>
          </table>
        )
      })}
    </div>
  )
}
