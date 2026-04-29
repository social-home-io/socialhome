/**
 * Spinner — three pulsing dots, brand-tinted.
 *
 * Replaces the previous border-rotation circle, which inadvertently
 * showed the text "Loading..." inside it (the markup carried a
 * `<span class="sr-only">` but the project never defined that class).
 * The new dots animation is purely visual; the screen-reader label
 * lives on the wrapping `role="status"` element via `aria-label` so
 * assistive tech still announces "Loading" without the text being
 * paint-visible.
 */
export function Spinner({ size = 8, label = 'Loading…' }: {
  size?: number
  label?: string
}) {
  return (
    <span
      class="sh-spinner"
      role="status"
      aria-label={label}
      style={{ '--sh-spinner-dot': `${size}px` }}
    >
      <span class="sh-spinner-dot" />
      <span class="sh-spinner-dot" />
      <span class="sh-spinner-dot" />
    </span>
  )
}
