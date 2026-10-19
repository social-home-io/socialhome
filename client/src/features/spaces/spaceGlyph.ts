/**
 * How a space is drawn when it has no picture, and how its tier is
 * marked — one place, so no surface invents its own fallback.
 *
 * - ``spaceGlyph``: the space's emoji, else the first letter of its name
 *   (as the hero does). Never a globe or a house: those read as "public"
 *   or "household" and would mislabel a private space.
 * - ``tierIcon``: the mark of the space's real tier — 🔐 private,
 *   🏠 household, 🤝 public, 🌐 global (the Discover scope chips' set).
 */

/** The emoji, else the name's first letter (upper-case), else "•". */
export function spaceGlyph(emoji: string | null | undefined, name: string | null | undefined): string {
  if (emoji) return emoji
  const first = Array.from((name ?? '').trim())[0]
  return first ? first.toLocaleUpperCase() : '•'
}

/** The tier's mark, or ``null`` for a tier this build doesn't know. */
export function tierIcon(tier: string | null | undefined): string | null {
  switch (tier) {
    case 'private':   return '🔐'
    case 'household': return '🏠'
    case 'public':    return '🤝'
    case 'global':    return '🌐'
    default:          return null
  }
}
