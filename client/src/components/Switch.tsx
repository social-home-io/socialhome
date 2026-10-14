/**
 * Switch — the one on/off switch of the SPA (``input.sh-switch`` in
 * ``app.css``).
 *
 * The checkbox *is* the graphic, so clicking or tapping the switch itself
 * always toggles it, wrapped in a ``<label>`` or not. (The old markup — a
 * 0×0 input next to a painted ``<span>`` — was only clickable where a
 * caller remembered a wrapping label; Admin › Dashboard's rows forgot.)
 *
 * Controlled: ``onChange`` gets the requested value and the switch keeps
 * showing ``checked`` until the parent re-renders with a new one, so a
 * caller that asks the server (or a confirm) first never shows a state it
 * hasn't accepted. Space toggles (native checkbox); Enter does too, as the
 * ARIA switch pattern allows.
 *
 * Name it with ``aria-label``, ``aria-labelledby`` or a ``<label for>`` /
 * wrapping ``<label>``; put hints in ``aria-describedby``.
 */
import type { JSX } from 'preact'

export interface SwitchProps extends Omit<
  JSX.InputHTMLAttributes<HTMLInputElement>,
  'type' | 'role' | 'checked' | 'onChange' | 'onKeyDown' | 'class' | 'className' | 'children'
> {
  checked: boolean
  onChange: (next: boolean) => void
}

export function Switch({ checked, onChange, ...rest }: SwitchProps) {
  return (
    <input
      {...rest}
      type="checkbox"
      role="switch"
      class="sh-switch"
      checked={checked}
      onChange={(e) => {
        // Hold the shown state until the parent re-renders with a new one.
        e.currentTarget.checked = checked
        if (!e.currentTarget.disabled) onChange(!checked)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && !e.currentTarget.disabled) {
          // Enter on a checkbox would submit a surrounding form instead.
          e.preventDefault()
          e.currentTarget.click()
        }
      }}
    />
  )
}
