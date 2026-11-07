/**
 * SpaceGlyph — what a space looks like in one small spot: its uploaded
 * picture when it has one (that is what the space shows everywhere),
 * else the Home Assistant logo when that is its icon, else its emoji,
 * else the first letter of its name (``spaceGlyph``).
 *
 * Renders only the content; the caller keeps its own tile (size, shape,
 * background), so every surface stays styled as before. Decorative — the
 * space's name is always written beside it.
 */
import { HaLogo } from './HaLogo'
import { isHaSymbol, spaceGlyph } from './spaceGlyph'

interface Props {
  /** The uploaded picture (``icon_url``), signed; null / absent = none. */
  iconUrl?: string | null
  emoji?: string | null
  name?: string | null
}

export function SpaceGlyph({ iconUrl, emoji, name }: Props) {
  if (iconUrl) return <img src={iconUrl} alt="" loading="lazy" />
  if (isHaSymbol(emoji)) return <HaLogo />
  return <>{spaceGlyph(emoji, name)}</>
}

/** The symbol before a space's name in running text (a table cell, a
 *  toggle's label): nothing when the space has neither a picture nor an
 *  emoji — the name alone, as before — else ``SpaceGlyph`` at text size
 *  (``.sh-space-inline-glyph``), hidden from assistive tech. */
export function SpaceInlineGlyph({ iconUrl, emoji, name }: Props) {
  if (!iconUrl && !emoji) return null
  return (
    <span class="sh-space-inline-glyph" aria-hidden="true">
      <SpaceGlyph iconUrl={iconUrl} emoji={emoji} name={name} />
    </span>
  )
}
