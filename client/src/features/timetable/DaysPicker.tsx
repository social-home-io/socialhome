/**
 * DaysPicker — which weekdays a timetable covers.
 *
 * Preset chips (Mon–Fri, Mon–Sat, Every day) cover nearly every
 * school; "Custom" reveals seven ``aria-pressed`` day toggles in the
 * timetable's week-start order. The selection can never become empty —
 * un-pressing the last day is refused with a hint.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { weekdayOrder, type WeekStart } from '@/utils/week'
import { daysSummary, weekdayName } from './time'

const PRESETS: readonly number[][] = [
  [0, 1, 2, 3, 4],
  [0, 1, 2, 3, 4, 5],
  [0, 1, 2, 3, 4, 5, 6],
]

const same = (a: readonly number[], b: readonly number[]) =>
  a.length === b.length && [...a].sort().every((d, i) => d === [...b].sort()[i])

interface Props {
  value: number[]
  onChange: (days: number[]) => void
  weekStart: WeekStart
}

export function DaysPicker({ value, onChange, weekStart }: Props) {
  const matchesPreset = PRESETS.some(p => same(p, value))
  const [custom, setCustom] = useState(!matchesPreset)
  const [refused, setRefused] = useState(false)
  const showCustom = custom || !matchesPreset

  const toggle = (d: number) => {
    if (value.includes(d)) {
      if (value.length === 1) { setRefused(true); return }
      onChange(value.filter(x => x !== d))
    } else {
      onChange([...value, d].sort((a, b) => a - b))
    }
    setRefused(false)
  }

  return (
    <fieldset class="sh-timetable-days">
      <legend>{t('timetable.days.legend')}</legend>
      <div class="sh-timetable-days__presets">
        {PRESETS.map(p => {
          const on = !showCustom && same(p, value)
          return (
            <button key={p.length} type="button" aria-pressed={on}
                    class={`sh-chip sh-timetable-chip${on ? ' sh-chip--active' : ''}`}
                    onClick={() => { setCustom(false); setRefused(false); onChange(p) }}>
              {daysSummary(p, weekStart)}
            </button>
          )
        })}
        <button type="button" aria-pressed={showCustom}
                class={`sh-chip sh-timetable-chip${showCustom ? ' sh-chip--active' : ''}`}
                onClick={() => setCustom(true)}>
          {t('timetable.days.custom')}
        </button>
      </div>
      {showCustom && (
        <div class="sh-timetable-days__custom" role="group" aria-label={t('timetable.days.custom_aria')}>
          {weekdayOrder(weekStart).map(d => {
            const on = value.includes(d)
            return (
              <button key={d} type="button" aria-pressed={on}
                      aria-label={weekdayName(d, 'long')}
                      class={`sh-timetable-days__day${on ? ' is-on' : ''}`}
                      onClick={() => toggle(d)}>
                {weekdayName(d, 'short')}
              </button>
            )
          })}
        </div>
      )}
      <p class="sh-form-hint" aria-live="polite">{refused ? t('timetable.days.keep_one') : ''}</p>
    </fieldset>
  )
}
