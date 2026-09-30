/**
 * A day column's heading — the localised short weekday plus a compact
 * "+" that adds a lesson after the day's last one. Today's heading is
 * tinted when the timetable is in effect today.
 */
import { t } from '@/i18n/i18n'
import { weekdayName } from './time'

interface Props {
  weekday: number
  id: string
  today: boolean
  onAdd: () => void
}

export function DayHeading({ weekday, id, today, onAdd }: Props) {
  const long = weekdayName(weekday, 'long')
  return (
    <div class={`sh-timetable-dayhead${today ? ' sh-timetable-dayhead--today' : ''}`}>
      <span class="sh-timetable-dayhead__name" id={id}>
        <span aria-hidden="true">{weekdayName(weekday, 'short')}</span>
        <span class="sr-only">{long}</span>
        {today && <span class="sr-only">{`, ${t('timetable.grid.today')}`}</span>}
      </span>
      <button
        type="button"
        class="sh-timetable-add"
        aria-label={t('timetable.grid.add_on', { day: long })}
        title={t('timetable.grid.add_lesson')}
        onClick={onAdd}
      >
        <span aria-hidden="true">+</span>
      </button>
    </div>
  )
}
