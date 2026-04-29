import { LogoMark } from './LogoMark'

/**
 * Spinner — the brand mark itself, animating. The chat-notch
 * breathes and the three feed dots cascade in a wave so it reads as
 * "Social Home is thinking" rather than a generic loading glyph.
 *
 * `size` historically meant "dot diameter in px" for the old
 * three-dot spinner. It now drives the LogoMark size. Callers
 * passing the legacy small numbers (6–12) get a logo-sized
 * proportional fallback so the visual weight stays roughly the
 * same — the multiplier matches the dots-to-logo perceived area.
 */
export function Spinner({ size = 8, label = 'Loading…' }: {
  size?: number
  label?: string
}) {
  // Old API: size = dot diameter (~6–12). New API uses LogoMark
  // pixels directly (~24–64). When a small legacy size is given,
  // scale up to a comparable logo size; for callers that already
  // pass logo-sized values (>=24) honour them as-is.
  const logoSize = size < 24 ? Math.max(24, size * 4) : size
  return (
    <span
      class="sh-spinner"
      role="status"
      aria-label={label}
    >
      <LogoMark size={logoSize} loading ariaLabel={label} />
    </span>
  )
}
