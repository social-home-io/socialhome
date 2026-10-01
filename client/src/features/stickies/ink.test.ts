import { describe, it, expect } from 'vitest'
import {
  inkFor, stickyBackground, parseHex, contrastRatio, DARK_INK, LIGHT_INK, DEFAULT_STICKY_HEX,
} from './ink'
import { STICKY_COLORS } from '@/components/StickyDialog'

describe('sticky ink', () => {
  it('every palette colour keeps the dark ink at ≥ 4.5:1', () => {
    for (const c of STICKY_COLORS) {
      expect(inkFor(c.hex)).toBe('dark')
      expect(contrastRatio(DARK_INK, c.hex)).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('a dark custom colour gets the light ink, at ≥ 4.5:1', () => {
    expect(inkFor('#123456')).toBe('light')
    expect(contrastRatio(LIGHT_INK, '#123456')).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(DARK_INK, '#123456')).toBeLessThan(4.5)
    expect(inkFor('#000')).toBe('light')
  })

  it('a mid colour where neither reaches 4.5 picks the better one', () => {
    // #767676: dark ink ≈ 3.8:1, light ink ≈ 4.3:1.
    expect(inkFor('#767676')).toBe('light')
    expect(inkFor('#8a8a8a')).toBe('dark')
  })

  it('parses #RGB and #RRGGBB in any case, rejects the rest', () => {
    expect(parseHex('#fff')).toEqual([255, 255, 255])
    expect(parseHex('#B3d4FF')).toEqual([179, 212, 255])
    expect(parseHex(' #123456 ')).toEqual([18, 52, 86])
    for (const bad of ['', 'red', '#12345', '#GGGGGG', '123456', 'url(x)', '#1234567']) {
      expect(parseHex(bad)).toBeNull()
    }
  })

  it('an invalid colour falls back to the default palette colour', () => {
    expect(stickyBackground('nope')).toBe(DEFAULT_STICKY_HEX)
    expect(stickyBackground('')).toBe(DEFAULT_STICKY_HEX)
    expect(inkFor('nope')).toBe('dark')
    expect(stickyBackground('#123456')).toBe('#123456')
  })
})
