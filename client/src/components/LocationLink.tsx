/**
 * LocationLink — render a calendar event's free-form ``location`` as
 * a tap-actionable link.
 *
 * Heuristic:
 *
 * 1. If the text parses as an ``http(s)://`` URL, link to it
 *    directly (Zoom, Meet, hotel page, …).
 * 2. Else, link to a Google Maps search of the raw text — the maps
 *    app is what most users want when they tap a venue / address /
 *    room name. Google's universal-link form
 *    (``https://www.google.com/maps/search/?api=1&query=<…>``)
 *    bounces to the native maps app on iOS / Android when one is
 *    installed, and falls back to the web UI otherwise.
 *
 * ``target="_blank"`` + ``rel="noopener noreferrer"`` keep the SPA
 * shell intact and stop the new tab from reaching back into the
 * opener. The click handler stops propagation so a parent (e.g. the
 * calendar row's expand toggle) doesn't fire on the link tap.
 */

const URL_LIKE = /^(https?:\/\/|www\.)\S+$/i

function locationHref(value: string): string {
  const trimmed = value.trim()
  if (URL_LIKE.test(trimmed)) {
    return trimmed.startsWith('www.') ? `https://${trimmed}` : trimmed
  }
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(trimmed)}`
}

export function LocationLink({
  value,
  class: className,
  label,
}: {
  value: string
  class?: string
  /** Optional override for the accessible name; defaults to ``Location: <value>``. */
  label?: string
}) {
  const trimmed = value.trim()
  if (!trimmed) return null
  return (
    <a
      class={className}
      href={locationHref(trimmed)}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={label ?? `Location: ${trimmed}`}
      onClick={(e) => e.stopPropagation()}
    >
      <span aria-hidden="true">📍 </span>
      {trimmed}
    </a>
  )
}
