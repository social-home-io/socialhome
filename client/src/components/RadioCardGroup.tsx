/**
 * RadioCardGroup — an accessible radio group rendered as a vertical list of
 * cards (icon + title + subtitle) instead of a text-heavy ``<select>``.
 *
 * Each card wraps a real ``<input type="radio">`` (kept in the DOM, not
 * removed) so native keyboard arrow-navigation and screen-reader semantics
 * come for free; the visual radio dot, icon, title and subtitle sit beside
 * it. Group the cards by passing a shared ``name``. Controlled via
 * ``value`` + ``onChange`` (works directly with a signal:
 * ``value={sig.value} onChange={v => sig.value = v}``).
 */
import type { JSX } from 'preact'

export interface RadioCardOption {
  value: string
  /** Emoji or short glyph shown at the leading edge of the card. */
  icon: string
  title: string
  subtitle: string
  /** When true, this single option is shown but not selectable (e.g. a
   *  join mode that doesn't apply to the chosen visibility). */
  disabled?: boolean
  /** CSS font stack for the icon + title — lets a font picker show each
   *  choice in its own face. */
  fontFamily?: string
}

export function RadioCardGroup({
  legend,
  name,
  value,
  options,
  onChange,
  disabled,
}: {
  legend: string
  name: string
  value: string
  options: RadioCardOption[]
  onChange: (value: string) => void
  disabled?: boolean
}): JSX.Element {
  return (
    <fieldset class="sh-radio-card-group" disabled={disabled}>
      <legend class="sh-radio-card-group__legend">{legend}</legend>
      {options.map((opt) => {
        const selected = value === opt.value
        const optDisabled = disabled || opt.disabled
        return (
          <label
            key={opt.value}
            class={
              'sh-radio-card'
              + (selected ? ' sh-radio-card--selected' : '')
              + (optDisabled ? ' sh-radio-card--disabled' : '')
            }
          >
            <input
              type="radio"
              class="sh-radio-card__input"
              name={name}
              value={opt.value}
              checked={selected}
              disabled={optDisabled}
              onChange={() => onChange(opt.value)}
            />
            <span class="sh-radio-card__icon" aria-hidden="true"
                  style={opt.fontFamily ? { fontFamily: opt.fontFamily } : undefined}>
              {opt.icon}
            </span>
            <span class="sh-radio-card__body">
              <span class="sh-radio-card__title"
                    style={opt.fontFamily ? { fontFamily: opt.fontFamily } : undefined}>
                {opt.title}
              </span>
              <span class="sh-radio-card__subtitle">{opt.subtitle}</span>
            </span>
          </label>
        )
      })}
    </fieldset>
  )
}
