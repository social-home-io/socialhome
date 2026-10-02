import { describe, it, expect } from 'vitest'
import { parseHex, relativeLuminance, contrast, mixRgb, AA_TEXT } from './contrast'

describe('contrast helpers', () => {
  it('parses #RGB and #RRGGBB in any case, rejects the rest', () => {
    expect(parseHex('#fff')).toEqual([255, 255, 255])
    expect(parseHex('#B3d4FF')).toEqual([179, 212, 255])
    expect(parseHex(' #123456 ')).toEqual([18, 52, 86])
    for (const bad of ['', 'red', '#12345', '#GGGGGG', '123456', 'url(x)', '#1234567', 'rgb(1,2,3)']) {
      expect(parseHex(bad)).toBeNull()
    }
  })

  it('computes WCAG relative luminance at the extremes', () => {
    expect(relativeLuminance([0, 0, 0])).toBe(0)
    expect(relativeLuminance([255, 255, 255])).toBeCloseTo(1, 6)
  })

  it('computes the WCAG contrast ratio, order-independent', () => {
    expect(contrast([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 6)
    expect(contrast([255, 255, 255], [0, 0, 0])).toBeCloseTo(21, 6)
    expect(contrast([118, 118, 118], [255, 255, 255])).toBeGreaterThanOrEqual(AA_TEXT)
    // The dark hearth hover under the dark ink — the regression this module fixes.
    expect(contrast(parseHex('#C04E22')!, parseHex('#1A1612')!)).toBeCloseTo(3.73, 2)
  })

  it('mixes like CSS color-mix(in srgb, a W%, b)', () => {
    expect(mixRgb([255, 255, 255], [0, 0, 0], 0.5)).toEqual([127.5, 127.5, 127.5])
    expect(mixRgb([210, 84, 42], [0, 0, 0], 0.86)).toEqual([180.6, 72.24, 36.12])
    expect(mixRgb([10, 20, 30], [200, 200, 200], 1)).toEqual([10, 20, 30])
  })
})
