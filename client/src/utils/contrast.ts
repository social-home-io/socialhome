/**
 * WCAG 2.x colour-contrast helpers — pure, no DOM.
 *
 * Shared by the sticky-note ink choice (``features/stickies/ink.ts``)
 * and the text-on-filled-primary choice for custom space colours
 * (``utils/primaryFill.ts``).
 */

/** An sRGB colour, channels 0–255 (not rounded — color-mix keeps fractions). */
export type Rgb = readonly [number, number, number]

/** WCAG AA for normal-size text. */
export const AA_TEXT = 4.5

/** ``#RGB`` / ``#RRGGBB`` (any case, surrounding space ignored) → RGB. */
export function parseHex(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return null
  const h = m[1].length === 3 ? m[1].split('').map(c => c + c).join('') : m[1]
  return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)) as [number, number, number]
}

/** WCAG relative luminance of an sRGB colour. */
export function relativeLuminance([r, g, b]: Rgb): number {
  const lin = (v: number) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** WCAG contrast ratio of two colours (1–21). */
export function contrast(a: Rgb, b: Rgb): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/** ``color-mix(in srgb, a <weightA·100>%, b)``. */
export function mixRgb(a: Rgb, b: Rgb, weightA: number): Rgb {
  const w = (x: number, y: number) => x * weightA + y * (1 - weightA)
  return [w(a[0], b[0]), w(a[1], b[1]), w(a[2], b[2])]
}
