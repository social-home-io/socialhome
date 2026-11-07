/**
 * How a space is drawn when it has no picture, and how its tier is
 * marked — one place, so no surface invents its own fallback.
 *
 * - ``spaceGlyph``: the space's emoji, else the first letter of its name
 *   (as the hero does). Never a globe or a house: those read as "public"
 *   or "household" and would mislabel a private space.
 * - ``tierIcon``: the mark of the space's real tier — 🔐 private,
 *   🏠 household, 🤝 public, 🌐 global (the Discover scope chips' set).
 * - ``HA_SPACE_SYMBOL``: the Home Assistant logo as a space "emoji".
 *
 * The Home Assistant symbol is stored in the space's ``emoji`` field as
 * 🏠 followed by VARIATION SELECTOR-15 (U+FE0E, "text presentation").
 * A household that knows it draws the logo (``SpaceGlyph``); one that
 * doesn't — an older build, a GFS directory, a text-only spot like an
 * ``<option>`` — simply shows a house. No new wire field or protocol
 * version: the value is an ordinary single emoji everywhere it travels.
 * The emoji picker never produces it (🏠 is emoji-presentation by
 * default, so a picked house carries no selector).
 */

/** The Home Assistant logo, as stored in a space's ``emoji`` field. */
export const HA_SPACE_SYMBOL = '\u{1F3E0}\uFE0E'

/** Is ``emoji`` the Home Assistant symbol (not a picked emoji)? */
export function isHaSymbol(emoji: string | null | undefined): boolean {
  return emoji === HA_SPACE_SYMBOL
}

/** The emoji to put before a space's name in plain text (a document
 *  title): a real emoji, never the Home Assistant symbol. */
export function titleEmoji(emoji: string | null | undefined): string | null {
  return emoji && !isHaSymbol(emoji) ? emoji : null
}

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
