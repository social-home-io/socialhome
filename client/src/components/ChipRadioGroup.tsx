/**
 * ChipRadioGroup — a row of pill chips that behaves as an ARIA
 * radiogroup: one Tab stop (the checked chip), arrow keys move focus
 * and selection together (wrapping), Home / End jump to the ends.
 *
 * Used by the Settings language and week-start pickers. When ``value``
 * changes while focus is inside the group (e.g. a failed save rolled
 * the choice back) focus follows the checked chip, so the group's only
 * Tab stop is never left unfocused.
 */
import { useEffect, useRef } from 'preact/hooks'

export interface ChipRadioOption<T extends string> {
  value: T
  label: string
  title?: string
}

interface ChipRadioGroupProps<T extends string> {
  options: ReadonlyArray<ChipRadioOption<T>>
  value: T
  onChange: (value: T) => void
  ariaLabel?: string
  labelledBy?: string
}

export function ChipRadioGroup<T extends string>({
  options, value, onChange, ariaLabel, labelledBy,
}: ChipRadioGroupProps<T>) {
  const group = useRef<HTMLDivElement>(null)

  const radioAt = (idx: number): HTMLElement | undefined =>
    group.current?.querySelectorAll<HTMLElement>('[role="radio"]')[idx]

  useEffect(() => {
    const root = group.current
    if (!root || !root.contains(document.activeElement)) return
    radioAt(options.findIndex(o => o.value === value))?.focus()
  }, [value, options])

  const onKeyDown = (e: KeyboardEvent, idx: number) => {
    const last = options.length - 1
    let next: number
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = idx === last ? 0 : idx + 1
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        next = idx === 0 ? last : idx - 1
        break
      case 'Home':
        next = 0
        break
      case 'End':
        next = last
        break
      default:
        return
    }
    e.preventDefault()
    radioAt(next)?.focus()
    onChange(options[next].value)
  }

  return (
    <div class="sh-locale-options" role="radiogroup" ref={group}
         aria-label={ariaLabel} aria-labelledby={labelledBy}>
      {options.map((o, idx) => {
        const checked = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            title={o.title}
            class={checked ? 'sh-locale-option sh-locale-option--active' : 'sh-locale-option'}
            onKeyDown={e => onKeyDown(e, idx)}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
