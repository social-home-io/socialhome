/**
 * WeekStartField — Monday / Sunday for one timetable. When the choice
 * differs from the viewer's own week-start preference a hint says so,
 * because the columns then run in a different order than their
 * calendar.
 */
import { t } from '@/i18n/i18n'
import { currentWeekStart, type WeekStart } from '@/utils/week'

interface Props {
  name: string
  value: WeekStart
  onChange: (ws: WeekStart) => void
}

export function WeekStartField({ name, value, onChange }: Props) {
  const mine = currentWeekStart()
  return (
    <fieldset class="sh-timetable-weekstart">
      <legend>{t('timetable.week_start.legend')}</legend>
      <div class="sh-timetable-weekstart__row">
        {([0, 6] as const).map(ws => (
          <label key={ws} class={`sh-timetable-kind__opt${value === ws ? ' is-on' : ''}`}>
            <input type="radio" name={name} class="sh-timetable-kind__input"
                   checked={value === ws} onChange={() => onChange(ws)} />
            {t(ws === 0 ? 'timetable.week_start.mon' : 'timetable.week_start.sun')}
          </label>
        ))}
      </div>
      {value !== mine && (
        <p class="sh-form-hint">
          {t(value === 6 ? 'timetable.week_start.differs_sun' : 'timetable.week_start.differs_mon')}
        </p>
      )}
    </fieldset>
  )
}
