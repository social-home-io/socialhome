/**
 * The favourite heart (♡, D12) as a drawn icon — the ♥ character turns
 * into a colour emoji on many phones and desktops, which clashed with the
 * line icons around it. Decorative: the control next to it carries the
 * words.
 */

/** The same path the viewer's ♡ button and the selection bar draw. */
export const HEART_PATH =
  'M12 19.5s-7.5-4.6-7.5-10a4.2 4.2 0 0 1 7.5-2.6 4.2 4.2 0 0 1 7.5 2.6c0 5.4-7.5 10-7.5 10Z'

export function HeartIcon({ size = 16, filled = true, class: cls }: {
  size?: number
  filled?: boolean
  class?: string
}) {
  return (
    <svg
      class={cls}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      fill={filled ? 'currentColor' : 'none'}
      stroke="currentColor"
      stroke-width="1.8"
      stroke-linejoin="round"
    >
      <path d={HEART_PATH} />
    </svg>
  )
}
