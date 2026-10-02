import { describe, it, expect } from 'vitest'
import {
  fillHex, fillHoverCss, fillHoverHex, inkHex, onFillInk, primaryFillOverrides,
  type FillInk,
} from './primaryFill'
import { AA_TEXT, contrast, parseHex } from './contrast'

const BRAND_LIGHT = '#D2542A'
const BRAND_DARK = '#E96A3F'
const THEMES = ['light', 'dark'] as const
const INKS: FillInk[] = ['bg', 'text', 'white', 'black']
const SAMPLES = [BRAND_LIGHT, BRAND_DARK, '#3A7D6E', '#8E6AC8', '#767676', '#2E86DE', '#F0C040', '#1D3557', '#000', '#fff']

const ink = (theme: 'light' | 'dark', i: FillInk) => parseHex(inkHex(theme, i))!

describe('text on a filled primary', () => {
  it('keeps the theme background ink on the brand hearth (light + dark)', () => {
    expect(onFillInk(BRAND_LIGHT, 'light')).toMatchObject({ ink: 'bg', hoverToward: '#000' })
    expect(onFillInk(BRAND_DARK, 'dark')).toMatchObject({ ink: 'bg', hoverToward: '#fff' })
    expect(onFillInk(BRAND_DARK, 'dark')!.ratio).toBeGreaterThanOrEqual(AA_TEXT)
  })

  it('rescues mid-tone custom primaries in dark mode', () => {
    // Under the dark --sh-bg ink both fail AA (#3A7D6E ≈ 3.9:1,
    // #8E6AC8 ≈ 4.3:1); neither theme ink reaches it on the teal.
    expect(contrast(ink('dark', 'bg'), parseHex('#3A7D6E')!)).toBeLessThan(AA_TEXT)
    expect(contrast(ink('dark', 'text'), parseHex('#3A7D6E')!)).toBeLessThan(AA_TEXT)
    expect(onFillInk('#3A7D6E', 'dark')).toMatchObject({ ink: 'white', hoverToward: '#000' })
    expect(onFillInk('#8E6AC8', 'dark')).toMatchObject({ ink: 'black', hoverToward: '#fff' })
    for (const hex of ['#3A7D6E', '#8E6AC8']) {
      expect(onFillInk(hex, 'dark')!.ratio).toBeGreaterThanOrEqual(AA_TEXT)
    }
  })

  it('takes the first ink in order that clears AA, else the best one', () => {
    for (const hex of SAMPLES) for (const theme of THEMES) {
      const r = onFillInk(hex, theme)!
      const fill = fillHex(hex, theme)!
      const ratios = INKS.map(i => contrast(ink(theme, i), fill))
      expect(r.ratio).toBeCloseTo(ratios[INKS.indexOf(r.ink)], 6)
      const first = ratios.findIndex(x => x >= AA_TEXT)
      if (first >= 0) expect(r.ink).toBe(INKS[first])
      else expect(r.ratio).toBeCloseTo(Math.max(...ratios), 6)
    }
  })

  it('prefers the theme inks over pure white / black', () => {
    expect(onFillInk('#F0C040', 'light')?.ink).toBe('text')
    expect(onFillInk('#1D3557', 'dark')?.ink).toBe('text')
    expect(onFillInk('#1D3557', 'light')?.ink).toBe('bg')
  })

  it('derives the fill like tokens.css: light = 86% primary + black, dark = primary', () => {
    expect(fillHex(BRAND_LIGHT, 'light')!.map(Math.round)).toEqual([181, 72, 36])
    expect(fillHex(BRAND_DARK, 'dark')).toEqual([233, 106, 63])
  })

  it('moves the hover away from the ink so it never loses contrast', () => {
    for (const hex of SAMPLES) for (const theme of THEMES) {
      const r = onFillInk(hex, theme)!
      expect(contrast(ink(theme, r.ink), fillHoverHex(hex, theme)!)).toBeGreaterThanOrEqual(r.ratio)
    }
    // The brand dark hover under the dark ink: was #C04E22 at 3.73:1.
    expect(contrast(ink('dark', 'bg'), fillHoverHex(BRAND_DARK, 'dark')!)).toBeGreaterThanOrEqual(AA_TEXT)
    expect(contrast(ink('light', 'bg'), fillHoverHex(BRAND_LIGHT, 'light')!)).toBeGreaterThanOrEqual(AA_TEXT)
  })

  it('emits per-theme custom properties that point at theme tokens', () => {
    expect(primaryFillOverrides('#3A7D6E')).toEqual({
      '--sh-on-primary-fill-light': 'var(--sh-bg)',
      '--sh-primary-fill-hover-light': fillHoverCss('#000'),
      '--sh-on-primary-fill-dark': '#fff',
      '--sh-primary-fill-hover-dark': fillHoverCss('#000'),
    })
    expect(primaryFillOverrides('#F0C040')['--sh-on-primary-fill-light']).toBe('var(--sh-text)')
    expect(fillHoverCss('#fff')).toBe('color-mix(in srgb, var(--sh-primary-fill) 85%, #fff)')
  })

  it('emits nothing for a colour it cannot parse (CSS defaults apply)', () => {
    expect(primaryFillOverrides('rebeccapurple')).toEqual({})
    expect(primaryFillOverrides('')).toEqual({})
    expect(onFillInk('nope', 'dark')).toBeNull()
    expect(fillHoverHex('nope', 'dark')).toBeNull()
  })
})
