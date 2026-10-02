/**
 * Text on a filled primary surface (buttons, avatars, chips, "mine"
 * bubbles) for a custom primary colour — pure, no DOM.
 *
 * ``tokens.css`` paints text on ``--sh-primary-fill`` with
 * ``--sh-on-primary-fill``, which defaults to ``var(--sh-bg)``: cream
 * on the deepened hearth in light mode, dark ember on the lifted hearth
 * in dark mode. That pairing is tuned for the brand hearth; a space can
 * pick any primary (``useSpaceTheme``), and a mid-tone one fails AA
 * under the dark ink (#3A7D6E ≈ 3.9:1, and only 4.0:1 under the cream
 * one). For such a primary this module
 * picks, per theme, the first ink that clears 4.5:1 on the fill — the
 * theme's background ink (``--sh-bg``), then its text ink
 * (``--sh-text``), then pure white / black (the cream and ember inks
 * are a notch short of the extremes, which is exactly the gap a
 * mid-tone primary falls into) — else the best of them, and a hover
 * fill that moves AWAY from that ink so hovering never lowers contrast.
 *
 * The hex values mirror ``tokens.css`` and only drive the choice; the
 * emitted properties reference the tokens themselves.
 */
import { AA_TEXT, contrast, mixRgb, parseHex, type Rgb } from './contrast'

export type FillTheme = 'light' | 'dark'
export type FillInk = 'bg' | 'text' | 'white' | 'black'

/** ``--sh-bg`` / ``--sh-text`` per theme (tokens.css). */
export const THEME_INKS: Record<FillTheme, Record<'bg' | 'text', string>> = {
  light: { bg: '#F4ECE0', text: '#1A1814' },
  dark:  { bg: '#1A1612', text: '#F1E9DA' },
}

/** Preference order, and the CSS each ink is emitted as. */
const INK_ORDER: readonly FillInk[] = ['bg', 'text', 'white', 'black']
const INK_CSS: Record<FillInk, string> = {
  bg: 'var(--sh-bg)', text: 'var(--sh-text)', white: '#fff', black: '#000',
}

/** The hex an ink stands for in a theme. */
export function inkHex(theme: FillTheme, ink: FillInk): string {
  if (ink === 'white') return '#fff'
  if (ink === 'black') return '#000'
  return THEME_INKS[theme][ink]
}

/** Light ``--sh-primary-fill`` = ``color-mix(primary 86%, #000)``;
 *  dark = the primary itself. */
const LIGHT_FILL_SHARE = 0.86
/** Hover = ``color-mix(fill 85%, #000 | #fff)``. */
const HOVER_FILL_SHARE = 0.85
const BLACK: Rgb = [0, 0, 0]
const WHITE: Rgb = [255, 255, 255]

/** The ``--sh-primary-fill-hover`` value for a hover toward black / white. */
export function fillHoverCss(toward: '#000' | '#fff'): string {
  return `color-mix(in srgb, var(--sh-primary-fill) ${Math.round(HOVER_FILL_SHARE * 100)}%, ${toward})`
}

/** ``--sh-primary-fill`` for this primary in this theme, or null if unparseable. */
export function fillHex(primary: string, theme: FillTheme): Rgb | null {
  const p = parseHex(primary)
  if (!p) return null
  return theme === 'light' ? mixRgb(p, BLACK, LIGHT_FILL_SHARE) : p
}

export interface OnFillChoice {
  ink: FillInk
  ratio: number
  /** Which way the hover fill moves — away from the ink. */
  hoverToward: '#000' | '#fff'
}

/** The ink for text on this primary's fill in this theme. */
export function onFillInk(primary: string, theme: FillTheme): OnFillChoice | null {
  const fill = fillHex(primary, theme)
  if (!fill) return null
  const ratios = INK_ORDER.map(ink => ({ ink, ratio: contrast(parseHex(inkHex(theme, ink))!, fill) }))
  const best = ratios.find(r => r.ratio >= AA_TEXT)
    ?? ratios.reduce((a, b) => (b.ratio > a.ratio ? b : a))
  const { ink, ratio } = best
  const inkRgb = parseHex(inkHex(theme, ink))!
  // A light ink wants a darker hover, a dark ink a lighter one.
  const hoverToward = contrast(inkRgb, WHITE) < contrast(inkRgb, BLACK) ? '#000' : '#fff'
  return { ink, ratio, hoverToward }
}

/** The hover fill (as RGB) for this primary in this theme. */
export function fillHoverHex(primary: string, theme: FillTheme): Rgb | null {
  const fill = fillHex(primary, theme)
  const choice = onFillInk(primary, theme)
  if (!fill || !choice) return null
  return mixRgb(fill, choice.hoverToward === '#000' ? BLACK : WHITE, HOVER_FILL_SHARE)
}

/**
 * Inline custom properties for ``<html>`` while a custom primary is
 * active. ``tokens.css`` reads ``--sh-on-primary-fill-{light,dark}`` /
 * ``--sh-primary-fill-hover-{light,dark}`` in the matching theme block
 * (per-theme, so a light ↔ dark toggle inside the space stays right).
 * ``{}`` for an unparseable colour — the CSS defaults then apply.
 */
export function primaryFillOverrides(primary: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const theme of ['light', 'dark'] as const) {
    const choice = onFillInk(primary, theme)
    if (!choice) return {}
    out[`--sh-on-primary-fill-${theme}`] = INK_CSS[choice.ink]
    out[`--sh-primary-fill-hover-${theme}`] = fillHoverCss(choice.hoverToward)
  }
  return out
}
