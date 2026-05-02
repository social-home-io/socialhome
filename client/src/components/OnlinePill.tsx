/**
 * OnlinePill — compact author online-status pill rendered in feed item
 * bylines and elsewhere where a coloured dot alone isn't enough context.
 *
 * Visually echoes the website's tape-card aesthetic: cream surface,
 * 1 px terracotta accent on the leading edge, tiny status dot, then
 * the label and (optionally) the user's zone — e.g.
 *
 *     [● Online · @ Home]    [○ Idle · @ Office]    nothing when offline
 *
 * Reads from the existing :class:`presence` signal store so updates
 * land live as ``user.online`` / ``user.idle`` / ``user.offline``
 * frames arrive. Looks up the row by ``user_id`` because posts carry
 * an opaque author id, not a username.
 *
 * Zone suffix is included only when ``showZone`` is true. Household
 * feed surfaces it (HA zone names are household-private); space feed
 * surfaces it only if the space has the per-space zone catalogue
 * (§23.8.7) and the row's ``zone_name`` is non-null. Set ``showZone``
 * to false on space surfaces to suppress HA zone names — caller's
 * choice.
 */
import { presence } from '@/store/presence'

interface Props {
  user_id?: string | null
  /** If true, append "· @ <zone>" when the row's zone_name is set. */
  showZone?: boolean
  /** Render a compact one-glyph version (just the dot) — used in
   *  tight spots like the comment byline. */
  compact?: boolean
}

export function OnlinePill({ user_id, showZone = true, compact = false }: Props) {
  if (!user_id) return null
  // Locate by user_id — the store is keyed on username but every
  // entry carries user_id as a secondary index.
  let row: ReturnType<typeof Object.values<typeof presence.value[string]>>[number] | undefined
  for (const v of Object.values(presence.value)) {
    if (v.user_id === user_id) { row = v; break }
  }
  if (!row || !row.is_online) return null
  const variant: 'online' | 'idle' = row.is_idle ? 'idle' : 'online'
  const label = variant === 'idle' ? 'Idle' : 'Online'
  const zoneLabel = showZone && row.zone_name ? row.zone_name : null

  if (compact) {
    return (
      <span
        class={`sh-online-pill sh-online-pill--compact sh-online-pill--${variant}`}
        title={zoneLabel ? `${label} · ${zoneLabel}` : label}
        aria-label={zoneLabel ? `${label}, in zone ${zoneLabel}` : label}
      >
        <span class={`sh-online-pill-dot sh-online-pill-dot--${variant}`} />
      </span>
    )
  }

  return (
    <span
      class={`sh-online-pill sh-online-pill--${variant}`}
      aria-label={zoneLabel ? `${label}, in zone ${zoneLabel}` : label}
    >
      <span class={`sh-online-pill-dot sh-online-pill-dot--${variant}`} aria-hidden="true" />
      <span class="sh-online-pill-label">{label}</span>
      {zoneLabel && (
        <>
          <span class="sh-online-pill-sep" aria-hidden="true">·</span>
          <span class="sh-online-pill-zone">@ {zoneLabel}</span>
        </>
      )}
    </span>
  )
}
