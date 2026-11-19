/**
 * LengthCounter — "n/max" under a capped text field, shown once the text
 * nears its cap (over 80 %), like the post composer's counter.
 *
 * A field with ``maxLength`` stops taking input at the cap without a
 * word; the counter says why typing stopped.
 */

interface Props {
  value: string
  max: number
  id?: string
}

export function LengthCounter({ value, max, id }: Props) {
  const n = value.length
  if (n <= max * 0.8) return null
  return (
    <span id={id} class={`sh-length-counter${n >= max ? ' sh-length-counter--full' : ''}`}
          aria-live="polite">
      {n}/{max}
    </span>
  )
}
