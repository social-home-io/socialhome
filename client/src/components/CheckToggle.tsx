/**
 * CheckToggle — the one "done" tick for Organize rows (shopping items,
 * tasks).
 *
 * A ``<button role="checkbox">``: Space / Enter toggle it, screen
 * readers announce "checked / not checked". The button is the hit
 * area (44 px under a coarse pointer) while the visible round
 * ``__dot`` stays small, so a list reads as items, not as a wall of
 * controls. Checked, the dot fills with ``--sh-primary-fill`` and the
 * tick takes ``--sh-bg`` — a pair that holds contrast in both themes.
 *
 * It sits beside the row text, never wrapping it, so tapping the text
 * can't tick the item off by accident.
 */
interface CheckToggleProps {
  checked: boolean
  /** Accessible name, e.g. "Bought Milk". */
  label: string
  /** Receives the requested next state. */
  onChange: (next: boolean) => void
  disabled?: boolean
  class?: string
}

export function CheckToggle({
  checked, label, onChange, disabled, class: extra,
}: CheckToggleProps) {
  const cls = ['sh-check-toggle', checked ? 'sh-check-toggle--checked' : '', extra ?? '']
    .filter(Boolean).join(' ')
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked ? 'true' : 'false'}
      aria-label={label}
      class={cls}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span class="sh-check-toggle__dot" aria-hidden="true">
        {checked ? '✓' : ''}
      </span>
    </button>
  )
}
