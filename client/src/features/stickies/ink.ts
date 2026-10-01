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

/** The palette's first colour — used for an unparseable stored value. */
export const DEFAULT_STICKY_HEX = '#FFF9B1'
/** ``--sh-sticky-ink`` */
export const DARK_INK = '#1F1A14'
/** ``--sh-sticky-ink-light`` */
export const LIGHT_INK = '#FBF7F0'
/** WCAG AA for normal text. */
const MIN_CONTRAST = 4.5

export type StickyInk = 'dark' | 'light'

/** ``#RGB`` / ``#RRGGBB`` (any case, surrounding space ignored) → RGB. */
export function parseHex(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return null
  const h = m[1].length === 3 ? m[1].split('').map(c => c + c).join('') : m[1]
  return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)) as [number, number, number]
}

/** WCAG relative luminance of an sRGB colour. */
export function relativeLuminance([r, g, b]: readonly number[]): number {
  const lin = (v: number) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** WCAG contrast ratio of two hex colours (invalid → the default). */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(parseHex(a) ?? parseHex(DEFAULT_STICKY_HEX)!)
  const lb = relativeLuminance(parseHex(b) ?? parseHex(DEFAULT_STICKY_HEX)!)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
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
  if (dark >= MIN_CONTRAST) return 'dark'
  return contrastRatio(LIGHT_INK, bg) > dark ? 'light' : 'dark'
}

/** The class that switches a note (or the dialog preview) to the light
 *  ink, or ``''``. */
export function inkClass(hex: string): string {
  return inkFor(hex) === 'light' ? 'sh-ink-light' : ''
}
