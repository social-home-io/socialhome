/**
 * QuickAddBar — the "type, Enter, repeat" add row at the top of an
 * Organize list (shopping items; tasks and board columns next).
 *
 * A form with one labelled text input and a primary submit button
 * that stays disabled while the input is blank. ``onValueChange``
 * also hands over the input element, for callers that track the
 * caret (Shopping's ``@ store`` autocomplete). Any other input
 * attribute or handler goes through ``inputProps``.
 */
import type { Ref, InputHTMLAttributes } from 'preact'
import { Button } from '@/components/Button'

interface QuickAddBarProps {
  value: string
  onValueChange: (value: string, input: HTMLInputElement) => void
  /** Runs on Enter / button press when the value isn't blank. */
  onSubmit: () => void
  inputLabel: string
  submitLabel: string
  placeholder?: string
  /** Lands on the text input: Preact 11 passes ``ref`` through as a prop. */
  ref?: Ref<HTMLInputElement>
  // The input is always ``type="text"``; ``type`` / ``role`` / ``list`` stay
  // out so Preact's ARIA-in-HTML input typings can check the element.
  inputProps?: Omit<
    InputHTMLAttributes<HTMLInputElement>,
    'value' | 'onInput' | 'ref' | 'type' | 'role' | 'list'
  >
  class?: string
}

export function QuickAddBar({
  value, onValueChange, onSubmit, inputLabel, submitLabel, placeholder,
  ref, inputProps, class: extra,
}: QuickAddBarProps) {
  const blank = !value.trim()
  return (
    <form
      class={extra ? `sh-quick-add ${extra}` : 'sh-quick-add'}
      onSubmit={(e) => {
        e.preventDefault()
        if (!blank) onSubmit()
      }}
    >
      <input
        type="text"
        autoComplete="off"
        {...inputProps}
        ref={ref}
        value={value}
        placeholder={placeholder}
        aria-label={inputLabel}
        onInput={(e) => {
          const el = e.currentTarget as HTMLInputElement
          onValueChange(el.value, el)
        }}
      />
      <Button type="submit" disabled={blank}>{submitLabel}</Button>
    </form>
  )
}
