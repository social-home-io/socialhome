/**
 * CopyTargets — "copy this day onto…": ``aria-pressed`` toggles for the
 * timetable's other days (DaysPicker style, in week-start order) plus
 * the "With subjects" checkbox. Off by default: copying just the times
 * is the common case (every day has the same bell schedule, different
 * subjects). Shared by the day builder and the "Copy to other days"
 * dialog.
 */
import { t } from '@/i18n/i18n'
import type { Timetable } from '@/types'
import { orderedDays, weekdayName } from './time'

interface Props {
  tt: Timetable
  source: number
  value: number[]
  onChange: (days: number[]) => void
  withSubjects: boolean
  onWithSubjects: (on: boolean) => void
  legend: string
  id: string
}

export function CopyTargets({
  tt, source, value, onChange, withSubjects, onWithSubjects, legend, id,
}: Props) {
  const days = orderedDays(tt.days, tt.week_start).filter(d => d !== source)
  if (days.length === 0) return null
  const toggle = (d: number) => onChange(value.includes(d)
    ? value.filter(x => x !== d)
    : [...value, d].sort((a, b) => a - b))
  const allOn = days.every(d => value.includes(d))
  return (
    <fieldset class="sh-timetable-copy">
      <legend>{legend}</legend>
      <div class="sh-timetable-days__custom">
        {days.map(d => {
          const on = value.includes(d)
          return (
            <button key={d} type="button" aria-pressed={on} aria-label={weekdayName(d, 'long')}
                    class={`sh-timetable-days__day${on ? ' is-on' : ''}`}
                    onClick={() => toggle(d)}>
              {weekdayName(d, 'short')}
            </button>
          )
        })}
        {days.length > 1 && (
          <button type="button" class="sh-chip sh-timetable-chip"
                  onClick={() => onChange(allOn ? [] : days)}>
            {t(allOn ? 'timetable.copy.none' : 'timetable.copy.all')}
          </button>
        )}
      </div>
      <label class="sh-timetable-check" for={`${id}-subjects`}>
        <input id={`${id}-subjects`} type="checkbox" checked={withSubjects}
               onChange={(e) => onWithSubjects((e.target as HTMLInputElement).checked)} />
        <span>
          {t('timetable.copy.with_subjects')}
          <span class="sh-form-hint sh-timetable-check__hint">{t('timetable.copy.with_subjects_hint')}</span>
        </span>
      </label>
    </fieldset>
  )
}
