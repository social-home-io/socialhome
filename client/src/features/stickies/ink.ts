/**
 * Ink for a sticky note's colour.
 *
 * Note colours are user data: the palette is all light pastels, but a
 * stored hex can be anything (an older client, another household's
 * picker). The fixed dark ink (``--sh-sticky-ink``) reads on every
 * palette colour; on a dark custom colour it would not, so such a note
 * switches to the light ink (``.sh-ink-light`` → ``--sh-sticky-ink-light``).
 *
 * The hex values here mirror the tokens in ``styles/tokens.css`` (light
 * theme) and only drive the choice — the rendered colours come from the
 * tokens. Pure, no DOM.
 */
import { AA_TEXT, contrast, parseHex } from '@/utils/contrast'

/** The palette's first colour — used for an unparseable stored value. */
export const DEFAULT_STICKY_HEX = '#FFF9B1'
/** ``--sh-sticky-ink`` */
export const DARK_INK = '#1F1A14'
/** ``--sh-sticky-ink-light`` */
export const LIGHT_INK = '#FBF7F0'

export type StickyInk = 'dark' | 'light'

/** WCAG contrast ratio of two hex colours (invalid → the default). */
export function contrastRatio(a: string, b: string): number {
  const fallback = parseHex(DEFAULT_STICKY_HEX)!
  return contrast(parseHex(a) ?? fallback, parseHex(b) ?? fallback)
}

/** The colour to paint a note with: its hex, or the default if invalid. */
export function stickyBackground(hex: string): string {
  return parseHex(hex ?? '') ? hex.trim() : DEFAULT_STICKY_HEX
}

/** Dark ink unless it falls below 4.5:1 on this colour — then light,
 *  unless light would read even worse (a mid grey). */
export function inkFor(hex: string): StickyInk {
  const bg = stickyBackground(hex)
  const dark = contrastRatio(DARK_INK, bg)
  if (dark >= AA_TEXT) return 'dark'
  return contrastRatio(LIGHT_INK, bg) > dark ? 'light' : 'dark'
}

/** The class that switches a note (or the dialog preview) to the light
 *  ink, or ``''``. */
export function inkClass(hex: string): string {
  return inkFor(hex) === 'light' ? 'sh-ink-light' : ''
}
