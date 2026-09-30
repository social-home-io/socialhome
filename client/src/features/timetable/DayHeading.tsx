/**
 * A day column's heading — the localised short weekday (plus the date
 * in week mode: "Mon 5"), a compact "+" that adds a lesson after the
 * day's last one, and a "⋯" menu with the day tools (Set up times…,
 * Copy to other days…). A visible ⋯ rather than a long-press: it is
 * discoverable, works with a mouse, keyboard and touch alike, and
 * needs no timing gesture. Today's heading is tinted when the
 * timetable is in effect today.
 */
import { useContext } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { dayOfMonth, fullDate } from './dates'
import { OverflowMenu, type MenuItem } from './OverflowMenu'
import { weekdayName } from './time'
import { WeekContext, blockedLabel } from './weekView'

interface Props {
  weekday: number
  id: string
  today: boolean
  onAdd: () => void
  /** Day tools for the ⋯ menu; omitted (week mode) → no menu. */
  menu?: MenuItem[]
  /** Why "+" can't add here (brush mode, a past or inactive week-mode
   *  day): it is then ``aria-disabled`` and says so. */
  blocked?: string | null
}

export function DayHeading({ weekday, id, today, onAdd, menu, blocked = null }: Props) {
  const week = useContext(WeekContext)
  const date = week?.dates[weekday]
  const long = weekdayName(weekday, 'long')
  return (
    <div class={`sh-timetable-dayhead${today ? ' sh-timetable-dayhead--today' : ''}`}>
      <span class="sh-timetable-dayhead__name" id={id}>
        <span aria-hidden="true">
          {weekdayName(weekday, 'short')}
          {date && <span class="sh-timetable-dayhead__date"> {dayOfMonth(date)}</span>}
        </span>
        <span class="sr-only">{date ? fullDate(date) : long}</span>
        {today && <span class="sr-only">{`, ${t('timetable.grid.today')}`}</span>}
      </span>
      <span class="sh-timetable-dayhead__tools">
        {menu && menu.length > 0 && (
          <OverflowMenu label={t('timetable.day.menu', { day: long })} items={menu} floating
                        triggerClass="sh-timetable-add sh-timetable-dayhead__more">
            <span aria-hidden="true">⋯</span>
          </OverflowMenu>
        )}
        <button
          type="button"
          class="sh-timetable-add"
          aria-label={blockedLabel(week
            ? t('timetable.week.add_on', { day: long })
            : t('timetable.grid.add_on', { day: long }), blocked)}
          title={blocked ?? t('timetable.grid.add_lesson')}
          aria-disabled={blocked ? 'true' : undefined}
          onClick={() => { if (!blocked) onAdd() }}
        >
          <span aria-hidden="true">+</span>
        </button>
      </span>
    </div>
  )
}
