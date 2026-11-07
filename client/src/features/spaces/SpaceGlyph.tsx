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
  /** Class for the ``<img>`` when the picture shows. */
  imgClass?: string
}

export function SpaceGlyph({ iconUrl, emoji, name, imgClass }: Props) {
  if (iconUrl) return <img class={imgClass} src={iconUrl} alt="" loading="lazy" />
  if (isHaSymbol(emoji)) return <HaLogo />
  return <>{spaceGlyph(emoji, name)}</>
}
