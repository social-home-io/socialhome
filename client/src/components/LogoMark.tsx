/**
 * Inline SVG logo mark — stylised house silhouette with a chat
 * notch carved into the right roofline and a three-dot feed line
 * threaded through the floor. Encodes both meanings of "Social
 * Home" in one glyph; mirrors the website's `LogoMark.astro` so
 * the brand reads identically from marketing site to product.
 *
 * `currentColor` drives the house outline + dots so the mark
 * inherits the surrounding ink colour (light + dark mode); the
 * chat-notch is filled in `--sh-primary` so the icon stays warm
 * even on monochrome surfaces.
 */
interface LogoMarkProps {
  size?: number
  class?: string
  ariaLabel?: string
}

export function LogoMark({
  size = 32,
  class: className,
  ariaLabel = 'Social Home',
}: LogoMarkProps) {
  return (
    <svg
      class={className}
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label={ariaLabel}
    >
      <path
        d="M8 30 L32 8 L56 30 V52 a4 4 0 0 1 -4 4 H12 a4 4 0 0 1 -4 -4 Z"
        stroke="currentColor"
        stroke-width="3"
        stroke-linejoin="round"
        fill="none"
      />
      <path
        d="M44 22 H52 a3 3 0 0 1 3 3 V32 a3 3 0 0 1 -3 3 H49 L45 39 V35 H46 a0 0 0 0 0 -1 0 Z"
        fill="var(--sh-primary)"
      />
      <circle cx="20" cy="42" r="2.4" fill="currentColor" />
      <circle cx="32" cy="42" r="2.4" fill="currentColor" />
      <circle cx="44" cy="42" r="2.4" fill="currentColor" />
      <path
        d="M22 42 H42"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
        stroke-dasharray="0.5 4"
      />
    </svg>
  )
}
