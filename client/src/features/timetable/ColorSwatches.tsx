/**
 * ColorSwatches — pick one of the twelve timetable colour tokens.
 *
 * Real radio inputs (visually hidden) give arrow-key navigation and
 * screen-reader semantics for free; each swatch shows the token's
 * light / dark tint via its ``.sh-timetable-c--<token>`` class. The
 * optional leading "none" swatch maps to ``null`` (automatic colour
 * for an entry, no colour for a timetable).
 */
import { t } from '@/i18n/i18n'
import type { TimetableColor } from '@/types'
import { TIMETABLE_COLORS, colorClass } from './colors'

interface Props {
  name: string
  value: TimetableColor | null
  onChange: (color: TimetableColor | null) => void
  /** Label of the ``null`` option; omit to hide it. */
  noneLabel?: string
  legend?: string
}

export function ColorSwatches({ name, value, onChange, noneLabel, legend }: Props) {
  return (
    <fieldset class="sh-timetable-swatches">
      <legend>{legend ?? t('timetable.color.legend')}</legend>
      <div class="sh-timetable-swatches__row">
        {noneLabel && (
          <label class={`sh-timetable-swatch sh-timetable-swatch--none${value === null ? ' is-on' : ''}`}
                 title={noneLabel}>
            <input type="radio" class="sh-timetable-swatch__input" name={name}
                   checked={value === null} onChange={() => onChange(null)} />
            <span class="sr-only">{noneLabel}</span>
          </label>
        )}
        {TIMETABLE_COLORS.map(c => {
          const label = t(`timetable.color.${c}`)
          return (
            <label key={c} title={label}
                   class={`sh-timetable-swatch ${colorClass(c)}${value === c ? ' is-on' : ''}`}>
              <input type="radio" class="sh-timetable-swatch__input" name={name}
                     value={c} checked={value === c} onChange={() => onChange(c)} />
              <span class="sr-only">{label}</span>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}
